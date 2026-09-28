from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException, status
from pymongo.database import Database

from app.auth.schemas import LoginRequest, RegisterRequest
from app.core.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.core.mongo_utils import new_id, utcnow
from app.email.service import email_service
from app.workspaces.models import SocialLevel, WorkspacePlan, WorkspaceRole
from workers.redis.client import get_redis_client

logger = logging.getLogger(__name__)

PASSWORD_RESET_TTL_SECONDS = 60 * 60
PASSWORD_RESET_PREFIX = "password_reset:"


def get_user_by_email(db: Database, email: str) -> dict | None:
    return db["users"].find_one({"email": email.lower().strip()})


def register_user(db: Database, payload: RegisterRequest) -> tuple[dict, dict, dict]:
    email = payload.email.lower().strip()
    if get_user_by_email(db, email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    now = utcnow()
    user_id = new_id()
    user = {
        "id": user_id,
        "email": email,
        "password_hash": hash_password(payload.password),
        "full_name": payload.full_name,
        "is_active": True,
        "is_platform_admin": False,
        "google_id": None,
        "created_at": now,
        "updated_at": now,
    }
    db["users"].insert_one(user)

    workspace_name = payload.workspace_name or (
        f"{payload.full_name}'s Workspace" if payload.full_name else f"{email.split('@')[0]}'s Workspace"
    )
    ws_id = new_id()
    workspace = {
        "id": ws_id,
        "name": workspace_name,
        "plan": WorkspacePlan.STARTER.value,
        "owner_user_id": user_id,
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    db["workspaces"].insert_one(workspace)

    mem_id = new_id()
    membership = {
        "id": mem_id,
        "user_id": user_id,
        "workspace_id": ws_id,
        "role": WorkspaceRole.OWNER.value,
        "social_level": SocialLevel.ADMIN.value,
        "created_at": now,
    }
    db["workspace_members"].insert_one(membership)

    try:
        email_service.send_welcome(
            to=user["email"],
            full_name=user.get("full_name"),
            workspace_name=workspace["name"],
        )
    except Exception:
        logger.exception("Welcome email failed for %s", user["email"])

    return user, workspace, membership


def authenticate_user(db: Database, payload: LoginRequest) -> dict:
    user = get_user_by_email(db, payload.email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    if not user.get("password_hash"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account uses Google sign-in. Continue with Google.",
        )
    if not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    if not user.get("is_active"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is suspended")
    return user


def issue_token_for_user(user: dict) -> str:
    return create_access_token(
        subject=str(user["id"]), extra={"is_platform_admin": user.get("is_platform_admin", False)}
    )


def request_password_reset(db: Database, email: str) -> None:
    """Always succeed from the caller's perspective (no email enumeration)."""
    user = get_user_by_email(db, email)
    if not user or not user.get("is_active"):
        return

    token = secrets.token_urlsafe(32)
    redis = get_redis_client()
    redis.setex(f"{PASSWORD_RESET_PREFIX}{token}", PASSWORD_RESET_TTL_SECONDS, str(user["id"]))

    reset_url = f"{settings.frontend_url.rstrip('/')}/reset-password?token={token}"
    sent = email_service.send_password_reset(
        to=user["email"],
        full_name=user.get("full_name"),
        reset_url=reset_url,
    )
    if not sent:
        logger.warning(
            "Password reset email not sent for %s (Resend misconfigured or failed)", user["email"]
        )


def reset_password(db: Database, token: str, new_password: str) -> None:
    redis = get_redis_client()
    key = f"{PASSWORD_RESET_PREFIX}{token.strip()}"
    user_id = redis.get(key)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset link is invalid or has expired",
        )

    user = db["users"].find_one({"id": str(user_id)})
    if not user or not user.get("is_active"):
        redis.delete(key)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset link is invalid or has expired",
        )

    db["users"].update_one(
        {"id": str(user_id)},
        {"$set": {"password_hash": hash_password(new_password), "updated_at": utcnow()}},
    )
    redis.delete(key)
