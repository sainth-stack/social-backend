"""LinkedIn UGC post publisher (member profile or Company Page)."""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional
from urllib.parse import quote

import httpx
from PIL import Image

from app.social.models import SocialPlatform
from app.social.publishers.base import BasePublisher, PublishResult, format_caption

logger = logging.getLogger(__name__)


def _author_urn(platform_account_id: str) -> str:
    """Build author URN from stored account id (person:X / workspace:Y / legacy)."""
    value = (platform_account_id or "").strip()
    if value.startswith("urn:li:workspace:"):
        return value.replace("urn:li:workspace:", "urn:li:organization:", 1)
    if value.startswith("urn:li:"):
        return value
    if value.startswith("workspace:"):
        return f"urn:li:organization:{value.split(':', 1)[1]}"
    if value.startswith(("person:", "organization:")):
        return f"urn:li:{value}"
    # Legacy bare person id from earlier connects
    return f"urn:li:person:{value}"


class LinkedInPublisher(BasePublisher):
    platform = SocialPlatform.LINKEDIN

    def publish(
        self,
        *,
        platform_account_id: str,
        access_token: str,
        caption: str,
        hashtags: list[str],
        image_url: Optional[str] = None,
        first_comment: Optional[str] = None,
    ) -> PublishResult:
        _ = first_comment  # LinkedIn first-comment not supported in this release
        text = format_caption(caption, hashtags)
        if not text:
            return PublishResult(
                success=False,
                error_code="INVALID_CONTENT",
                error_message="LinkedIn post must include caption text",
                retryable=False,
            )

        author = _author_urn(platform_account_id)
        body = {
            "author": author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text[:3000]},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                if image_url:
                    media = self._upload_image(client, image_url, author, access_token)
                    if isinstance(media, PublishResult):
                        return media
                    body["specificContent"]["com.linkedin.ugc.ShareContent"].update(
                        shareMediaCategory="IMAGE",
                        media=[{"status": "READY", "media": media}],
                    )
                response = client.post(
                    "https://api.linkedin.com/v2/ugcPosts",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                        "X-Restli-Protocol-Version": "2.0.0",
                        "LinkedIn-Version": "202401",
                    },
                    json=body,
                )
                if not response.is_success:
                    return self._error_result(response)
                data = response.json() if response.content else {}
                post_id = response.headers.get("x-restli-id") or data.get("id") or ""
                return PublishResult(success=True, platform_post_id=str(post_id))
        except httpx.HTTPError:
            logger.warning("LinkedIn publish request failed")
            return PublishResult(
                success=False,
                error_code="API_ERROR",
                error_message="LinkedIn publish request failed",
                retryable=True,
            )
        except (ValueError, TypeError, AttributeError):
            return PublishResult(
                success=False,
                error_code="API_ERROR",
                error_message="Invalid LinkedIn publish response",
                retryable=False,
            )

    def _upload_image(
        self, client: httpx.Client, image_url: str, author: str, access_token: str
    ) -> str | PublishResult:
        max_bytes = 10 * 1024 * 1024
        try:
            if httpx.URL(image_url).scheme not in ("http", "https"):
                raise ValueError
            with client.stream("GET", image_url, follow_redirects=True) as response:
                if not response.is_success:
                    return self._error_result(response)
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                expected_format = {"image/jpeg": "JPEG", "image/png": "PNG"}.get(mime)
                if not expected_format:
                    raise ValueError
                length = response.headers.get("content-length")
                if length is not None and not 0 < int(length) <= max_bytes:
                    raise ValueError
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if len(content) + len(chunk) > max_bytes:
                        raise ValueError
                    content.extend(chunk)
            if not content:
                raise ValueError
            with Image.open(BytesIO(content)) as image:
                if image.format != expected_format:
                    raise ValueError
                image.verify()
        except (ValueError, httpx.InvalidURL, OSError, Image.DecompressionBombError):
            return PublishResult(
                success=False,
                error_code="INVALID_IMAGE",
                error_message="LinkedIn requires a valid JPEG or PNG image up to 10 MB at an HTTP(S) URL",
                retryable=False,
            )

        headers = {
            "Authorization": f"Bearer {access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
        }
        response = client.post(
            "https://api.linkedin.com/v2/assets?action=registerUpload",
            headers=headers,
            json={
                "registerUploadRequest": {
                    "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                    "owner": author,
                    "serviceRelationships": [
                        {"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}
                    ],
                }
            },
        )
        if not response.is_success:
            return self._error_result(response)
        try:
            value = response.json()["value"]
            asset = value["asset"]
            upload = value["uploadMechanism"]["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]
            upload_url = upload["uploadUrl"]
            if not isinstance(asset, str) or not asset.startswith("urn:li:digitalmediaAsset:"):
                raise ValueError
            if not isinstance(upload_url, str) or httpx.URL(upload_url).scheme not in ("http", "https"):
                raise ValueError
            upload_headers = dict(upload.get("headers") or {})
            upload_headers.update(Authorization=f"Bearer {access_token}")
            upload_headers["Content-Type"] = mime
        except (KeyError, TypeError, ValueError, httpx.InvalidURL):
            return PublishResult(
                success=False,
                error_code="API_ERROR",
                error_message="Invalid LinkedIn image registration response",
                retryable=False,
            )
        response = client.put(upload_url, headers=upload_headers, content=bytes(content))
        if not response.is_success:
            return self._error_result(response)
        return asset

    def _error_result(self, response: httpx.Response) -> PublishResult:
        status_code = response.status_code
        error_code = "API_ERROR"
        retryable = status_code >= 500 or status_code == 429
        if status_code in (401, 403):
            error_code = "TOKEN_EXPIRED" if status_code == 401 else "PERMISSION_DENIED"
            retryable = False
        elif status_code == 429:
            error_code = "RATE_LIMITED"

        return PublishResult(
            success=False,
            error_code=error_code,
            error_message=f"LinkedIn request failed (HTTP {status_code})",
            retryable=retryable,
        )

    def delete_remote(
        self,
        *,
        platform_post_id: str,
        platform_account_id: str,
        access_token: str,
    ) -> PublishResult:
        _ = platform_account_id
        raw_id = (platform_post_id or "").strip()
        if not raw_id:
            return PublishResult(
                success=False,
                error_code="INVALID_CONTENT",
                error_message="Missing LinkedIn post id",
                retryable=False,
            )
        if raw_id.startswith("urn:li:"):
            post_urn = raw_id
        elif raw_id.startswith("ugcPost:"):
            post_urn = f"urn:li:{raw_id}"
        else:
            post_urn = f"urn:li:ugcPost:{raw_id}"
        encoded = quote(post_urn, safe="")
        url = f"https://api.linkedin.com/rest/posts/{encoded}"
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.delete(
                    url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "X-Restli-Protocol-Version": "2.0.0",
                        "LinkedIn-Version": "202401",
                    },
                )
                if response.status_code >= 400:
                    return self._error_result(response)
            return PublishResult(success=True, platform_post_id=post_urn)
        except httpx.HTTPError as exc:
            logger.exception("LinkedIn delete failed: %s", exc)
            return PublishResult(
                success=False,
                error_code="API_ERROR",
                error_message=str(exc),
                retryable=True,
            )
