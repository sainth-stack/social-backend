from __future__ import annotations

from urllib.parse import quote

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pymongo.database import Database

from app.auth.deps import get_current_user
from app.auth.google import (
    build_google_auth_url,
    complete_google_sign_in,
    google_oauth_configured,
)
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
from app.core.mongo_utils import public_doc
from app.core.rate_limit import enforce_rate_limit

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=RegisterResponse, status_code=201)
def register(
    payload: RegisterRequest,
    request: Request,
    db: Database = Depends(get_db),
) -> RegisterResponse:
    enforce_rate_limit(request, key_prefix="auth_register", limit=10, window_seconds=3600)
    user, workspace, membership = register_user(db, payload)
    token = issue_token_for_user(user)
    return RegisterResponse(
        access_token=token,
        user=UserOut.model_validate(public_doc(user)),
        workspace=WorkspaceSummaryOut(
            id=workspace["id"],
            name=workspace["name"],
            plan=workspace["plan"],
            role=membership["role"],
            social_level=membership["social_level"],
        ),
    )


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    db: Database = Depends(get_db),
) -> TokenResponse:
    enforce_rate_limit(request, key_prefix="auth_login", limit=30, window_seconds=900)
    user = authenticate_user(db, payload)
    token = issue_token_for_user(user)
    return TokenResponse(access_token=token)


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: Database = Depends(get_db),
) -> ForgotPasswordResponse:
    enforce_rate_limit(request, key_prefix="auth_forgot", limit=10, window_seconds=3600)
    request_password_reset(db, payload.email)
    return ForgotPasswordResponse()


@router.post("/reset-password", response_model=ResetPasswordResponse)
def reset_password_endpoint(
    payload: ResetPasswordRequest,
    request: Request,
    db: Database = Depends(get_db),
) -> ResetPasswordResponse:
    enforce_rate_limit(request, key_prefix="auth_reset", limit=20, window_seconds=3600)
    reset_password(db, payload.token, payload.password)
    return ResetPasswordResponse()


@router.get("/google/status")
def google_auth_status() -> dict[str, bool]:
    return {"enabled": google_oauth_configured()}


@router.get("/google/url")
def google_auth_url() -> dict[str, str]:
    return {"url": build_google_auth_url()}


@router.get("/google/callback")
def google_auth_callback(
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    error_description: Optional[str] = Query(default=None),
    db: Database = Depends(get_db),
) -> RedirectResponse:
    frontend = settings.frontend_url.rstrip("/")
    if error:
        msg = error_description or error
        return RedirectResponse(url=f"{frontend}/auth/google/callback?error={quote(msg)}")
    if not code or not state:
        return RedirectResponse(
            url=f"{frontend}/auth/google/callback?error={quote('Google sign-in was cancelled')}"
        )
    try:
        _, token = complete_google_sign_in(db, code, state)
        return RedirectResponse(url=f"{frontend}/auth/google/callback?token={token}")
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Google sign-in failed"
        return RedirectResponse(url=f"{frontend}/auth/google/callback?error={quote(detail)}")


@router.get("/me", response_model=MeResponse)
def me(current_user: dict = Depends(get_current_user), db: Database = Depends(get_db)) -> MeResponse:
    memberships = list(db["workspace_members"].find({"user_id": current_user["id"]}))
    workspace_ids = [m["workspace_id"] for m in memberships]
    workspaces_map = {
        w["id"]: w
        for w in db["workspaces"].find({"id": {"$in": workspace_ids}})
    }
    workspace_summaries = [
        WorkspaceSummaryOut(
            id=m["workspace_id"],
            name=workspaces_map.get(m["workspace_id"], {}).get("name", ""),
            plan=workspaces_map.get(m["workspace_id"], {}).get("plan", "starter"),
            role=m["role"],
            social_level=m["social_level"],
        )
        for m in memberships
        if m["workspace_id"] in workspaces_map
    ]
    return MeResponse(user=UserOut.model_validate(public_doc(current_user)), workspaces=workspace_summaries)
