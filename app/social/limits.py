"""Plan-limit enforcement for the Social Media module."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from pymongo.database import Database

from app.plans.catalog import PlanDefinition, is_unlimited
from app.plans.service import count_ai_usage_this_month, get_effective_plan


def get_limits(db: Database, workspace: dict) -> PlanDefinition:
    return get_effective_plan(db, workspace.get("plan", "starter"))


def _month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def _payment_required(code: str, message: str, limit: int, used: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={"code": code, "message": message, "limit": limit, "used": used},
    )


def enforce_account_limit(db: Database, workspace: dict) -> None:
    plan = get_limits(db, workspace)
    limit = plan.limits.connected_accounts
    if is_unlimited(limit):
        return
    count = db["social_accounts"].count_documents(
        {"workspace_id": workspace["id"], "is_active": True}
    )
    if count >= limit:
        raise _payment_required(
            "PLAN_LIMIT_ACCOUNTS",
            f"Your {plan.name} plan allows {limit} connected accounts. Upgrade to connect more.",
            limit,
            count,
        )


def enforce_posts_limit(db: Database, workspace: dict) -> None:
    plan = get_limits(db, workspace)
    limit = plan.limits.posts_per_month
    if is_unlimited(limit):
        return
    count = db["social_posts"].count_documents(
        {"workspace_id": workspace["id"], "created_at": {"$gte": _month_start()}}
    )
    if count >= limit:
        raise _payment_required(
            "PLAN_LIMIT_POSTS",
            f"Your {plan.name} plan allows {limit} posts per month. Upgrade for higher limits.",
            limit,
            count,
        )


def enforce_templates_limit(db: Database, workspace: dict) -> None:
    plan = get_limits(db, workspace)
    limit = plan.limits.templates
    if is_unlimited(limit):
        return
    count = db["social_templates"].count_documents(
        {"workspace_id": workspace["id"], "is_system": False}
    )
    if count >= limit:
        raise _payment_required(
            "PLAN_LIMIT_TEMPLATES",
            f"Your {plan.name} plan allows {limit} templates. Upgrade to create more.",
            limit,
            count,
        )


def enforce_approval_available(db: Database, workspace: dict) -> None:
    plan = get_limits(db, workspace)
    if not plan.limits.approval_workflow:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "PLAN_LIMIT_APPROVAL",
                "message": "Approval workflow requires the Growth plan or higher.",
            },
        )


# ── AI generation quotas (text / image / video) — enforced separately ──────

def _enforce_ai_limit(db: Database, workspace: dict, kind: str, limit: int, plan_name: str) -> None:
    if is_unlimited(limit):
        return
    used = count_ai_usage_this_month(db, workspace["id"], kind)
    if used >= limit:
        label = {"text": "AI text generations", "image": "AI images", "video": "AI videos"}[kind]
        raise _payment_required(
            f"PLAN_LIMIT_AI_{kind.upper()}",
            f"Your {plan_name} plan allows {limit} {label} per month. Upgrade for higher limits.",
            limit,
            used,
        )


def enforce_ai_text_limit(db: Database, workspace: dict) -> None:
    plan = get_limits(db, workspace)
    _enforce_ai_limit(db, workspace, "text", plan.limits.ai_text_per_month, plan.name)


def enforce_ai_image_limit(db: Database, workspace: dict) -> None:
    plan = get_limits(db, workspace)
    _enforce_ai_limit(db, workspace, "image", plan.limits.ai_images_per_month, plan.name)


def enforce_ai_video_limit(db: Database, workspace: dict) -> None:
    plan = get_limits(db, workspace)
    if plan.limits.ai_videos_per_month == 0:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "PLAN_LIMIT_AI_VIDEO",
                "message": f"AI video generation is not available on the {plan.name} plan. Upgrade to Growth or higher.",
            },
        )
    _enforce_ai_limit(db, workspace, "video", plan.limits.ai_videos_per_month, plan.name)


def usage_snapshot(db: Database, workspace: dict) -> dict:
    plan = get_limits(db, workspace)
    accounts = db["social_accounts"].count_documents(
        {"workspace_id": workspace["id"], "is_active": True}
    )
    posts = db["social_posts"].count_documents(
        {"workspace_id": workspace["id"], "created_at": {"$gte": _month_start()}}
    )
    templates = db["social_templates"].count_documents(
        {"workspace_id": workspace["id"], "is_system": False}
    )
    ai_text = count_ai_usage_this_month(db, workspace["id"], "text")
    ai_images = count_ai_usage_this_month(db, workspace["id"], "image")
    ai_videos = count_ai_usage_this_month(db, workspace["id"], "video")
    return {
        "plan": workspace.get("plan", "starter"),
        "accounts": {"used": accounts, "limit": plan.limits.connected_accounts},
        "postsThisMonth": {"used": posts, "limit": plan.limits.posts_per_month},
        "templates": {"used": templates, "limit": plan.limits.templates},
        "aiTextThisMonth": {"used": ai_text, "limit": plan.limits.ai_text_per_month},
        "aiImagesThisMonth": {"used": ai_images, "limit": plan.limits.ai_images_per_month},
        "aiVideosThisMonth": {"used": ai_videos, "limit": plan.limits.ai_videos_per_month},
        "approvalWorkflow": plan.limits.approval_workflow,
        "brandVoice": plan.limits.brand_voice,
    }
