"""
Abstract base class for object storage providers.

Implement this interface to add a new storage backend (GCS, Cloudflare R2, etc.).
All callers depend only on this interface — never on a concrete implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import BinaryIO


class ObjectStorageProvider(ABC):
    """Provider-agnostic object storage interface."""

    # ── Key helpers ────────────────────────────────────────────────────────────

    @abstractmethod
    def build_key(self, workspace_id: str, document_id: str, extension: str) -> str:
        """Build the canonical storage key for an uploaded asset."""

    @abstractmethod
    def storage_bucket(self) -> str:
        """Return the bucket / container name being used (for DB persistence)."""

    # ── Core operations ────────────────────────────────────────────────────────

    @abstractmethod
    def upload_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        bucket: str | None = None,
    ) -> None:
        """Upload raw bytes to the given key."""

    @abstractmethod
    def upload_fileobj(
        self,
        key: str,
        fileobj: BinaryIO,
        *,
        content_type: str | None = None,
        bucket: str | None = None,
    ) -> None:
        """Upload a file-like object to the given key."""

    @abstractmethod
    def download_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        """Download and return the full content of an object."""

    @abstractmethod
    def object_exists(self, key: str, *, bucket: str | None = None) -> bool:
        """Return True if the object exists."""

    @abstractmethod
    def delete_object(self, key: str, *, bucket: str | None = None) -> None:
        """Delete an object. Silently no-ops if it does not exist."""

    # ── Presigned URLs ─────────────────────────────────────────────────────────

    @abstractmethod
    def presigned_put_url(
        self,
        key: str,
        *,
        content_type: str | None = None,
        expires_in: int = 3600,
    ) -> str:
        """Return a pre-signed URL that allows a client to PUT an object."""

    @abstractmethod
    def presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_in: int = 3600,
    ) -> str:
        """Return a pre-signed URL that allows a client to GET an object."""
