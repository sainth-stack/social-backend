"""
Storage provider factory — Amazon S3 (preferred) or Azure Blob Storage.

S3 (set all three):
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  AWS_BUCKET_NAME
  AWS_REGION              (default ap-south-1)
  AWS_STORAGE_PREFIX      (default peers)

Azure Blob (fallback):
  AZURE_STORAGE_CONNECTION_STRING
  or AZURE_STORAGE_ACCOUNT_NAME + AZURE_STORAGE_ACCOUNT_KEY
  AZURE_STORAGE_CONTAINER_NAME
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import settings
from app.providers.storage.base import ObjectStorageProvider


def _use_s3() -> bool:
    return bool(
        settings.aws_access_key_id
        and settings.aws_secret_access_key
        and settings.aws_bucket_name
    )


@lru_cache(maxsize=1)
def get_storage_provider() -> ObjectStorageProvider:
    if _use_s3():
        from app.providers.storage.s3 import S3StorageProvider

        return S3StorageProvider()

    from app.providers.storage.azure_blob import AzureBlobStorageProvider

    return AzureBlobStorageProvider()
