from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import false, func, select
from sqlalchemy.orm import Session

from app.admin.schemas import (
    AdminOverviewOut,
    AdminAnalyticsOut,
    AdminUserListItem,
    AdminUserListOut,
    AnalyticsOverviewOut,
    PlanDistributionRow,
    PlanMixEntry,
    PlatformMixRow,
    PostsOverTimePoint,
    PricingPlanLimits,
    PricingPlanOut,
)
from app.plans import service as plans_service
from app.plans.catalog import PLAN_CATALOG, PLAN_ORDER
from app.plans.models import AiUsageEvent
from app.social.models import (
    SocialPlatformPostStatus,
    SocialPost,
    SocialPostPlatform,
    SocialPostStatus,
)
from app.core.security import hash_password
from app.users.models import User
from app.workspaces.models import SocialLevel, Workspace, WorkspaceMember, WorkspacePlan, WorkspaceRole

# Estimated flat display prices used for MRR — Enterprise is custom/negotiated
# and excluded from the estimate (contributes $0 by design, per spec).
_MRR_PLAN_PRICES_USD: dict[str, float] = {
    WorkspacePlan.STARTER.value: 399.0,
    WorkspacePlan.GROWTH.value: 1499.0,
    WorkspacePlan.ENTERPRISE.value: 0.0,
}


def list_users(db: Session, *, search: str | None = None, limit: int = 100, offset: int = 0) -> list[User]:
    stmt = select(User)
    if search:
        like = f"%{search.lower()}%"
        stmt = stmt.where(func.lower(User.email).like(like))
    stmt = stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all())


