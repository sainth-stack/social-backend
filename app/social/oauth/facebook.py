"""Facebook (Meta) OAuth + Graph API for Page accounts."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status

from app.core.config import settings
from app.social.models import SocialAccountType, SocialPlatform
from app.social.oauth.base import OAuthAccountProfile, OAuthHandler, OAuthTokenResult

logger = logging.getLogger(__name__)

FACEBOOK_SCOPES = [
    "pages_manage_posts",
    "pages_read_engagement",
    "pages_show_list",
    "pages_read_user_content",
    "read_insights",
]


class FacebookOAuth(OAuthHandler):
    platform = SocialPlatform.FACEBOOK

    def __init__(self) -> None:
        self.app_id = settings.meta_app_id
        self.app_secret = settings.meta_app_secret
        self.api_version = settings.meta_api_version
        self.redirect_uri = settings.meta_social_redirect_uri.replace(
            "{platform}", "facebook"
        )

    def _require_credentials(self) -> None:
        if not self.app_id or not self.app_secret:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Facebook OAuth is not configured. Set META_APP_ID and "
                    "META_APP_SECRET in the backend environment."
                ),
            )

    def build_authorization_url(self, state: str) -> str:
        self._require_credentials()
        params = {
            "client_id": self.app_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "scope": ",".join(FACEBOOK_SCOPES),
            "response_type": "code",
        }
        return f"https://www.facebook.com/{self.api_version}/dialog/oauth?{urlencode(params)}"

    def exchange_code(self, code: str, *, state: str | None = None) -> OAuthTokenResult:
        self._require_credentials()
        short_lived = self._exchange_code_for_token(code)
        long_lived = self._exchange_for_long_lived(short_lived["access_token"])
        user_token = long_lived["access_token"]
        expires_in = int(long_lived.get("expires_in") or 60 * 60 * 24 * 60)
        token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        pages = self._fetch_pages(user_token)
        accounts: list[OAuthAccountProfile] = []
        for page in pages:
            page_token = page.get("access_token") or user_token
            page_id = str(page["id"])
            picture = None
            if isinstance(page.get("picture"), dict):
                picture = (page["picture"].get("data") or {}).get("url")
            followers = self._extract_followers(page)
            if followers <= 0:
                followers = self._fetch_follower_count(page_id, page_token)
            accounts.append(
                OAuthAccountProfile(
                    platform_account_id=page_id,
                    account_name=page.get("name") or "Facebook Page",
                    account_type=SocialAccountType.PAGE,
                    account_picture_url=picture,
                    follower_count=followers,
                    access_token=page_token,
                    refresh_token=None,
                    token_expires_at=token_expires_at,
                )
            )

        if not accounts:
            # Fall back to the user profile so connect still succeeds in sandbox apps.
            me = self._get_me(user_token)
            accounts.append(
                OAuthAccountProfile(
                    platform_account_id=str(me["id"]),
                    account_name=me.get("name") or "Facebook User",
                    account_type=SocialAccountType.PROFILE,
                    account_picture_url=(me.get("picture") or {}).get("data", {}).get("url"),
                    follower_count=0,
                    access_token=user_token,
                    refresh_token=None,
                    token_expires_at=token_expires_at,
                )
            )

        return OAuthTokenResult(accounts=accounts)

    def sync_account_stats(self, platform_account_id: str, access_token: str) -> dict:
        url = f"https://graph.facebook.com/{self.api_version}/{platform_account_id}"
        params = {
            "fields": "name,picture.type(large),followers_count,fan_count",
            "access_token": access_token,
        }
        data = self._get_json(url, params)
        picture = None
        if isinstance(data.get("picture"), dict):
            picture = (data["picture"].get("data") or {}).get("url")
        followers = self._extract_followers(data)
        if followers <= 0:
            followers = self._fetch_follower_count(platform_account_id, access_token)
        return {
            "account_name": data.get("name"),
            "account_picture_url": picture,
            "follower_count": followers,
        }

    # ── Internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_followers(data: dict[str, Any]) -> int:
        for key in ("followers_count", "fan_count", "follows_count"):
            val = data.get(key)
            if val is None:
                continue
            try:
                return max(0, int(val))
            except (TypeError, ValueError):
                continue
        return 0

    def _fetch_follower_count(self, page_id: str, access_token: str) -> int:
        """Fallback: Page insights when fan_count fields are missing (not when truly 0)."""
        url = f"https://graph.facebook.com/{self.api_version}/{page_id}/insights"
        for metric in ("page_fans", "page_follows"):
            try:
                with httpx.Client(timeout=30.0) as client:
                    response = client.get(
                        url,
                        params={
                            "metric": metric,
                            "period": "day",
                            "access_token": access_token,
                        },
                    )
                    if response.status_code >= 400:
                        continue
                    payload = response.json() or {}
                    rows = payload.get("data") or []
                    if not rows:
                        continue
                    values = rows[0].get("values") or []
                    if not values:
                        continue
                    raw = values[-1].get("value")
                    if raw is None:
                        continue
                    return max(0, int(raw))
            except Exception as exc:
                logger.info(
                    "Facebook insights %s unavailable for %s: %s",
                    metric,
                    page_id,
                    exc,
                )
        return 0

    def _exchange_code_for_token(self, code: str) -> dict[str, Any]:
        url = f"https://graph.facebook.com/{self.api_version}/oauth/access_token"
        params = {
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "redirect_uri": self.redirect_uri,
            "code": code,
        }
        return self._get_json(url, params)

    def _exchange_for_long_lived(self, short_token: str) -> dict[str, Any]:
        url = f"https://graph.facebook.com/{self.api_version}/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "fb_exchange_token": short_token,
        }
        return self._get_json(url, params)

    def _fetch_pages(self, user_token: str) -> list[dict[str, Any]]:
        url = f"https://graph.facebook.com/{self.api_version}/me/accounts"
        params = {
            "fields": "id,name,access_token,picture.type(large),followers_count,fan_count",
            "access_token": user_token,
            "limit": 100,
        }
        data = self._get_json(url, params)
        return list(data.get("data") or [])

    def _get_me(self, user_token: str) -> dict[str, Any]:
        url = f"https://graph.facebook.com/{self.api_version}/me"
        params = {
            "fields": "id,name,picture.type(large)",
            "access_token": user_token,
        }
        return self._get_json(url, params)

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            logger.error("Facebook Graph API error: %s", detail)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Facebook API error: {detail}",
            ) from exc
        except httpx.HTTPError as exc:
            logger.error("Facebook Graph API request failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to reach Facebook API",
            ) from exc

        if "error" in payload:
            message = payload["error"].get("message", "Unknown Facebook error")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Facebook API error: {message}",
            )
        return payload
