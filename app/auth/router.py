from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.google import build_google_auth_url, complete_google_sign_in
from app.auth.schemas import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    MeResponse,
    RegisterRequest,
    RegisterResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    TokenResponse,
    UserOut,
    WorkspaceSummaryOut,
)
from app.auth.service import (
    authenticate_user,
    issue_token_for_user,
    register_user,
    request_password_reset,
    reset_password,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.users.models import User
from app.workspaces.models import WorkspaceMember

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=RegisterResponse, status_code=201)
def register(
    payload: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> RegisterResponse:
    enforce_rate_limit(request, key_prefix="auth_register", limit=10, window_seconds=3600)
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
def login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TokenResponse:
    enforce_rate_limit(request, key_prefix="auth_login", limit=30, window_seconds=900)
    user = authenticate_user(db, payload)
    token = issue_token_for_user(user)
    return TokenResponse(access_token=token)


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ForgotPasswordResponse:
    enforce_rate_limit(request, key_prefix="auth_forgot", limit=10, window_seconds=3600)
    request_password_reset(db, payload.email)
    return ForgotPasswordResponse()


@router.post("/reset-password", response_model=ResetPasswordResponse)
def reset_password_endpoint(
    payload: ResetPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ResetPasswordResponse:
    enforce_rate_limit(request, key_prefix="auth_reset", limit=20, window_seconds=3600)
    reset_password(db, payload.token, payload.password)
    return ResetPasswordResponse()


@router.get("/google/url")
def google_auth_url() -> dict[str, str]:
    return {"url": build_google_auth_url()}


@router.get("/google/callback")
def google_auth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    frontend = settings.frontend_url.rstrip("/")
    try:
        _, token = complete_google_sign_in(db, code, state)
        return RedirectResponse(url=f"{frontend}/auth/google/callback?token={token}")
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Google sign-in failed"
        return RedirectResponse(url=f"{frontend}/auth/google/callback?error={quote(detail)}")


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