def count_workspaces_for_users(db: Session, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not user_ids:
        return {}
    stmt = (
        select(WorkspaceMember.user_id, func.count(WorkspaceMember.id))
        .where(WorkspaceMember.user_id.in_(user_ids))
        .group_by(WorkspaceMember.user_id)
    )
    return {row[0]: row[1] for row in db.execute(stmt).all()}


def get_user_or_404(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def update_user(db: Session, user_id: uuid.UUID, data: dict) -> User:
    user = get_user_or_404(db, user_id)
    for field, value in data.items():
        if value is not None and hasattr(user, field):
            setattr(user, field, value)
    db.commit()
    return user


def suspend_user(db: Session, user_id: uuid.UUID) -> User:
    user = get_user_or_404(db, user_id)
    user.is_active = False
    db.commit()
    return user


def reactivate_user(db: Session, user_id: uuid.UUID) -> User:
    user = get_user_or_404(db, user_id)
    user.is_active = True
    db.commit()
    return user


def list_workspaces(db: Session, *, search: str | None = None, limit: int = 100, offset: int = 0):
    stmt = select(Workspace)
    if search:
        like = f"%{search.lower()}%"
        stmt = stmt.where(func.lower(Workspace.name).like(like))
    stmt = stmt.order_by(Workspace.created_at.desc()).limit(limit).offset(offset)
    workspaces = list(db.execute(stmt).scalars().all())

    results = []
    for w in workspaces:
        owner = db.get(User, w.owner_user_id)
        member_count = db.scalar(
            select(func.count()).select_from(WorkspaceMember).where(WorkspaceMember.workspace_id == w.id)
        ) or 0
        results.append((w, owner, member_count))
    return results


def get_workspace_or_404(db: Session, workspace_id: uuid.UUID) -> Workspace:
    workspace = db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return workspace


def set_workspace_plan(db: Session, workspace_id: uuid.UUID, plan: str) -> Workspace:
    workspace = get_workspace_or_404(db, workspace_id)
    try:
        workspace.plan = WorkspacePlan(plan)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown plan: {plan}") from exc
    db.commit()
    return workspace


def set_workspace_status(db: Session, workspace_id: uuid.UUID, is_active: bool) -> Workspace:
    workspace = get_workspace_or_404(db, workspace_id)
    workspace.is_active = is_active
    db.commit()
    return workspace


def analytics_overview(db: Session) -> AnalyticsOverviewOut:
    since_30d = datetime.now(timezone.utc) - timedelta(days=30)

    total_users = db.scalar(select(func.count()).select_from(User)) or 0
    total_workspaces = db.scalar(select(func.count()).select_from(Workspace)) or 0
    active_workspaces = db.scalar(
        select(func.count()).select_from(Workspace).where(Workspace.is_active.is_(True))
    ) or 0

    posts_published_30d = db.scalar(
        select(func.count())
        .select_from(SocialPost)
        .where(SocialPost.status == SocialPostStatus.PUBLISHED, SocialPost.published_at >= since_30d)
    ) or 0

    failed_publishes_30d = db.scalar(
        select(func.count())
        .select_from(SocialPostPlatform)
        .where(
            SocialPostPlatform.status == SocialPlatformPostStatus.FAILED,
            SocialPostPlatform.published_at.is_(None) | (SocialPostPlatform.published_at >= since_30d),
        )
    ) or 0

    def _ai_count(kind: str) -> int:
        return db.scalar(
            select(func.coalesce(func.sum(AiUsageEvent.quantity), 0)).where(
                AiUsageEvent.kind == kind, AiUsageEvent.created_at >= since_30d
            )
        ) or 0

    ai_text_30d = _ai_count("text")
    ai_image_30d = _ai_count("image")
    ai_video_30d = _ai_count("video")

    plan_mix_rows = db.execute(
        select(Workspace.plan, func.count()).group_by(Workspace.plan)
    ).all()
    plan_mix = [PlanMixEntry(plan=p.value, workspace_count=c) for p, c in plan_mix_rows]

    return AnalyticsOverviewOut(
        total_users=total_users,
        total_workspaces=total_workspaces,
        active_workspaces=active_workspaces,
        posts_published_30d=posts_published_30d,
        ai_text_generations_30d=int(ai_text_30d),
        ai_image_generations_30d=int(ai_image_30d),
        ai_video_generations_30d=int(ai_video_30d),
        failed_publishes_30d=failed_publishes_30d,
        plan_mix=plan_mix,
    )


# ── Frontend-facing admin API (overview / analytics / users / pricing) ─────


def _plan_distribution_rows(db: Session) -> list[PlanDistributionRow]:
    rows = db.execute(select(Workspace.plan, func.count()).group_by(Workspace.plan)).all()
    counts = {p.value: c for p, c in rows}
    # Always include every catalog plan (even with 0 workspaces) so the FE chart is stable.
    return [PlanDistributionRow(plan=key, count=counts.get(key, 0)) for key in PLAN_ORDER]


def _ai_usage_sum(db: Session, since: datetime) -> int:
    total = 0
    for kind in ("text", "image", "video"):
        total += int(
            db.scalar(
                select(func.coalesce(func.sum(AiUsageEvent.quantity), 0)).where(
                    AiUsageEvent.kind == kind, AiUsageEvent.created_at >= since
                )
            )
            or 0
        )
    return total


def admin_overview(db: Session) -> AdminOverviewOut:
    now = datetime.now(timezone.utc)
    since_30d = now - timedelta(days=30)

    total_users = db.scalar(select(func.count()).select_from(User)) or 0
    total_workspaces = db.scalar(select(func.count()).select_from(Workspace)) or 0

    new_users_30d = db.scalar(
        select(func.count()).select_from(User).where(User.created_at >= since_30d)
    ) or 0
    users_before_30d = max(total_users - new_users_30d, 0)
    user_growth_30d_pct = (new_users_30d / users_before_30d * 100) if users_before_30d > 0 else (
        100.0 if new_users_30d > 0 else 0.0
    )

    posts_30d = db.scalar(
        select(func.count())
        .select_from(SocialPost)
        .where(SocialPost.status == SocialPostStatus.PUBLISHED, SocialPost.published_at >= since_30d)
    ) or 0

    failed_publishes_30d = db.scalar(
        select(func.count())
        .select_from(SocialPostPlatform)
        .where(
            SocialPostPlatform.status == SocialPlatformPostStatus.FAILED,
            SocialPostPlatform.published_at.is_(None) | (SocialPostPlatform.published_at >= since_30d),
        )
    ) or 0

    ai_usage_30d = _ai_usage_sum(db, since_30d)

    active_plan_rows = db.execute(
        select(Workspace.plan, func.count())
        .where(Workspace.is_active.is_(True))
        .group_by(Workspace.plan)
    ).all()
    mrr_estimate_usd = sum(
        _MRR_PLAN_PRICES_USD.get(plan.value, 0.0) * count for plan, count in active_plan_rows
    )

    return AdminOverviewOut(
        total_users=total_users,
        total_workspaces=total_workspaces,
        posts_30d=posts_30d,
        ai_usage_30d=ai_usage_30d,
        failed_publishes_30d=failed_publishes_30d,
        mrr_estimate_usd=round(mrr_estimate_usd, 2),
        user_growth_30d_pct=round(user_growth_30d_pct, 2),
        plan_distribution=_plan_distribution_rows(db),
    )


def admin_analytics(db: Session) -> AdminAnalyticsOut:
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=29)
    since_30d = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)

    posts_rows = db.execute(
        select(func.date(SocialPost.published_at), func.count())
        .where(SocialPost.status == SocialPostStatus.PUBLISHED, SocialPost.published_at >= since_30d)
        .group_by(func.date(SocialPost.published_at))
    ).all()
    posts_by_date: dict[date, int] = {}
    for day, count in posts_rows:
        day_value = day if isinstance(day, date) else datetime.fromisoformat(str(day)).date()
        posts_by_date[day_value] = count

    posts_over_time = [
        PostsOverTimePoint(date=start_date + timedelta(days=offset), posts=posts_by_date.get(start_date + timedelta(days=offset), 0))
        for offset in range(30)
    ]

    platform_rows = db.execute(
        select(SocialPostPlatform.platform, func.count())
        .where(SocialPostPlatform.status == SocialPlatformPostStatus.PUBLISHED)
        .group_by(SocialPostPlatform.platform)
    ).all()
    platform_mix = [PlatformMixRow(platform=p.value, count=c) for p, c in platform_rows]

    return AdminAnalyticsOut(
        posts_over_time=posts_over_time,
        plan_distribution=_plan_distribution_rows(db),
        platform_mix=platform_mix,
    )


