"""X (Twitter) OAuth 2.0 with PKCE."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status

from app.core.config import settings
from app.social.models import SocialAccountType, SocialPlatform
from app.social.oauth.base import OAuthAccountProfile, OAuthHandler, OAuthTokenResult
from workers.redis.client import get_redis_client

logger = logging.getLogger(__name__)

X_SCOPES = ["tweet.read", "tweet.write", "users.read", "offline.access"]


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


class XTwitterOAuth(OAuthHandler):
    platform = SocialPlatform.X

    def __init__(self) -> None:
        self.client_id = settings.x_client_id
        self.client_secret = settings.x_client_secret
        self.redirect_uri = settings.x_redirect_uri

    def _require_credentials(self) -> None:
        if not self.client_id:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "X OAuth is not configured. Set X_CLIENT_ID (and X_CLIENT_SECRET "
                    "if using confidential client) in the backend environment."
                ),
            )

    def build_authorization_url(self, state: str) -> str:
        self._require_credentials()
        verifier, challenge = _pkce_pair()
        redis = get_redis_client()
        redis.setex(f"social_oauth_pkce:{state}", 600, verifier)

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(X_SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"https://twitter.com/i/oauth2/authorize?{urlencode(params)}"

    def exchange_code(self, code: str, *, state: Optional[str] = None) -> OAuthTokenResult:
        self._require_credentials()
        verifier = None
        if state:
            redis = get_redis_client()
            verifier = redis.get(f"social_oauth_pkce:{state}")
            redis.delete(f"social_oauth_pkce:{state}")
        if not verifier:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing PKCE verifier for X OAuth",
            )

        token_data = self._exchange_code(code, verifier)
        access_token = token_data["access_token"]
        expires_in = int(token_data.get("expires_in") or 7200)
        token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        refresh_token = token_data.get("refresh_token")

        me = self._get_me(access_token)
        user = me.get("data") or me
        user_id = str(user.get("id") or "")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="X user id missing",
            )

        username = user.get("username") or ""
        display_name = user.get("name") or username or "X User"
        # Prefer @username for previews; fall back to display name.
        account_name = f"@{username}" if username else display_name

        return OAuthTokenResult(
            accounts=[
                OAuthAccountProfile(
                    platform_account_id=user_id,
                    account_name=account_name,
                    account_type=SocialAccountType.PROFILE,
                    account_picture_url=user.get("profile_image_url"),
                    follower_count=int((user.get("public_metrics") or {}).get("followers_count") or 0),
                    access_token=access_token,
                    refresh_token=refresh_token,
                    token_expires_at=token_expires_at,
                )
            ]
        )

    def refresh_access_token(self, refresh_token: str) -> Optional[dict]:
        """Exchange refresh_token for a new access token (OAuth 2.0)."""
        self._require_credentials()
        url = "https://api.twitter.com/2/oauth2/token"
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        auth = (self.client_id, self.client_secret) if self.client_secret else None
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, data=data, headers=headers, auth=auth)
                if response.status_code >= 400:
                    logger.error("X token refresh failed: %s", response.text[:400])
                    return None
                payload = response.json()
                if not payload.get("access_token"):
                    return None
                return {
                    "access_token": payload["access_token"],
                    "refresh_token": payload.get("refresh_token") or refresh_token,
                    "expires_in": int(payload.get("expires_in") or 7200),
                }
        except httpx.HTTPError as exc:
            logger.error("X token refresh request failed: %s", exc)
            return None

    def sync_account_stats(self, platform_account_id: str, access_token: str) -> dict:
        user = self._get_user(access_token, platform_account_id)
        username = user.get("username") or ""
        display_name = user.get("name") or username
        metrics = user.get("public_metrics") or {}
        followers = int(
            metrics.get("followers_count")
            or metrics.get("followers")
            or 0
        )
        return {
            "account_name": f"@{username}" if username else display_name,
            "account_picture_url": user.get("profile_image_url"),
            "follower_count": max(0, followers),
        }

    def _exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        url = "https://api.twitter.com/2/oauth2/token"
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
            "client_id": self.client_id,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        auth = None
        if self.client_secret:
            auth = (self.client_id, self.client_secret)
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, data=data, headers=headers, auth=auth)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            logger.error("X token exchange failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"X authorization failed: {exc}",
            ) from exc

    def _get_me(self, access_token: str) -> dict[str, Any]:
        return self._x_get(
            "https://api.twitter.com/2/users/me",
            access_token,
            {"user.fields": "profile_image_url,public_metrics,username,name"},
        )

    def _get_user(self, access_token: str, platform_account_id: str) -> dict[str, Any]:
        """Prefer /users/:id (includes public_metrics), fall back to /users/me."""
        user_id = (platform_account_id or "").strip()
        if user_id:
            try:
                payload = self._x_get(
                    f"https://api.twitter.com/2/users/{user_id}",
                    access_token,
                    {
                        "user.fields": "profile_image_url,public_metrics,username,name",
                    },
                )
                user = payload.get("data") or payload
                if user.get("id") or user.get("username"):
                    return user
            except Exception as exc:
                logger.info("X users/%s failed, falling back to me: %s", user_id, exc)
        me = self._get_me(access_token)
        return me.get("data") or me

    def _x_get(
        self,
        url: str,
        access_token: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                url,
                params=params or {},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code == 402:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=(
                        "X API credits are depleted for this app. "
                        "Follower sync needs available X API credits — wait for reset or upgrade the X developer plan."
                    ),
                )
            if response.status_code == 401:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="X token expired — click Reconnect on the account card.",
                )
            if response.status_code >= 400:
                logger.error("X API error %s: %s", response.status_code, response.text[:400])
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"X API error ({response.status_code})",
                )
            return response.json()
