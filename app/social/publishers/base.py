"""Abstract publisher interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from app.social.models import SocialPlatform


@dataclass
class PublishResult:
    success: bool
    platform_post_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    retryable: bool = False


class BasePublisher(ABC):
    platform: SocialPlatform

    @abstractmethod
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
        """Publish content to the platform and return the result."""


def get_publisher(platform: SocialPlatform | str) -> BasePublisher:
    from app.social.publishers.facebook import FacebookPublisher
    from app.social.publishers.instagram import InstagramPublisher
    from app.social.publishers.linkedin import LinkedInPublisher
    from app.social.publishers.x_twitter import XTwitterPublisher

    value = platform.value if isinstance(platform, SocialPlatform) else platform
    if value == SocialPlatform.FACEBOOK.value:
        return FacebookPublisher()
    if value == SocialPlatform.INSTAGRAM.value:
        return InstagramPublisher()
    if value == SocialPlatform.LINKEDIN.value:
        return LinkedInPublisher()
    if value == SocialPlatform.X.value:
        return XTwitterPublisher()
    raise ValueError(f"Publisher not available for platform: {value}")


NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        "TOKEN_EXPIRED",
        "INVALID_IMAGE",
        "ACCOUNT_NOT_FOUND",
        "PERMISSION_DENIED",
        "UNSUPPORTED_PLATFORM",
        "INVALID_CONTENT",
    }
)

# Exponential backoff seconds by retry_count (0-indexed attempt after failure)
RETRY_BACKOFF_SECONDS = [
    5 * 60,  # 5 min
    30 * 60,  # 30 min
    2 * 60 * 60,  # 2 hr
    6 * 60 * 60,  # 6 hr
    24 * 60 * 60,  # 24 hr
]
MAX_RETRIES = 5


def is_retryable_error(error_code: str | None, retryable_flag: bool = False) -> bool:
    if not error_code:
        return retryable_flag
    if error_code in NON_RETRYABLE_ERROR_CODES:
        return False
    if error_code in ("RATE_LIMITED", "API_ERROR"):
        return True
    return retryable_flag


def backoff_seconds(retry_count: int, error_code: str | None = None) -> int:
    idx = min(max(retry_count, 0), len(RETRY_BACKOFF_SECONDS) - 1)
    seconds = RETRY_BACKOFF_SECONDS[idx]
    if error_code == "RATE_LIMITED":
        return max(seconds, 30 * 60)
    return seconds



def format_caption(caption: str, hashtags: list[str]) -> str:
    tags = " ".join(f"#{h.lstrip('#')}" for h in (hashtags or []) if h)
    if not tags:
        return (caption or "").strip()
    return f"{(caption or '').strip()}\n\n{tags}".strip()
