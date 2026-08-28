from __future__ import annotations

import logging
import secrets
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.schemas import LoginRequest, RegisterRequest
from app.core.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.email.service import email_service
from app.users.models import User
from app.workspaces.models import Workspace, WorkspaceMember, WorkspacePlan, WorkspaceRole, SocialLevel
from workers.redis.client import get_redis_client

logger = logging.getLogger(__name__)

PASSWORD_RESET_TTL_SECONDS = 60 * 60
PASSWORD_RESET_PREFIX = "password_reset:"


def get_user_by_email(db: Session, email: str) -> User | None:
    stmt = select(User).where(User.email == email.lower().strip())
    return db.execute(stmt).scalar_one_or_none()


def register_user(db: Session, payload: RegisterRequest) -> tuple[User, Workspace, WorkspaceMember]:
    email = payload.email.lower().strip()
    if get_user_by_email(db, email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        is_active=True,
        is_platform_admin=False,
    )
    db.add(user)
    db.flush()

    workspace_name = payload.workspace_name or (
        f"{payload.full_name}'s Workspace" if payload.full_name else f"{email.split('@')[0]}'s Workspace"
    )
    # Free tier — WorkspacePlan.STARTER maps to the Free catalog entry.
    workspace = Workspace(
        name=workspace_name,
        plan=WorkspacePlan.STARTER,
        owner_user_id=user.id,
        is_active=True,
    )
    db.add(workspace)
    db.flush()

    membership = WorkspaceMember(
        user_id=user.id,
        workspace_id=workspace.id,
        role=WorkspaceRole.OWNER,
        social_level=SocialLevel.ADMIN,
    )
    db.add(membership)
    db.flush()
    db.commit()

    try:
        email_service.send_welcome(
            to=user.email,
            full_name=user.full_name,
            workspace_name=workspace.name,
        )
    except Exception:
        logger.exception("Welcome email failed for %s", user.email)

    return user, workspace, membership


def authenticate_user(db: Session, payload: LoginRequest) -> User:
    user = get_user_by_email(db, payload.email)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
    if not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account uses Google sign-in. Continue with Google.",
        )
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is suspended")
    return user


def issue_token_for_user(user: User) -> str:
    return create_access_token(subject=str(user.id), extra={"is_platform_admin": user.is_platform_admin})


def request_password_reset(db: Session, email: str) -> None:
    """Always succeed from the caller's perspective (no email enumeration)."""
    user = get_user_by_email(db, email)
    if not user or not user.is_active:
        return

    token = secrets.token_urlsafe(32)
    redis = get_redis_client()
    redis.setex(f"{PASSWORD_RESET_PREFIX}{token}", PASSWORD_RESET_TTL_SECONDS, str(user.id))

    reset_url = f"{settings.frontend_url.rstrip('/')}/reset-password?token={token}"
    sent = email_service.send_password_reset(
        to=user.email,
        full_name=user.full_name,
        reset_url=reset_url,
    )
    if not sent:
        logger.warning("Password reset email not sent for %s (Resend misconfigured or failed)", user.email)


def reset_password(db: Session, token: str, new_password: str) -> None:
    redis = get_redis_client()
    key = f"{PASSWORD_RESET_PREFIX}{token.strip()}"
    user_id = redis.get(key)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset link is invalid or has expired",
        )

    try:
        uid = uuid.UUID(str(user_id))
    except ValueError as exc:
        redis.delete(key)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset link is invalid or has expired",
        ) from exc

    user = db.get(User, uid)
    if not user or not user.is_active:
        redis.delete(key)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset link is invalid or has expired",
        )

    user.password_hash = hash_password(new_password)
    db.add(user)
    db.commit()
    redis.delete(key)