def _primary_workspace_subquery():
    """One row per user: their first-owned (oldest) workspace, if any."""
    rn = (
        func.row_number()
        .over(partition_by=Workspace.owner_user_id, order_by=Workspace.created_at.asc())
        .label("rn")
    )
    ranked = select(
        Workspace.id.label("workspace_id"),
        Workspace.owner_user_id.label("owner_user_id"),
        Workspace.name.label("workspace_name"),
        Workspace.plan.label("workspace_plan"),
        rn,
    ).subquery()
    return (
        select(
            ranked.c.workspace_id,
            ranked.c.owner_user_id,
            ranked.c.workspace_name,
            ranked.c.workspace_plan,
        )
        .where(ranked.c.rn == 1)
        .subquery()
    )


def _to_admin_user_list_item(user: User, workspace_id, workspace_name, workspace_plan, social_level) -> AdminUserListItem:
    return AdminUserListItem(
        id=user.id,
        email=user.email,
        name=user.full_name or user.email,
        workspace_id=workspace_id,
        workspace_name=workspace_name or "",
        plan=workspace_plan.value if workspace_plan else "starter",
        status="active" if user.is_active else "suspended",
        social_permission_level=social_level.value if social_level else "viewer",
        is_platform_admin=user.is_platform_admin,
        created_at=user.created_at,
    )


