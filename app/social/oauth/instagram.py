"""Instagram API with Instagram Login (Business Login).

Meta rejects instagram_business_* scopes on Facebook Login dialogs, and rejects
legacy instagram_basic / instagram_content_publish on apps using the current
Instagram use case. Connect must use Instagram OAuth (instagram.com) with the
Instagram App ID from the Meta dashboard Instagram product.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status

from app.core.config import settings
from app.social.models import SocialAccountType, SocialPlatform
from app.social.oauth.base import OAuthAccountProfile, OAuthHandler, OAuthTokenResult

logger = logging.getLogger(__name__)

INSTAGRAM_SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
]


class InstagramOAuth(OAuthHandler):
    platform = SocialPlatform.INSTAGRAM

    def __init__(self) -> None:
        # Instagram Login uses the Instagram product App ID/secret (not META_APP_ID).
        self.app_id = settings.meta_instagram_app_id or settings.meta_app_id
        self.app_secret = settings.meta_instagram_app_secret or settings.meta_app_secret
        self.api_version = settings.meta_api_version
        # Instagram Business Login rejects http://localhost — use HTTPS tunnel/prod URL.
        explicit = (settings.meta_instagram_redirect_uri or "").strip()
        self.redirect_uri = explicit or settings.meta_social_redirect_uri.replace(
            "{platform}", "instagram"
        )

    def _require_credentials(self) -> None:
        if not self.app_id or not self.app_secret:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Instagram OAuth is not configured. Set META_INSTAGRAM_APP_ID and "
                    "META_INSTAGRAM_APP_SECRET (from Meta → Instagram use case → "
                    "API setup with Instagram login) in the backend environment."
                ),
            )
        uri = (self.redirect_uri or "").strip()
        if not uri:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Set META_INSTAGRAM_REDIRECT_URI to an HTTPS callback "
                    "(e.g. https://xxxx.ngrok-free.app/api/v1/social/oauth/instagram/callback) "
                    "and add that exact URL in Meta → Valid OAuth Redirect URIs."
                ),
            )
        lowered = uri.lower()
        if lowered.startswith("http://localhost") or lowered.startswith("http://127.0.0.1"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Instagram rejects http://localhost redirect URIs. "
                    "Run `ngrok http 8001`, set META_INSTAGRAM_REDIRECT_URI to "
                    "https://<subdomain>.ngrok-free.app/api/v1/social/oauth/instagram/callback, "
                    "add the same URL in Meta Developer → Instagram → Valid OAuth Redirect URIs, "
                    "then restart the backend."
                ),
            )
        if not lowered.startswith("https://"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "META_INSTAGRAM_REDIRECT_URI must be HTTPS. "
                    f"Current value is invalid for Instagram Login: {uri}"
                ),
            )

    def build_authorization_url(self, state: str) -> str:
        self._require_credentials()
        params = {
            "client_id": self.app_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "scope": ",".join(INSTAGRAM_SCOPES),
            "response_type": "code",
        }
        return f"https://www.instagram.com/oauth/authorize?{urlencode(params)}"

    def exchange_code(self, code: str, *, state: str | None = None) -> OAuthTokenResult:
        self._require_credentials()
        # Instagram may append #_ to the code — strip fragment if present.
        code = code.split("#", 1)[0]
        short_lived = self._exchange_code_for_token(code)
        short_token = short_lived["access_token"]
        user_id = str(
            short_lived.get("user_id")
            or short_lived.get("id")
            or ""
        )

        long_lived = self._exchange_for_long_lived(short_token)
        access_token = long_lived.get("access_token") or short_token
        expires_in = int(long_lived.get("expires_in") or 60 * 60 * 24 * 60)
        token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        profile = self._fetch_ig_profile(access_token, preferred_user_id=user_id)
        platform_account_id = str(
            profile.get("user_id") or profile.get("id") or user_id
        )
        if not platform_account_id:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Instagram did not return a user id after OAuth.",
            )

        account = OAuthAccountProfile(
            platform_account_id=platform_account_id,
            account_name=profile.get("username")
            or profile.get("name")
            or "Instagram Account",
            account_type=SocialAccountType.PROFILE,
            account_picture_url=profile.get("profile_picture_url"),
            follower_count=int(profile.get("followers_count") or 0),
            access_token=access_token,
            refresh_token=None,
            token_expires_at=token_expires_at,
        )
        return OAuthTokenResult(accounts=[account])

    def sync_account_stats(self, platform_account_id: str, access_token: str) -> dict:
        profile = self._fetch_ig_profile(
            access_token, preferred_user_id=platform_account_id
        )
        return {
            "account_name": profile.get("username") or profile.get("name"),
            "account_picture_url": profile.get("profile_picture_url"),
            "follower_count": int(profile.get("followers_count") or 0),
        }

    # ── Internals ─────────────────────────────────────────────────────────────

    def _exchange_code_for_token(self, code: str) -> dict[str, Any]:
        url = "https://api.instagram.com/oauth/access_token"
        data = {
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
            "code": code,
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, data=data)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            logger.error("Instagram token exchange error: %s", detail)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Instagram API error: {detail}",
            ) from exc
        except httpx.HTTPError as exc:
            logger.error("Instagram token exchange failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to reach Instagram API",
            ) from exc

        # Newer responses wrap the token in data[0].
        if isinstance(payload.get("data"), list) and payload["data"]:
            return payload["data"][0]
        if "access_token" in payload:
            return payload
        if "error" in payload or "error_message" in payload:
            message = (
                payload.get("error_message")
                or (payload.get("error") or {}).get("message")
                or str(payload)
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Instagram API error: {message}",
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Instagram token exchange returned an unexpected response",
        )

    def _exchange_for_long_lived(self, short_token: str) -> dict[str, Any]:
        url = "https://graph.instagram.com/access_token"
        params = {
            "grant_type": "ig_exchange_token",
            "client_secret": self.app_secret,
            "access_token": short_token,
        }
        return self._get_json(url, params)

    def _fetch_ig_profile(
        self, access_token: str, *, preferred_user_id: str = ""
    ) -> dict[str, Any]:
        fields = "user_id,username,name,account_type,profile_picture_url,followers_count"
        # Prefer /me; fall back to explicit user id when needed.
        try:
            return self._get_json(
                f"https://graph.instagram.com/{self.api_version}/me",
                {"fields": fields, "access_token": access_token},
            )
        except HTTPException:
            if not preferred_user_id:
                raise
            return self._get_json(
                f"https://graph.instagram.com/{self.api_version}/{preferred_user_id}",
                {"fields": fields, "access_token": access_token},
            )

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            logger.error("Instagram Graph API error: %s", detail)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Instagram API error: {detail}",
            ) from exc
        except httpx.HTTPError as exc:
            logger.error("Instagram Graph API request failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to reach Instagram API",
            ) from exc

        if "error" in payload:
            message = payload["error"].get("message", "Unknown Instagram error")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Instagram API error: {message}",
            )
        return payload
