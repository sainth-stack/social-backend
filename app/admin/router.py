from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.admin import service
from app.admin.schemas import (
    AdminAnalyticsOut,
    AdminOverviewOut,
    AdminUserListItem,
    AdminUserListOut,
    AdminUserOut,
    AdminUserPlanUpdateRequest,
    AdminUserSuspendRequest,
    AdminUserUpdateRequest,
    AdminWorkspaceOut,
    AdminWorkspacePlanUpdateRequest,
    AdminWorkspaceStatusUpdateRequest,
    AnalyticsOverviewOut,
    PricingPlanOut,
    PricingPlanUpdateRequest,
)
from app.auth.deps import require_platform_admin
from app.core.database import get_db
from app.plans import service as plans_service
from app.plans.schemas import PlanOut, PlanUpdateRequest
from app.users.models import User

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_platform_admin)],
)


# ── Frontend dashboard API ──────────────────────────────────────────────────
# Shapes here mirror `social-media/frontend/src/types/admin.ts` exactly
# (camelCase over the wire) — see app/admin/schemas.py for the alias setup.

@router.get("/overview", response_model=AdminOverviewOut)
def get_admin_overview(db: Session = Depends(get_db)) -> AdminOverviewOut:
    return service.admin_overview(db)


@router.get("/analytics", response_model=AdminAnalyticsOut)
def get_admin_analytics(db: Session = Depends(get_db)) -> AdminAnalyticsOut:
    return service.admin_analytics(db)


@router.get("/users", response_model=AdminUserListOut)
def list_admin_users(
    search: Optional[str] = Query(default=None),
    plan: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=200),  # noqa: N803 — mirrors FE query param casing
    db: Session = Depends(get_db),
) -> AdminUserListOut:
    return service.list_admin_users(
        db, search=search, plan=plan, status_filter=status, page=page, page_size=pageSize
    )


@router.post("/users/{user_id}/suspend", response_model=AdminUserListItem)
def suspend_admin_user(
    user_id: uuid.UUID, payload: AdminUserSuspendRequest | None = None, db: Session = Depends(get_db)
) -> AdminUserListItem:
    suspended = payload.suspended if payload is not None else None
    return service.set_admin_user_suspended(db, user_id, suspended)


@router.post("/users/{user_id}/plan", response_model=AdminUserListItem)
def change_admin_user_plan(
    user_id: uuid.UUID, payload: AdminUserPlanUpdateRequest, db: Session = Depends(get_db)
) -> AdminUserListItem:
    return service.set_admin_user_plan(db, user_id, payload.plan)


@router.get("/pricing", response_model=list[PricingPlanOut])
def list_admin_pricing(db: Session = Depends(get_db)) -> list[PricingPlanOut]:
    return service.list_pricing_plans(db)


@router.put("/pricing/{plan_key}", response_model=PricingPlanOut)
def update_admin_pricing(
    plan_key: str, payload: PricingPlanUpdateRequest, db: Session = Depends(get_db)
) -> PricingPlanOut:
    return service.update_pricing_plan(db, plan_key, payload)


# ── Legacy admin CRUD (by-id lookups, not used by the current dashboard) ───

@router.get("/users/{user_id}", response_model=AdminUserOut)
def get_user(user_id: uuid.UUID, db: Session = Depends(get_db)) -> AdminUserOut:
    user = service.get_user_or_404(db, user_id)
    counts = service.count_workspaces_for_users(db, [user.id])
    return AdminUserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        is_platform_admin=user.is_platform_admin,
        created_at=user.created_at,
        workspace_count=counts.get(user.id, 0),
    )


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user(
    user_id: uuid.UUID, payload: AdminUserUpdateRequest, db: Session = Depends(get_db)
) -> AdminUserOut:
    user = service.update_user(db, user_id, payload.model_dump(exclude_unset=True))
    counts = service.count_workspaces_for_users(db, [user.id])
    return AdminUserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        is_platform_admin=user.is_platform_admin,
        created_at=user.created_at,
        workspace_count=counts.get(user.id, 0),
    )


# ── Workspaces ───────────────────────────────────────────────────────────────

@router.get("/workspaces", response_model=list[AdminWorkspaceOut])
def list_workspaces(
    search: Optional[str] = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[AdminWorkspaceOut]:
    rows = service.list_workspaces(db, search=search, limit=limit, offset=offset)
    return [
        AdminWorkspaceOut(
            id=w.id,
            name=w.name,
            plan=w.plan.value,
            is_active=w.is_active,
            owner_email=owner.email if owner else "",
            member_count=member_count,
            created_at=w.created_at,
        )
        for w, owner, member_count in rows
    ]


@router.patch("/workspaces/{workspace_id}/plan", response_model=AdminWorkspaceOut)
def set_workspace_plan(
    workspace_id: uuid.UUID, payload: AdminWorkspacePlanUpdateRequest, db: Session = Depends(get_db)
) -> AdminWorkspaceOut:
    workspace = service.set_workspace_plan(db, workspace_id, payload.plan)
    owner = db.get(User, workspace.owner_user_id)
    return AdminWorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        plan=workspace.plan.value,
        is_active=workspace.is_active,
        owner_email=owner.email if owner else "",
        member_count=0,
        created_at=workspace.created_at,
    )


@router.patch("/workspaces/{workspace_id}/status", response_model=AdminWorkspaceOut)
def set_workspace_status(
    workspace_id: uuid.UUID, payload: AdminWorkspaceStatusUpdateRequest, db: Session = Depends(get_db)
) -> AdminWorkspaceOut:
    workspace = service.set_workspace_status(db, workspace_id, payload.is_active)
    owner = db.get(User, workspace.owner_user_id)
    return AdminWorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        plan=workspace.plan.value,
        is_active=workspace.is_active,
        owner_email=owner.email if owner else "",
        member_count=0,
        created_at=workspace.created_at,
    )


# ── Pricing / plans (legacy — hardcoded plan keys, snake_case) ─────────────

@router.get("/plans", response_model=list[PlanOut])
def get_plans(db: Session = Depends(get_db)) -> list[PlanOut]:
    return plans_service.list_effective_plans(db)


@router.put("/plans/{plan_key}", response_model=PlanOut)
def update_plan(plan_key: str, payload: PlanUpdateRequest, db: Session = Depends(get_db)) -> PlanOut:
    plans_service.upsert_override(db, plan_key, payload.model_dump(exclude_unset=True))
    db.commit()
    return plans_service.to_plan_out(db, plan_key)


@router.delete("/plans/{plan_key}/override", response_model=PlanOut)
def clear_plan_override(plan_key: str, db: Session = Depends(get_db)) -> PlanOut:
    override = plans_service.get_override(db, plan_key)
    if override:
        db.delete(override)
        db.commit()
    return plans_service.to_plan_out(db, plan_key)


# ── Analytics ────────────────────────────────────────────────────────────────

@router.get("/analytics/overview", response_model=AnalyticsOverviewOut)
def analytics_overview(db: Session = Depends(get_db)) -> AnalyticsOverviewOut:
    return service.analytics_overview(db)
