"""Upload social post images and videos to Azure Blob and return public HTTPS URLs.

Instagram (and other platforms) require a publicly reachable media URL — not
data: URIs or private localhost paths. We store bytes in Azure Blob and return
a long-lived read SAS URL Meta can fetch.
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, status

from app.providers.storage.factory import get_storage_provider

logger = logging.getLogger(__name__)

# Instagram needs to fetch the media; keep SAS valid for scheduled posts.
_SAS_EXPIRES_SECONDS = 60 * 60 * 24 * 30  # 30 days
_MAX_IMAGE_BYTES = 8 * 1024 * 1024    # 8 MB
_MAX_VIDEO_BYTES = 200 * 1024 * 1024  # 200 MB

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[\w/+.-]+);base64,(?P<data>.+)$",
    re.DOTALL,
)

_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
    "video/x-msvideo": "avi",
}


@dataclass(frozen=True)
class SocialBlobUpload:
    url: str
    blob_key: str
    content_type: str
    file_size: int


def refresh_blob_url(blob_key: str) -> str:
    """Return a fresh read SAS URL for an existing blob key."""
    return _public_url_for_key(blob_key)


def blob_key_from_url(url: str) -> Optional[str]:
    """Extract blob key from an Azure Blob SAS URL if possible."""
    try:
        parsed = urlparse(url)
        if "blob.core.windows.net" not in parsed.netloc.lower():
            return None
        path = parsed.path.lstrip("/")
        if not path or "/" not in path:
            return None
        # path is {container}/{key...}
        return path.split("/", 1)[1]
    except Exception:
        return None


def _public_url_for_key(key: str) -> str:
    storage = get_storage_provider()
    return storage.presigned_get_url(key, expires_in=_SAS_EXPIRES_SECONDS)


def upload_social_image_bytes(
    workspace_id: str | uuid.UUID,
    data: bytes,
    *,
    content_type: str = "image/jpeg",
    filename_hint: Optional[str] = None,
) -> SocialBlobUpload:
    """Upload image bytes to Azure Blob and return a public HTTPS (SAS) URL."""
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty image upload",
        )
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image must be under 8 MB",
        )

    mime = (content_type or "image/jpeg").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only image MIME types are accepted by this endpoint",
        )

    ext = _MIME_TO_EXT.get(mime)
    if not ext and filename_hint and "." in filename_hint:
        ext = filename_hint.rsplit(".", 1)[-1].lower()[:8]
    if not ext:
        ext = "jpg"

    key = f"social/{workspace_id}/{uuid.uuid4().hex}.{ext}"
    storage = get_storage_provider()
    storage.upload_bytes(key, data, content_type=mime)
    url = _public_url_for_key(key)
    logger.info("Uploaded social image org=%s key=%s bytes=%s", workspace_id, key, len(data))
    return SocialBlobUpload(url=url, blob_key=key, content_type=mime, file_size=len(data))


def upload_social_video_bytes(
    workspace_id: str | uuid.UUID,
    data: bytes,
    *,
    content_type: str = "video/mp4",
    filename_hint: Optional[str] = None,
) -> SocialBlobUpload:
    """Upload video bytes to Azure Blob and return a public HTTPS (SAS) URL."""
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty video upload",
        )
    if len(data) > _MAX_VIDEO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video must be under 200 MB",
        )

    mime = (content_type or "video/mp4").split(";")[0].strip().lower()
    if not mime.startswith("video/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only video MIME types are accepted by this endpoint",
        )

    ext = _MIME_TO_EXT.get(mime)
    if not ext and filename_hint and "." in filename_hint:
        ext = filename_hint.rsplit(".", 1)[-1].lower()[:8]
    if not ext:
        ext = "mp4"

    key = f"social-video/{workspace_id}/{uuid.uuid4().hex}.{ext}"
    storage = get_storage_provider()
    storage.upload_bytes(key, data, content_type=mime)
    url = _public_url_for_key(key)
    logger.info("Uploaded social video org=%s key=%s bytes=%s", workspace_id, key, len(data))
    return SocialBlobUpload(url=url, blob_key=key, content_type=mime, file_size=len(data))


def _upload_data_url(workspace_id: str | uuid.UUID, image_url: str) -> str:
    match = _DATA_URL_RE.match(image_url.strip())
    if not match:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid data URL for image upload",
        )
    mime = match.group("mime")
    try:
        data = base64.b64decode(match.group("data"), validate=False)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid base64 image data",
        ) from exc
    return upload_social_image_bytes(workspace_id, data, content_type=mime).url


def _upload_remote_url(workspace_id: str | uuid.UUID, image_url: str) -> str:
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            response = client.get(image_url)
            response.raise_for_status()
            data = response.content
            content_type = response.headers.get("content-type") or "image/jpeg"
    except httpx.HTTPError as exc:
        logger.warning("Failed to download image for blob upload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not download image to store in Azure Blob",
        ) from exc
    return upload_social_image_bytes(workspace_id, data, content_type=content_type).url


def _is_our_blob_url(image_url: str) -> bool:
    try:
        host = urlparse(image_url).netloc.lower()
    except Exception:
        return False
    return "blob.core.windows.net" in host


def ensure_public_image_url(
    workspace_id: str | uuid.UUID,
    image_url: Optional[str],
    *,
    force_reupload: bool = False,
) -> Optional[str]:
    """Return an HTTPS URL Instagram/Meta can fetch.

    - data: URIs → upload to Azure Blob
    - ephemeral remote URLs (e.g. OpenAI) → download + upload to Blob
    - existing Azure Blob URLs → keep (unless force_reupload)
    - other http(s) URLs → re-host on Blob so they stay available for schedules
    """
    if not image_url or not str(image_url).strip():
        return None

    url = str(image_url).strip()
    if url.startswith("data:"):
        return _upload_data_url(workspace_id, url)

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image must be an HTTP(S) URL or uploaded file",
        )

    if _is_our_blob_url(url) and not force_reupload:
        return url

    # Re-host so scheduled posts still work if the source URL expires.
    return _upload_remote_url(workspace_id, url)


def is_video_media_url(url: str | None) -> bool:
    if not url:
        return False
    lower = url.lower()
    return (
        "/social-video/" in lower
        or lower.endswith(".mp4")
        or lower.endswith(".mov")
        or lower.endswith(".webm")
    )
