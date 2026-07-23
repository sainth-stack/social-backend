"""Merge the hardcoded plan catalog with DB admin overrides.

``get_effective_plan()`` is the one function the rest of the app should call
to find out what a workspace's plan actually allows — it transparently
applies any ``PlanOverride`` row on top of ``app.plans.catalog`` defaults.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.plans.catalog import PLAN_CATALOG, PlanDefinition, PlanLimits, get_plan_definition
from app.plans.models import AiUsageEvent, PlanOverride
from app.plans.schemas import PlanLimitsOut, PlanOut


def _apply_override(base: PlanDefinition, override: PlanOverride | None) -> tuple[PlanDefinition, bool]:
    if override is None:
        return base, False

    limits = PlanLimits(
        connected_accounts=_coalesce(override.connected_accounts, base.limits.connected_accounts),
        posts_per_month=_coalesce(override.posts_per_month, base.limits.posts_per_month),
        ai_text_per_month=_coalesce(override.ai_text_per_month, base.limits.ai_text_per_month),
        ai_images_per_month=_coalesce(override.ai_images_per_month, base.limits.ai_images_per_month),
        ai_videos_per_month=_coalesce(override.ai_videos_per_month, base.limits.ai_videos_per_month),
        templates=_coalesce(override.templates, base.limits.templates),
        brand_voice=_coalesce(override.brand_voice, base.limits.brand_voice),
        approval_workflow=_coalesce(override.approval_workflow, base.limits.approval_workflow),
    )
    definition = PlanDefinition(
        key=base.key,
        name=base.name,
        monthly_price_usd=_coalesce(override.monthly_price_usd, base.monthly_price_usd),
        annual_price_usd=_coalesce(override.annual_price_usd, base.annual_price_usd),
        description=base.description,
        limits=limits,
    )
    is_override = any(
        getattr(override, f) is not None
        for f in (
            "monthly_price_usd",
            "annual_price_usd",
            "connected_accounts",
            "posts_per_month",
            "ai_text_per_month",
            "ai_images_per_month",
            "ai_videos_per_month",
            "templates",
            "brand_voice",
            "approval_workflow",
        )
    )
    return definition, is_override


def _coalesce(value, default):
    return value if value is not None else default


def get_override(db: Session, plan_key: str) -> PlanOverride | None:
    stmt = select(PlanOverride).where(PlanOverride.plan_key == plan_key)
    return db.execute(stmt).scalar_one_or_none()


def get_effective_plan(db: Session, plan_key: str) -> PlanDefinition:
    base = get_plan_definition(plan_key)
    override = get_override(db, plan_key)
    definition, _ = _apply_override(base, override)
    return definition


def to_plan_out(db: Session, plan_key: str) -> PlanOut:
    base = get_plan_definition(plan_key)
    override = get_override(db, plan_key)
    definition, is_override = _apply_override(base, override)
    return PlanOut(
        key=definition.key,
        name=definition.name,
        monthly_price_usd=definition.monthly_price_usd,
        annual_price_usd=definition.annual_price_usd,
        description=definition.description,
        limits=PlanLimitsOut(**definition.limits.__dict__),
        is_override=is_override,
    )


def list_effective_plans(db: Session) -> list[PlanOut]:
    return [to_plan_out(db, key) for key in PLAN_CATALOG.keys()]


def upsert_override(db: Session, plan_key: str, data: dict) -> PlanOverride:
    if plan_key not in PLAN_CATALOG:
        raise ValueError(f"Unknown plan: {plan_key}")
    override = get_override(db, plan_key)
    if override is None:
        override = PlanOverride(plan_key=plan_key)
        db.add(override)
    for field, value in data.items():
        if hasattr(override, field):
            setattr(override, field, value)
    db.flush()
    return override


# ── AI usage tracking / enforcement ─────────────────────────────────────────

def _month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def count_ai_usage_this_month(db: Session, workspace_id, kind: str) -> int:
    start = _month_start()
    stmt = select(func.coalesce(func.sum(AiUsageEvent.quantity), 0)).where(
        AiUsageEvent.workspace_id == workspace_id,
        AiUsageEvent.kind == kind,
        AiUsageEvent.created_at >= start,
    )
    return int(db.execute(stmt).scalar_one())


def record_ai_usage(db: Session, workspace_id, kind: str, *, user_id=None, quantity: int = 1) -> AiUsageEvent:
    event = AiUsageEvent(workspace_id=workspace_id, user_id=user_id, kind=kind, quantity=quantity)
    db.add(event)
    db.flush()
    return event
