from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, status
from pymongo.database import Database

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
from app.core.mongo_utils import new_id, utcnow
from app.core.security import hash_password
from app.plans import service as plans_service
from app.plans.catalog import PLAN_CATALOG, PLAN_ORDER
from app.social.models import SocialPlatformPostStatus, SocialPostStatus
from app.workspaces.models import SocialLevel, WorkspacePlan, WorkspaceRole

# Estimated flat display prices used for MRR
_MRR_PLAN_PRICES_USD: dict[str, float] = {
    WorkspacePlan.STARTER.value: 399.0,
    WorkspacePlan.GROWTH.value: 1499.0,
    WorkspacePlan.ENTERPRISE.value: 0.0,
}


def list_users(
    db: Database, *, search: str | None = None, limit: int = 100, offset: int = 0
) -> list[dict]:
    query: dict = {}
    if search:
        like = search.lower()
        query["$or"] = [
            {"email": {"$regex": like, "$options": "i"}},
            {"full_name": {"$regex": like, "$options": "i"}},
        ]
    return list(
        db["users"].find(query).sort("created_at", -1).skip(offset).limit(limit)
    )


def count_workspaces_for_users(db: Database, user_ids: list[str]) -> dict[str, int]:
    if not user_ids:
        return {}
    pipeline = [
        {"$match": {"user_id": {"$in": user_ids}}},
        {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
    ]
    return {row["_id"]: row["count"] for row in db["workspace_members"].aggregate(pipeline)}


def get_user_or_404(db: Database, user_id: str) -> dict:
    user = db["users"].find_one({"id": str(user_id)})
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def update_user(db: Database, user_id: str, data: dict) -> dict:
    user = get_user_or_404(db, user_id)
    allowed = {"full_name", "is_active", "is_platform_admin"}
    updates = {k: v for k, v in data.items() if k in allowed and v is not None}
    if updates:
        updates["updated_at"] = utcnow()
        db["users"].update_one({"id": str(user_id)}, {"$set": updates})
    return db["users"].find_one({"id": str(user_id)})


def suspend_user(db: Database, user_id: str) -> dict:
    user = get_user_or_404(db, user_id)
    db["users"].update_one(
        {"id": str(user_id)}, {"$set": {"is_active": False, "updated_at": utcnow()}}
    )
    return db["users"].find_one({"id": str(user_id)})


def reactivate_user(db: Database, user_id: str) -> dict:
    user = get_user_or_404(db, user_id)
    db["users"].update_one(
        {"id": str(user_id)}, {"$set": {"is_active": True, "updated_at": utcnow()}}
    )
    return db["users"].find_one({"id": str(user_id)})


def list_workspaces(
    db: Database, *, search: str | None = None, limit: int = 100, offset: int = 0
):
    query: dict = {}
    if search:
        query["name"] = {"$regex": search.lower(), "$options": "i"}
    workspaces = list(
        db["workspaces"].find(query).sort("created_at", -1).skip(offset).limit(limit)
    )
    results = []
    for w in workspaces:
        owner = db["users"].find_one({"id": w.get("owner_user_id")})
        member_count = db["workspace_members"].count_documents({"workspace_id": w["id"]})
        results.append((w, owner, member_count))
    return results


def get_workspace_or_404(db: Database, workspace_id: str) -> dict:
    workspace = db["workspaces"].find_one({"id": str(workspace_id)})
    if not workspace:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return workspace


def set_workspace_plan(db: Database, workspace_id: str, plan: str) -> dict:
    workspace = get_workspace_or_404(db, workspace_id)
    try:
        plan_val = WorkspacePlan(plan).value
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown plan: {plan}"
        ) from exc
    db["workspaces"].update_one(
        {"id": str(workspace_id)}, {"$set": {"plan": plan_val, "updated_at": utcnow()}}
    )
    return db["workspaces"].find_one({"id": str(workspace_id)})


def set_workspace_status(db: Database, workspace_id: str, is_active: bool) -> dict:
    get_workspace_or_404(db, workspace_id)
    db["workspaces"].update_one(
        {"id": str(workspace_id)}, {"$set": {"is_active": is_active, "updated_at": utcnow()}}
    )
    return db["workspaces"].find_one({"id": str(workspace_id)})