def list_admin_users(
    db: Session,
    *,
    search: str | None = None,
    plan: str | None = None,
    status_filter: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> AdminUserListOut:
    page = max(page, 1)
    page_size = max(1, min(page_size, 200))

    primary_ws = _primary_workspace_subquery()

    filters = []
    if search:
        like = f"%{search.strip().lower()}%"
        filters.append(
            func.lower(User.email).like(like)
            | func.lower(func.coalesce(User.full_name, "")).like(like)
            | func.lower(func.coalesce(primary_ws.c.workspace_name, "")).like(like)
        )
    if plan:
        try:
            filters.append(primary_ws.c.workspace_plan == WorkspacePlan(plan))
        except ValueError:
            filters.append(false())
    if status_filter in ("active", "suspended"):
        filters.append(User.is_active.is_(status_filter == "active"))

    count_stmt = select(func.count()).select_from(User).outerjoin(
        primary_ws, primary_ws.c.owner_user_id == User.id
    )
    for f in filters:
        count_stmt = count_stmt.where(f)
    total = db.scalar(count_stmt) or 0

    stmt = (
        select(
            User,
            primary_ws.c.workspace_id,
            primary_ws.c.workspace_name,
            primary_ws.c.workspace_plan,
            WorkspaceMember.social_level,
        )
        .outerjoin(primary_ws, primary_ws.c.owner_user_id == User.id)
        .outerjoin(
            WorkspaceMember,
            (WorkspaceMember.user_id == User.id) & (WorkspaceMember.workspace_id == primary_ws.c.workspace_id),
        )
    )
    for f in filters:
        stmt = stmt.where(f)
    stmt = stmt.order_by(User.created_at.desc()).limit(page_size).offset((page - 1) * page_size)

    rows = db.execute(stmt).all()
    items = [
        _to_admin_user_list_item(user, workspace_id, workspace_name, workspace_plan, social_level)
        for user, workspace_id, workspace_name, workspace_plan, social_level in rows
    ]

    total_pages = max(1, (total + page_size - 1) // page_size)
    return AdminUserListOut(items=items, total=total, page=page, page_size=page_size, total_pages=total_pages)


def _get_primary_workspace_for_user(db: Session, user_id: uuid.UUID) -> Workspace | None:
    stmt = (
        select(Workspace)
        .where(Workspace.owner_user_id == user_id)
        .order_by(Workspace.created_at.asc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def _get_membership(db: Session, user_id: uuid.UUID, workspace_id: uuid.UUID) -> WorkspaceMember | None:
    stmt = select(WorkspaceMember).where(
        WorkspaceMember.user_id == user_id, WorkspaceMember.workspace_id == workspace_id
    )
    return db.execute(stmt).scalar_one_or_none()


def _admin_user_item_for(db: Session, user: User) -> AdminUserListItem:
    workspace = _get_primary_workspace_for_user(db, user.id)
    membership = _get_membership(db, user.id, workspace.id) if workspace else None
    return _to_admin_user_list_item(
        user,
        workspace.id if workspace else None,
        workspace.name if workspace else None,
        workspace.plan if workspace else None,
        membership.social_level if membership else None,
    )


def create_admin_user(
    db: Session,
    *,
    email: str,
    password: str,
    full_name: str | None = None,
    workspace_name: str | None = None,
    plan: str = "starter",
    is_platform_admin: bool = False,
) -> AdminUserListItem:
    """Create a user with an owned workspace. Returns the admin list row (no JWT)."""
    from app.auth.service import get_user_by_email

    normalized_email = email.lower().strip()
    if get_user_by_email(db, normalized_email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    try:
        workspace_plan = WorkspacePlan(plan)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown plan: {plan}") from exc

    user = User(
        email=normalized_email,
        password_hash=hash_password(password),
        full_name=full_name.strip() if full_name else None,
        is_active=True,
        is_platform_admin=is_platform_admin,
    )
    db.add(user)
    db.flush()

    resolved_workspace_name = workspace_name or (
        f"{full_name}'s Workspace" if full_name else f"{normalized_email.split('@')[0]}'s Workspace"
    )
    workspace = Workspace(
        name=resolved_workspace_name,
        plan=workspace_plan,
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
    return _admin_user_item_for(db, user)


def set_admin_user_suspended(db: Session, user_id: uuid.UUID, suspended: bool | None) -> AdminUserListItem:
    user = get_user_or_404(db, user_id)
    user.is_active = not suspended if suspended is not None else not user.is_active
    db.commit()
    return _admin_user_item_for(db, user)


def set_admin_user_plan(db: Session, user_id: uuid.UUID, plan: str) -> AdminUserListItem:
    user = get_user_or_404(db, user_id)
    workspace = _get_primary_workspace_for_user(db, user_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User has no workspace to update")
    try:
        workspace.plan = WorkspacePlan(plan)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown plan: {plan}") from exc
    db.commit()
    return _admin_user_item_for(db, user)


# ── Pricing (frontend camelCase shape) ──────────────────────────────────────

_LIMIT_FIELD_MAP: dict[str, str] = {
    "accounts": "connected_accounts",
    "posts_per_month": "posts_per_month",
    "ai_text_generations": "ai_text_per_month",
    "ai_image_generations": "ai_images_per_month",
    "ai_video_generations": "ai_videos_per_month",
    "templates": "templates",
    "brand_voice": "brand_voice",
    "approval_workflow": "approval_workflow",
}


def _none_if_unlimited(value: int) -> int | None:
    """Catalog limits use -1 for "unlimited"; the FE contract uses `null`."""
    return None if value is None or value < 0 else value


def _unlimited_if_none(value: int | None) -> int:
    """Inverse of `_none_if_unlimited` — FE `null` (unlimited) -> catalog -1."""
    return -1 if value is None else value


def _plan_out_to_pricing_plan(plan_out) -> PricingPlanOut:
    limits = plan_out.limits
    return PricingPlanOut(
        id=plan_out.key,
        name=plan_out.name,
        tagline=plan_out.description,
        price_monthly_usd=plan_out.monthly_price_usd,
        price_annual_usd=plan_out.annual_price_usd,
        is_custom=plan_out.monthly_price_usd is None,
        recommended=plan_out.key == WorkspacePlan.GROWTH.value,
        limits=PricingPlanLimits(
            accounts=_none_if_unlimited(limits.connected_accounts),
            posts_per_month=_none_if_unlimited(limits.posts_per_month),
            ai_text_generations=_none_if_unlimited(limits.ai_text_per_month),
            ai_image_generations=_none_if_unlimited(limits.ai_images_per_month),
            ai_video_generations=_none_if_unlimited(limits.ai_videos_per_month),
            templates=_none_if_unlimited(limits.templates),
            brand_voice=limits.brand_voice,
            approval_workflow=limits.approval_workflow,
        ),
    )


def list_pricing_plans(db: Session) -> list[PricingPlanOut]:
    return [_plan_out_to_pricing_plan(plans_service.to_plan_out(db, key)) for key in PLAN_CATALOG]


def update_pricing_plan(db: Session, plan_key: str, payload) -> PricingPlanOut:
    if plan_key not in PLAN_CATALOG:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown plan: {plan_key}")

    override_data: dict = {}
    if payload.tagline is not None:
        # Description/tagline isn't part of PlanOverride (display-only in the catalog);
        # nothing to persist here today, kept for forward-compatibility with the FE payload.
        pass
    if payload.price_monthly_usd is not None:
        override_data["monthly_price_usd"] = payload.price_monthly_usd
    if payload.price_annual_usd is not None:
        override_data["annual_price_usd"] = payload.price_annual_usd
    if payload.limits is not None:
        limits_data = payload.limits.model_dump(exclude_unset=True, by_alias=False)
        for fe_field, value in limits_data.items():
            db_field = _LIMIT_FIELD_MAP.get(fe_field)
            if not db_field:
                continue
            # FE numeric limit fields are `number | null` (null == unlimited); booleans pass through as-is.
            is_bool_field = fe_field in ("brand_voice", "approval_workflow")
            override_data[db_field] = value if is_bool_field else _unlimited_if_none(value)

    if override_data:
        plans_service.upsert_override(db, plan_key, override_data)
        db.commit()

    return _plan_out_to_pricing_plan(plans_service.to_plan_out(db, plan_key))
