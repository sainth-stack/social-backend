"""Abstract OAuth handler for social platforms."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.social.models import SocialAccountType, SocialPlatform


@dataclass
class OAuthAccountProfile:
    platform_account_id: str
    account_name: str
    account_type: SocialAccountType
    account_picture_url: Optional[str] = None
    follower_count: int = 0
    access_token: str = ""
    refresh_token: Optional[str] = None
    token_expires_at: Optional[datetime] = None


@dataclass
class OAuthTokenResult:
    accounts: list[OAuthAccountProfile] = field(default_factory=list)


class OAuthHandler(ABC):
    platform: SocialPlatform

    @abstractmethod
    def build_authorization_url(self, state: str) -> str:
        """Return the platform authorization URL."""

    @abstractmethod
    def exchange_code(self, code: str, *, state: Optional[str] = None) -> OAuthTokenResult:
        """Exchange an authorization code for tokens and account profiles."""

    @abstractmethod
    def sync_account_stats(
        self,
        platform_account_id: str,
        access_token: str,
    ) -> dict:
        """Fetch latest follower count / picture for an account."""


def get_oauth_handler(platform: SocialPlatform | str) -> OAuthHandler:
    from app.social.oauth.facebook import FacebookOAuth
    from app.social.oauth.instagram import InstagramOAuth
    from app.social.oauth.linkedin import LinkedInOAuth
    from app.social.oauth.x_twitter import XTwitterOAuth

    value = platform.value if isinstance(platform, SocialPlatform) else platform
    if value == SocialPlatform.FACEBOOK.value:
        return FacebookOAuth()
    if value == SocialPlatform.INSTAGRAM.value:
        return InstagramOAuth()
    if value == SocialPlatform.LINKEDIN.value:
        return LinkedInOAuth()
    if value == SocialPlatform.X.value:
        return XTwitterOAuth()
    raise ValueError(f"OAuth not supported for platform: {value}")


def generate_state_token() -> str:
    return uuid.uuid4().hex
