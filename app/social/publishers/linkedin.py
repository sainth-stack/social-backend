"""LinkedIn UGC post publisher (member profile or Company Page)."""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

import httpx

from app.social.models import SocialPlatform
from app.social.publishers.base import BasePublisher, PublishResult, format_caption

logger = logging.getLogger(__name__)


def _author_urn(platform_account_id: str) -> str:
    """Build author URN from stored account id (person:X / workspace:Y / legacy)."""
    value = (platform_account_id or "").strip()
    if value.startswith("urn:li:"):
        return value
    if value.startswith("workspace:"):
        return f"urn:li:{value}"
    if value.startswith("person:"):
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

        if image_url and image_url.startswith("http"):
            logger.info("LinkedIn image attach skipped (text-only publish in this release)")

        try:
            with httpx.Client(timeout=60.0) as client:
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
                if response.status_code >= 400:
                    return self._error_result(response)
                data = response.json() if response.content else {}
                post_id = response.headers.get("x-restli-id") or data.get("id") or ""
                return PublishResult(success=True, platform_post_id=str(post_id))
        except httpx.HTTPError as exc:
            logger.exception("LinkedIn publish failed: %s", exc)
            return PublishResult(
                success=False,
                error_code="API_ERROR",
                error_message=str(exc),
                retryable=True,
            )

    def _error_result(self, response: httpx.Response) -> PublishResult:
        try:
            payload = response.json()
            message = (
                payload.get("message")
                or (payload.get("error") or {}).get("message")
                or response.text
            )
        except Exception:
            message = response.text

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
            error_message=message,
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
