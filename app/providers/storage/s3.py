"""Amazon S3 object storage provider."""

from __future__ import annotations

import logging
from typing import BinaryIO
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, status

from app.core.config import settings
from app.providers.storage.base import ObjectStorageProvider

logger = logging.getLogger(__name__)


def _client():
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is not installed. Run: pip install boto3") from exc

    return boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )


class S3StorageProvider(ObjectStorageProvider):
    def __init__(self) -> None:
        if not settings.aws_bucket_name:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AWS_BUCKET_NAME is not configured",
            )
        self._bucket = settings.aws_bucket_name
        self._prefix = (settings.aws_storage_prefix or "").strip("/")

    def _full_key(self, key: str) -> str:
        key = key.lstrip("/")
        if self._prefix and not key.startswith(f"{self._prefix}/"):
            return f"{self._prefix}/{key}"
        return key

    def build_key(self, workspace_id: str, document_id: str, extension: str) -> str:
        ext = extension.lstrip(".")
        return self._full_key(f"social/{workspace_id}/{document_id}/original.{ext}")

    @property
    def storage_bucket(self) -> str:
        return self._bucket

    def upload_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        bucket: str | None = None,
    ) -> None:
        bucket_name = bucket or self._bucket
        full_key = self._full_key(key)
        extra: dict = {}
        if content_type:
            extra["ContentType"] = content_type
        try:
            client = _client()
            try:
                client.put_object(
                    Bucket=bucket_name,
                    Key=full_key,
                    Body=data,
                    ACL="public-read",
                    **extra,
                )
            except ClientError as acl_exc:
                logger.info("S3 public-read ACL skipped for %s: %s", full_key, acl_exc)
                client.put_object(Bucket=bucket_name, Key=full_key, Body=data, **extra)
        except (ClientError, BotoCoreError) as exc:
            logger.exception("S3 upload failed key=%s", full_key)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="S3 upload failed",
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
        bucket_name = bucket or self._bucket
        full_key = self._full_key(key)
        try:
            response = _client().get_object(Bucket=bucket_name, Key=full_key)
            return response["Body"].read()
        except (ClientError, BotoCoreError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="S3 object not found",
            ) from exc

    def object_exists(self, key: str, *, bucket: str | None = None) -> bool:
        bucket_name = bucket or self._bucket
        full_key = self._full_key(key)
        try:
            _client().head_object(Bucket=bucket_name, Key=full_key)
            return True
        except ClientError:
            return False

    def delete_object(self, key: str, *, bucket: str | None = None) -> None:
        bucket_name = bucket or self._bucket
        full_key = self._full_key(key)
        try:
            _client().delete_object(Bucket=bucket_name, Key=full_key)
        except (ClientError, BotoCoreError) as exc:
            logger.warning("S3 delete failed key=%s: %s", full_key, exc)

    def presigned_put_url(
        self,
        key: str,
        *,
        content_type: str | None = None,
        expires_in: int = 3600,
    ) -> str:
        full_key = self._full_key(key)
        params: dict = {"Bucket": self._bucket, "Key": full_key}
        if content_type:
            params["ContentType"] = content_type
        try:
            return _client().generate_presigned_url(
                "put_object",
                Params=params,
                ExpiresIn=expires_in,
            )
        except (ClientError, BotoCoreError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to create S3 presigned PUT URL",
            ) from exc

    def presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_in: int = 3600,
    ) -> str:
        bucket_name = bucket or self._bucket
        full_key = self._full_key(key)
        try:
            return _client().generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket_name, "Key": full_key},
                ExpiresIn=expires_in,
            )
        except (ClientError, BotoCoreError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to create S3 presigned GET URL",
            ) from exc

    def public_url(self, key: str, *, bucket: str | None = None) -> str:
        bucket_name = bucket or self._bucket
        full_key = self._full_key(key)
        encoded = quote(full_key, safe="/")
        region = settings.aws_region or "us-east-1"
        return f"https://{bucket_name}.s3.{region}.amazonaws.com/{encoded}"