def analytics_overview(db: Database) -> AnalyticsOverviewOut:
    since_30d = datetime.now(timezone.utc) - timedelta(days=30)

    total_users = db["users"].count_documents({})
    total_workspaces = db["workspaces"].count_documents({})
    active_workspaces = db["workspaces"].count_documents({"is_active": True})

    posts_published_30d = db["social_posts"].count_documents(
        {"status": SocialPostStatus.PUBLISHED.value, "published_at": {"$gte": since_30d}}
    )

    failed_publishes_30d = db["social_post_platforms"].count_documents(
        {"status": SocialPlatformPostStatus.FAILED.value}
    )

    def _ai_count(kind: str) -> int:
        pipeline = [
            {"$match": {"kind": kind, "created_at": {"$gte": since_30d}}},
            {"$group": {"_id": None, "total": {"$sum": "$quantity"}}},
        ]
        result = list(db["plans_ai_usage"].aggregate(pipeline))
        return int(result[0]["total"]) if result else 0

    ai_text_30d = _ai_count("text")
    ai_image_30d = _ai_count("image")
    ai_video_30d = _ai_count("video")

    # Plan mix
    pipeline = [{"$group": {"_id": "$plan", "count": {"$sum": 1}}}]
    plan_mix = [
        PlanMixEntry(plan=row["_id"], workspace_count=row["count"])
        for row in db["workspaces"].aggregate(pipeline)
    ]

    return AnalyticsOverviewOut(
        total_users=total_users,
        total_workspaces=total_workspaces,
        active_workspaces=active_workspaces,
        posts_published_30d=posts_published_30d,
        ai_text_generations_30d=ai_text_30d,
        ai_image_generations_30d=ai_image_30d,
        ai_video_generations_30d=int(ai_video_30d),
        failed_publishes_30d=failed_publishes_30d,
        plan_mix=plan_mix,
    )


def _plan_distribution_rows(db: Database) -> list[PlanDistributionRow]:
    pipeline = [{"$group": {"_id": "$plan", "count": {"$sum": 1}}}]
    counts = {row["_id"]: row["count"] for row in db["workspaces"].aggregate(pipeline)}
    return [PlanDistributionRow(plan=key, count=counts.get(key, 0)) for key in PLAN_ORDER]


def _ai_usage_sum(db: Database, since: datetime) -> int:
    total = 0
    for kind in ("text", "image", "video"):
        pipeline = [
            {"$match": {"kind": kind, "created_at": {"$gte": since}}},
            {"$group": {"_id": None, "total": {"$sum": "$quantity"}}},
        ]
        result = list(db["plans_ai_usage"].aggregate(pipeline))
        total += int(result[0]["total"]) if result else 0
    return total


def admin_overview(db: Database) -> AdminOverviewOut:
    now = datetime.now(timezone.utc)
    since_30d = now - timedelta(days=30)

    total_users = db["users"].count_documents({})
    total_workspaces = db["workspaces"].count_documents({})

    new_users_30d = db["users"].count_documents({"created_at": {"$gte": since_30d}})
    users_before_30d = max(total_users - new_users_30d, 0)
    user_growth_30d_pct = (
        new_users_30d / users_before_30d * 100
        if users_before_30d > 0
        else (100.0 if new_users_30d > 0 else 0.0)
    )

    posts_30d = db["social_posts"].count_documents(
        {"status": SocialPostStatus.PUBLISHED.value, "published_at": {"$gte": since_30d}}
    )

    failed_publishes_30d = db["social_post_platforms"].count_documents(
        {"status": SocialPlatformPostStatus.FAILED.value}
    )

    ai_usage_30d = _ai_usage_sum(db, since_30d)

    pipeline = [
        {"$match": {"is_active": True}},
        {"$group": {"_id": "$plan", "count": {"$sum": 1}}},
    ]
    mrr_estimate_usd = sum(
        _MRR_PLAN_PRICES_USD.get(row["_id"], 0.0) * row["count"]
        for row in db["workspaces"].aggregate(pipeline)
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


def admin_analytics(db: Database) -> AdminAnalyticsOut:
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=29)
    since_30d = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)

    # Posts over time
    pipeline = [
        {
            "$match": {
                "status": SocialPostStatus.PUBLISHED.value,
                "published_at": {"$gte": since_30d},
            }
        },
        {
            "$group": {
                "_id": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$published_at"}
                },
                "count": {"$sum": 1},
            }
        },
    ]
    posts_by_date: dict[str, int] = {
        row["_id"]: row["count"] for row in db["social_posts"].aggregate(pipeline)
    }

    posts_over_time = [
        PostsOverTimePoint(
            date=start_date + timedelta(days=offset),
            posts=posts_by_date.get(
                (start_date + timedelta(days=offset)).isoformat(), 0
            ),
        )
        for offset in range(30)
    ]

    # Platform mix
    pipeline2 = [
        {"$match": {"status": SocialPlatformPostStatus.PUBLISHED.value}},
        {"$group": {"_id": "$platform", "count": {"$sum": 1}}},
    ]
    platform_mix = [
        PlatformMixRow(platform=row["_id"], count=row["count"])
        for row in db["social_post_platforms"].aggregate(pipeline2)
    ]

    return AdminAnalyticsOut(
        posts_over_time=posts_over_time,
        plan_distribution=_plan_distribution_rows(db),
        platform_mix=platform_mix,
    )


def _get_primary_workspace_for_user(db: Database, user_id: str) -> dict | None:
    return db["workspaces"].find_one(
        {"owner_user_id": str(user_id)},
        sort=[("created_at", 1)],
    )


def _get_membership(db: Database, user_id: str, workspace_id: str) -> dict | None:
    return db["workspace_members"].find_one(
        {"user_id": str(user_id), "workspace_id": str(workspace_id)}
    )


