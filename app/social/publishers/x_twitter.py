"""X (Twitter) tweet publisher — text only on free tier."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.social.models import SocialPlatform
from app.social.publishers.base import BasePublisher, PublishResult, format_caption

logger = logging.getLogger(__name__)


class XTwitterPublisher(BasePublisher):
    platform = SocialPlatform.X

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
        _ = platform_account_id
        _ = first_comment

        if image_url:
            return PublishResult(
                success=False,
                error_code="INVALID_IMAGE",
                error_message=(
                    "X free tier supports text-only tweets. Remove the image or "
                    "publish without an image for X."
                ),
                retryable=False,
            )

        text = format_caption(caption, hashtags)
        if not text:
            return PublishResult(
                success=False,
                error_code="INVALID_CONTENT",
                error_message="Tweet text is empty",
                retryable=False,
            )
        if len(text) > 280:
            text = text[:279] + "…"

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    "https://api.twitter.com/2/tweets",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    json={"text": text},
                )
                if response.status_code >= 400:
                    return self._error_result(response)
                data = response.json()
                tweet_id = (data.get("data") or {}).get("id")
                return PublishResult(success=True, platform_post_id=str(tweet_id or ""))
        except httpx.HTTPError as exc:
            logger.exception("X publish failed: %s", exc)
            return PublishResult(
                success=False,
                error_code="API_ERROR",
                error_message=str(exc),
                retryable=True,
            )

    def _error_result(self, response: httpx.Response) -> PublishResult:
        try:
            payload = response.json()
            errors = payload.get("errors") or []
            if errors:
                message = errors[0].get("message") or str(errors[0])
            else:
                message = payload.get("detail") or payload.get("title") or response.text
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
