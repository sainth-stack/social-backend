"""Google OAuth 2.0 for user sign-in and sign-up."""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.service import issue_token_for_user
from app.core.config import settings
from app.users.models import User
from app.workspaces.models import Workspace, WorkspaceMember, WorkspacePlan, WorkspaceRole, SocialLevel
from workers.redis.client import get_redis_client

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
        "redirect_uri": settings.google_redirect_uri,
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
                    "redirect_uri": settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_resp.status_code >= 400:
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


def find_or_create_google_user(db: Session, profile: dict[str, Any]) -> User:
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

    user = db.scalars(select(User).where(User.google_id == google_id)).first()
    if not user:
        user = db.scalars(select(User).where(User.email == email)).first()

    if user:
        if not user.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is suspended")
        if not user.google_id:
            user.google_id = google_id
        if full_name and not user.full_name:
            user.full_name = full_name
        db.commit()
        db.refresh(user)
        return user

    user = User(
        email=email,
        password_hash=None,
        google_id=google_id,
        full_name=full_name,
        is_active=True,
        is_platform_admin=False,
    )
    db.add(user)
    db.flush()

    workspace_name = (
        f"{full_name}'s Workspace" if full_name else f"{email.split('@')[0]}'s Workspace"
    )
    workspace = Workspace(
        name=workspace_name,
        plan=WorkspacePlan.STARTER,
        owner_user_id=user.id,
        is_active=True,
    )
    db.add(workspace)
    db.flush()
    db.add(
        WorkspaceMember(
            user_id=user.id,
            workspace_id=workspace.id,
            role=WorkspaceRole.OWNER,
            social_level=SocialLevel.ADMIN,
        )
    )
    db.commit()
    db.refresh(user)
    return user


def complete_google_sign_in(db: Session, code: str, state: str) -> tuple[User, str]:
    verify_google_state(state)
    profile = exchange_google_code(code)
    user = find_or_create_google_user(db, profile)
    token = issue_token_for_user(user)
    return user, token
