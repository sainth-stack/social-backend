from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.schemas import (
    LoginRequest,
    MeResponse,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    UserOut,
    WorkspaceSummaryOut,
)
from app.auth.service import authenticate_user, issue_token_for_user, register_user
from app.core.database import get_db
from app.users.models import User
from app.workspaces.models import WorkspaceMember

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=RegisterResponse, status_code=201)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> RegisterResponse:
    user, workspace, membership = register_user(db, payload)
    token = issue_token_for_user(user)
    return RegisterResponse(
        access_token=token,
        user=UserOut.model_validate(user),
        workspace=WorkspaceSummaryOut(
            id=workspace.id,
            name=workspace.name,
            plan=workspace.plan.value,
            role=membership.role.value,
            social_level=membership.social_level.value,
        ),
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = authenticate_user(db, payload)
    token = issue_token_for_user(user)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=MeResponse)
def me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> MeResponse:
    stmt = (
        select(WorkspaceMember)
        .where(WorkspaceMember.user_id == current_user.id)
        .join(WorkspaceMember.workspace)
    )
    memberships = db.execute(stmt).scalars().all()
    workspaces = [
        WorkspaceSummaryOut(
            id=m.workspace.id,
            name=m.workspace.name,
            plan=m.workspace.plan.value,
            role=m.role.value,
            social_level=m.social_level.value,
        )
        for m in memberships
    ]
    return MeResponse(user=UserOut.model_validate(current_user), workspaces=workspaces)
