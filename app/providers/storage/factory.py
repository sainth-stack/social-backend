"""
Storage provider — Azure Blob Storage.

Required env vars:
  AZURE_STORAGE_CONNECTION_STRING   (preferred)
  or: AZURE_STORAGE_ACCOUNT_NAME + AZURE_STORAGE_ACCOUNT_KEY
  AZURE_STORAGE_CONTAINER_NAME
  AZURE_STORAGE_ACCOUNT_NAME + AZURE_STORAGE_ACCOUNT_KEY  (needed for SAS/presigned URLs)
"""

from __future__ import annotations

from functools import lru_cache

from app.providers.storage.base import ObjectStorageProvider


@lru_cache(maxsize=1)
def get_storage_provider() -> ObjectStorageProvider:
    from app.providers.storage.azure_blob import AzureBlobStorageProvider
    return AzureBlobStorageProvider()
