"""Storage provider factory — Amazon S3 only."""

from __future__ import annotations

from functools import lru_cache

from app.providers.storage.base import ObjectStorageProvider


@lru_cache(maxsize=1)
def get_storage_provider() -> ObjectStorageProvider:
    from app.providers.storage.s3 import S3StorageProvider
    return S3StorageProvider()