def _to_admin_user_list_item(
    user: dict, workspace_id, workspace_name, workspace_plan, social_level
) -> AdminUserListItem:
    return AdminUserListItem(
        id=user["id"],
        email=user["email"],
        name=user.get("full_name") or user["email"],
        workspace_id=str(workspace_id) if workspace_id else None,
        workspace_name=workspace_name or "",
        plan=workspace_plan or "starter",
        status="active" if user.get("is_active") else "suspended",
        social_permission_level=social_level or "viewer",
        is_platform_admin=bool(user.get("is_platform_admin")),
        created_at=user.get("created_at"),
    )


def _admin_user_item_for(db: Database, user: dict) -> AdminUserListItem:
    workspace = _get_primary_workspace_for_user(db, user["id"])
    membership = _get_membership(db, user["id"], workspace["id"]) if workspace else None
    return _to_admin_user_list_item(
        user,
        workspace["id"] if workspace else None,
        workspace["name"] if workspace else None,
        workspace.get("plan") if workspace else None,
        membership.get("social_level") if membership else None,
    )


def list_admin_users(
    db: Database,
    *,
    search: str | None = None,
    plan: str | None = None,
    status_filter: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> AdminUserListOut:
    page = max(page, 1)
    page_size = max(1, min(page_size, 200))

    query: dict = {}
    if search:
        like = search.strip().lower()
        query["$or"] = [
            {"email": {"$regex": like, "$options": "i"}},
            {"full_name": {"$regex": like, "$options": "i"}},
        ]
    if status_filter in ("active", "suspended"):
        query["is_active"] = status_filter == "active"

    total = db["users"].count_documents(query)

    users = list(
        db["users"]
        .find(query)
        .sort("created_at", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = []
    for user in users:
        workspace = _get_primary_workspace_for_user(db, user["id"])
        # Filter by plan if requested
        if plan and (workspace is None or workspace.get("plan") != plan):
            continue
        membership = _get_membership(db, user["id"], workspace["id"]) if workspace else None
        items.append(
            _to_admin_user_list_item(
                user,
                workspace["id"] if workspace else None,
                workspace["name"] if workspace else None,
                workspace.get("plan") if workspace else None,
                membership.get("social_level") if membership else None,
            )
        )

    total_pages = max(1, (total + page_size - 1) // page_size)
    return AdminUserListOut(
        items=items, total=total, page=page, page_size=page_size, total_pages=total_pages
    )


def create_admin_user(
    db: Database,
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
        workspace_plan = WorkspacePlan(plan).value
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown plan: {plan}"
        ) from exc

    now = utcnow()
    user_id = new_id()
    db["users"].insert_one(
        {
            "id": user_id,
            "email": normalized_email,
            "password_hash": hash_password(password),
            "full_name": full_name.strip() if full_name else None,
            "is_active": True,
            "is_platform_admin": is_platform_admin,
            "google_id": None,
            "created_at": now,
            "updated_at": now,
        }
    )

    resolved_workspace_name = workspace_name or (
        f"{full_name}'s Workspace" if full_name else f"{normalized_email.split('@')[0]}'s Workspace"
    )
    ws_id = new_id()
    db["workspaces"].insert_one(
        {
            "id": ws_id,
            "name": resolved_workspace_name,
            "plan": workspace_plan,
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

    user = db["users"].find_one({"id": user_id})
    return _admin_user_item_for(db, user)


def set_admin_user_suspended(db: Database, user_id: str, suspended: bool | None) -> AdminUserListItem:
    user = get_user_or_404(db, user_id)
    new_active = not suspended if suspended is not None else not user.get("is_active", True)
    db["users"].update_one(
        {"id": str(user_id)}, {"$set": {"is_active": new_active, "updated_at": utcnow()}}
    )
    user = db["users"].find_one({"id": str(user_id)})
    return _admin_user_item_for(db, user)


def set_admin_user_plan(db: Database, user_id: str, plan: str) -> AdminUserListItem:
    user = get_user_or_404(db, user_id)
    workspace = _get_primary_workspace_for_user(db, user_id)
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User has no workspace to update"
        )
    try:
        plan_val = WorkspacePlan(plan).value
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown plan: {plan}"
        ) from exc
    db["workspaces"].update_one(
        {"id": workspace["id"]}, {"$set": {"plan": plan_val, "updated_at": utcnow()}}
    )
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
    return None if value is None or value < 0 else value


def _unlimited_if_none(value: int | None) -> int:
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


def list_pricing_plans(db: Database) -> list[PricingPlanOut]:
    return [_plan_out_to_pricing_plan(plans_service.to_plan_out(db, key)) for key in PLAN_CATALOG]


def update_pricing_plan(db: Database, plan_key: str, payload) -> PricingPlanOut:
    if plan_key not in PLAN_CATALOG:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown plan: {plan_key}"
        )

    override_data: dict = {}
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
            is_bool_field = fe_field in ("brand_voice", "approval_workflow")
            override_data[db_field] = value if is_bool_field else _unlimited_if_none(value)

    if override_data:
        plans_service.upsert_override(db, plan_key, override_data)

    return _plan_out_to_pricing_plan(plans_service.to_plan_out(db, plan_key))
