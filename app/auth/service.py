from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.schemas import LoginRequest, RegisterRequest
from app.core.security import create_access_token, hash_password, verify_password
from app.users.models import User
from app.workspaces.models import Workspace, WorkspaceMember, WorkspacePlan, WorkspaceRole, SocialLevel


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

    return user, workspace, membership


def authenticate_user(db: Session, payload: LoginRequest) -> User:
    user = get_user_by_email(db, payload.email)
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is suspended")
    return user


def issue_token_for_user(user: User) -> str:
    return create_access_token(subject=str(user.id), extra={"is_platform_admin": user.is_platform_admin})
