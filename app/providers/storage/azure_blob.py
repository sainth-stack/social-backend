"""
Azure Blob Storage provider.

Configure:
  AZURE_STORAGE_CONNECTION_STRING   (preferred — includes account + key)
  or:
  AZURE_STORAGE_ACCOUNT_NAME + AZURE_STORAGE_ACCOUNT_KEY
  AZURE_STORAGE_CONTAINER_NAME      (required)
  AZURE_STORAGE_PREFIX              (optional, default: social)

Presigned URLs are implemented via Azure SAS tokens with the same
expiry semantics as S3 presigned URLs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import BinaryIO

from fastapi import HTTPException, status

from app.core.config import settings
from app.providers.storage.base import ObjectStorageProvider

logger = logging.getLogger(__name__)


def _get_service_client():
    """Lazy import + build the BlobServiceClient from settings."""
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError as exc:
        raise RuntimeError(
            "azure-storage-blob is not installed. "
            "Run: pip install azure-storage-blob"
        ) from exc

    if settings.azure_storage_connection_string:
        return BlobServiceClient.from_connection_string(settings.azure_storage_connection_string)

    if settings.azure_storage_account_name and settings.azure_storage_account_key:
        account_url = f"https://{settings.azure_storage_account_name}.blob.core.windows.net"
        from azure.core.credentials import AzureNamedKeyCredential

        credential = AzureNamedKeyCredential(
            settings.azure_storage_account_name,
            settings.azure_storage_account_key,
        )
        return BlobServiceClient(account_url=account_url, credential=credential)

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Azure Blob Storage is not configured. "
            "Set AZURE_STORAGE_CONNECTION_STRING or "
            "AZURE_STORAGE_ACCOUNT_NAME + AZURE_STORAGE_ACCOUNT_KEY."
        ),
    )


def _container_name(bucket: str | None = None) -> str:
    name = bucket or settings.azure_storage_container_name
    if not name:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AZURE_STORAGE_CONTAINER_NAME is not configured",
        )
    return name


class AzureBlobStorageProvider(ObjectStorageProvider):
    """Azure Blob Storage implementation of ObjectStorageProvider."""

    _ensured_containers: set[str] = set()

    def build_key(self, workspace_id: str, document_id: str, extension: str) -> str:
        ext = extension.lstrip(".")
        prefix = settings.azure_storage_prefix.strip("/") or "social"
        return f"{prefix}/{workspace_id}/{document_id}/original.{ext}"

    def storage_bucket(self) -> str:
        return _container_name()

    def _ensure_container(self, container: str) -> None:
        """Create the blob container if missing (fixes ContainerNotFound on first upload)."""
        if container in AzureBlobStorageProvider._ensured_containers:
            return
        service = _get_service_client()
        cc = service.get_container_client(container)
        try:
            if not cc.exists():
                logger.info("Creating Azure Blob container %s", container)
                cc.create_container()
        except Exception as exc:
            # Race: another worker created it, or exists check failed — try create once.
            try:
                cc.create_container()
            except Exception as create_exc:
                # AlreadyExists is fine; anything else is logged and re-raised on upload.
                msg = str(create_exc).lower()
                if "containeralreadyexists" not in msg and "already exists" not in msg:
                    logger.warning(
                        "Could not ensure container %s: exists=%s create=%s",
                        container,
                        exc,
                        create_exc,
                    )
                    return
        AzureBlobStorageProvider._ensured_containers.add(container)

    def _container_client(self, bucket: str | None = None):
        service = _get_service_client()
        container = _container_name(bucket)
        self._ensure_container(container)
        return service.get_container_client(container)

    def upload_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        bucket: str | None = None,
    ) -> None:
        try:
            cc = self._container_client(bucket)
            kwargs: dict = {
                "overwrite": True,
                "connection_timeout": 600,
            }
            if content_type:
                from azure.storage.blob import ContentSettings
                kwargs["content_settings"] = ContentSettings(content_type=content_type)
            try:
                cc.upload_blob(name=key, data=data, **kwargs)
            except Exception as first_exc:
                # Retry once after forcing container create (handles ContainerNotFound).
                err = str(first_exc)
                if "ContainerNotFound" in err or "ContainerNotFound" in type(first_exc).__name__:
                    AzureBlobStorageProvider._ensured_containers.discard(_container_name(bucket))
                    self._ensure_container(_container_name(bucket))
                    cc = self._container_client(bucket)
                    cc.upload_blob(name=key, data=data, **kwargs)
                else:
                    raise
        except Exception as exc:
            logger.exception("Azure Blob upload failed key=%s", key)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Azure Blob Storage upload failed",
            ) from exc

    def upload_fileobj(
        self,
        key: str,
        fileobj: BinaryIO,
        *,
        content_type: str | None = None,
        bucket: str | None = None,
    ) -> None:
        self.upload_bytes(key, fileobj.read(), content_type=content_type, bucket=bucket)

    def download_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        try:
            cc = self._container_client(bucket)
            blob = cc.get_blob_client(key)
            return blob.download_blob().readall()
        except Exception as exc:
            logger.exception("Azure Blob download failed key=%s", key)
            raise RuntimeError(f"Failed to download blob: {key}") from exc

    def object_exists(self, key: str, *, bucket: str | None = None) -> bool:
        try:
            cc = self._container_client(bucket)
            blob = cc.get_blob_client(key)
            return blob.exists()
        except Exception:
            logger.exception("Azure Blob exists check failed key=%s", key)
            return False

    def delete_object(self, key: str, *, bucket: str | None = None) -> None:
        if not key:
            return
        try:
            cc = self._container_client(bucket)
            cc.delete_blob(key, delete_snapshots="include")
        except Exception:
            logger.exception("Azure Blob delete failed key=%s", key)

    def presigned_put_url(
        self,
        key: str,
        *,
        content_type: str | None = None,
        expires_in: int = 3600,
    ) -> str:
        """Generate a SAS URL for uploading (equivalent to S3 presigned PUT)."""
        try:
            from azure.storage.blob import (
                BlobSasPermissions,
                generate_blob_sas,
            )
        except ImportError as exc:
            raise RuntimeError("azure-storage-blob is not installed") from exc

        container = _container_name()
        account_name = settings.azure_storage_account_name
        account_key = settings.azure_storage_account_key

        if not account_name or not account_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "AZURE_STORAGE_ACCOUNT_NAME and AZURE_STORAGE_ACCOUNT_KEY are required "
                    "for presigned URLs (connection string alone is not sufficient)."
                ),
            )

        expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=container,
            blob_name=key,
            account_key=account_key,
            permission=BlobSasPermissions(write=True, create=True),
            expiry=expiry,
            content_type=content_type,
        )
        url = (
            f"https://{account_name}.blob.core.windows.net"
            f"/{container}/{key}?{sas_token}"
        )
        return url

    def presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_in: int = 3600,
    ) -> str:
        """Generate a SAS URL for downloading (equivalent to S3 presigned GET)."""
        try:
            from azure.storage.blob import (
                BlobSasPermissions,
                generate_blob_sas,
            )
        except ImportError as exc:
            raise RuntimeError("azure-storage-blob is not installed") from exc

        container = _container_name(bucket)
        account_name = settings.azure_storage_account_name
        account_key = settings.azure_storage_account_key

        if not account_name or not account_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "AZURE_STORAGE_ACCOUNT_NAME and AZURE_STORAGE_ACCOUNT_KEY are required "
                    "for presigned URLs."
                ),
            )

        expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=container,
            blob_name=key,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )
        url = (
            f"https://{account_name}.blob.core.windows.net"
            f"/{container}/{key}?{sas_token}"
        )
        return url
