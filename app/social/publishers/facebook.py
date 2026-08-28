"""Facebook Page post publisher (Graph API)."""

from __future__ import annotations

import base64
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.social.models import SocialPlatform
from app.social.publishers.base import BasePublisher, PublishResult, format_caption

logger = logging.getLogger(__name__)


class FacebookPublisher(BasePublisher):
    platform = SocialPlatform.FACEBOOK

    def __init__(self) -> None:
        self.api_version = settings.meta_api_version

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
        message = format_caption(caption, hashtags)
        if not message and not image_url:
            return PublishResult(
                success=False,
                error_code="INVALID_CONTENT",
                error_message="Post must include caption or image",
                retryable=False,
            )

        try:
            if image_url:
                post_id = self._publish_photo(
                    platform_account_id, access_token, message, image_url
                )
            else:
                post_id = self._publish_feed(platform_account_id, access_token, message)

            if first_comment and post_id:
                self._post_comment(post_id, access_token, first_comment)

            return PublishResult(success=True, platform_post_id=post_id)
        except httpx.HTTPStatusError as exc:
            return self._http_error(exc)
        except Exception as exc:
            logger.exception("Facebook publish failed: %s", exc)
            return PublishResult(
                success=False,
                error_code="API_ERROR",
                error_message=str(exc),
                retryable=True,
            )

    def _publish_feed(self, page_id: str, token: str, message: str) -> str:
        url = f"https://graph.facebook.com/{self.api_version}/{page_id}/feed"
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                url,
                data={"message": message, "access_token": token},
            )
            response.raise_for_status()
            data = response.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "Facebook API error"))
        return str(data.get("id") or data.get("post_id") or "")

    def _publish_photo(
        self, page_id: str, token: str, message: str, image_url: str
    ) -> str:
        url = f"https://graph.facebook.com/{self.api_version}/{page_id}/photos"
        with httpx.Client(timeout=90.0) as client:
            if image_url.startswith("data:"):
                # Multipart upload for data URIs
                match = re.match(r"data:(image/[^;]+);base64,(.+)", image_url, re.DOTALL)
                if not match:
                    raise RuntimeError("Invalid image data URI")
                content_type = match.group(1)
                raw = base64.b64decode(match.group(2))
                ext = "jpg" if "jpeg" in content_type else content_type.split("/")[-1]
                files = {"source": (f"image.{ext}", raw, content_type)}
                response = client.post(
                    url,
                    data={"caption": message, "access_token": token},
                    files=files,
                )
            elif urlparse(image_url).scheme in ("http", "https"):
                response = client.post(
                    url,
                    data={"url": image_url, "caption": message, "access_token": token},
                )
            else:
                raise RuntimeError("Unsupported image URL format")
            response.raise_for_status()
            data = response.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "Facebook API error"))
        # photos endpoint returns { id: photo_id, post_id: pageid_postid }
        return str(data.get("post_id") or data.get("id") or "")

    def _post_comment(self, post_id: str, token: str, message: str) -> None:
        url = f"https://graph.facebook.com/{self.api_version}/{post_id}/comments"
        try:
            with httpx.Client(timeout=30.0) as client:
                client.post(url, data={"message": message, "access_token": token})
        except Exception as exc:
            logger.warning("Facebook first comment failed (non-fatal): %s", exc)

    def _http_error(self, exc: httpx.HTTPStatusError) -> PublishResult:
        status_code = exc.response.status_code
        try:
            payload = exc.response.json()
            message = payload.get("error", {}).get("message") or exc.response.text
            code = payload.get("error", {}).get("code")
        except Exception:
            message = exc.response.text
            code = None

        error_code = "API_ERROR"
        retryable = status_code >= 500 or status_code == 429
        if status_code == 401 or code in (190, 102):
            error_code = "TOKEN_EXPIRED"
            retryable = False
        elif status_code == 429:
            error_code = "RATE_LIMITED"
        elif "image" in (message or "").lower():
            error_code = "INVALID_IMAGE"
            retryable = False

        return PublishResult(
            success=False,
            error_code=error_code,
            error_message=message,
            retryable=retryable,
        )
