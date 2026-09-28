"""Google OAuth 2.0 for user sign-in and sign-up."""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status
from pymongo.database import Database

from app.auth.service import issue_token_for_user
from app.core.config import settings
from app.core.mongo_utils import new_id, utcnow
from app.workspaces.models import SocialLevel, WorkspacePlan, WorkspaceRole
from workers.redis.client import get_redis_client

logger = logging.getLogger(__name__)

GOOGLE_AUTH_STATE_TTL = 600
GOOGLE_AUTH_STATE_PREFIX = "google_auth_state:"


def google_oauth_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def build_google_auth_url() -> str:
    if not google_oauth_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google sign-in is not configured on this server",
        )
    state = secrets.token_urlsafe(24)
    redis = get_redis_client()
    redis.setex(f"{GOOGLE_AUTH_STATE_PREFIX}{state}", GOOGLE_AUTH_STATE_TTL, "1")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.resolved_google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


def verify_google_state(state: str) -> None:
    redis = get_redis_client()
    key = f"{GOOGLE_AUTH_STATE_PREFIX}{state}"
    if not redis.get(key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired Google sign-in session",
        )
    redis.delete(key)


def exchange_google_code(code: str) -> dict[str, Any]:
    if not google_oauth_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google sign-in is not configured",
        )
    try:
        with httpx.Client(timeout=30.0) as client:
            token_resp = client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": settings.resolved_google_redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_resp.status_code >= 400:
                try:
                    err_body = token_resp.json()
                    err_code = err_body.get("error", "")
                    err_desc = err_body.get("error_description", "")
                except Exception:
                    err_code = ""
                    err_desc = token_resp.text[:200]
                logger.warning(
                    "Google token exchange failed: %s %s (redirect_uri=%s)",
                    err_code,
                    err_desc,
                    settings.resolved_google_redirect_uri,
                )
                if err_code == "redirect_uri_mismatch":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            "Google redirect URI mismatch. Add this exact URL under "
                            "Google Cloud Console → APIs & Services → Credentials → "
                            "OAuth client → Authorized redirect URIs: "
                            f"{settings.resolved_google_redirect_uri}"
                        ),
                    )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Google sign-in failed — try again",
                )
            tokens = token_resp.json()
            access_token = tokens.get("access_token")
            if not access_token:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Google did not return an access token",
                )
            user_resp = client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_resp.status_code >= 400:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Could not load Google profile",
                )
            return user_resp.json()
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google sign-in service unavailable",
        ) from exc


def find_or_create_google_user(db: Database, profile: dict[str, Any]) -> dict:
    google_id = str(profile.get("sub") or "").strip()
    email = str(profile.get("email") or "").lower().strip()
    full_name = (profile.get("name") or profile.get("given_name") or "").strip() or None

    if not google_id or not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google profile missing required fields",
        )
    if not profile.get("email_verified", True):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google email is not verified",
        )

    user = db["users"].find_one({"google_id": google_id})
    if not user:
        user = db["users"].find_one({"email": email})

    if user:
        if not user.get("is_active"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is suspended")
        updates: dict = {}
        if not user.get("google_id"):
            updates["google_id"] = google_id
        if full_name and not user.get("full_name"):
            updates["full_name"] = full_name
        if updates:
            updates["updated_at"] = utcnow()
            db["users"].update_one({"id": user["id"]}, {"$set": updates})
            user = db["users"].find_one({"id": user["id"]})
        return user

    now = utcnow()
    user_id = new_id()
    user = {
        "id": user_id,
        "email": email,
        "password_hash": None,
        "google_id": google_id,
        "full_name": full_name,
        "is_active": True,
        "is_platform_admin": False,
        "created_at": now,
        "updated_at": now,
    }
    db["users"].insert_one(user)

    workspace_name = (
        f"{full_name}'s Workspace" if full_name else f"{email.split('@')[0]}'s Workspace"
    )
    ws_id = new_id()
    db["workspaces"].insert_one(
        {
            "id": ws_id,
            "name": workspace_name,
            "plan": WorkspacePlan.STARTER.value,
            "owner_user_id": user_id,
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
    )
    db["workspace_members"].insert_one(
        {
            "id": new_id(),
            "user_id": user_id,
            "workspace_id": ws_id,
            "role": WorkspaceRole.OWNER.value,
            "social_level": SocialLevel.ADMIN.value,
            "created_at": now,
        }
    )
    return user


def complete_google_sign_in(db: Database, code: str, state: str) -> tuple[dict, str]:
    verify_google_state(state)
    profile = exchange_google_code(code)
    user = find_or_create_google_user(db, profile)
    token = issue_token_for_user(user)
    return user, token
