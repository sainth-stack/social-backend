"""Merge the hardcoded plan catalog with DB admin overrides."""

from __future__ import annotations

from datetime import datetime, timezone

from pymongo.database import Database

from app.plans.catalog import PLAN_CATALOG, PlanDefinition, PlanLimits, get_plan_definition
from app.plans.schemas import PlanLimitsOut, PlanOut


def _apply_override(base: PlanDefinition, override: dict | None) -> tuple[PlanDefinition, bool]:
    if override is None:
        return base, False

    limits = PlanLimits(
        connected_accounts=_coalesce(override.get("connected_accounts"), base.limits.connected_accounts),
        posts_per_month=_coalesce(override.get("posts_per_month"), base.limits.posts_per_month),
        ai_text_per_month=_coalesce(override.get("ai_text_per_month"), base.limits.ai_text_per_month),
        ai_images_per_month=_coalesce(override.get("ai_images_per_month"), base.limits.ai_images_per_month),
        ai_videos_per_month=_coalesce(override.get("ai_videos_per_month"), base.limits.ai_videos_per_month),
        templates=_coalesce(override.get("templates"), base.limits.templates),
        brand_voice=_coalesce(override.get("brand_voice"), base.limits.brand_voice),
        approval_workflow=_coalesce(override.get("approval_workflow"), base.limits.approval_workflow),
    )
    definition = PlanDefinition(
        key=base.key,
        name=base.name,
        monthly_price_usd=_coalesce(override.get("monthly_price_usd"), base.monthly_price_usd),
        annual_price_usd=_coalesce(override.get("annual_price_usd"), base.annual_price_usd),
        description=base.description,
        limits=limits,
    )
    is_override = any(
        override.get(f) is not None
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


def get_override(db: Database, plan_key: str) -> dict | None:
    return db["plan_overrides"].find_one({"plan_key": plan_key})


def get_effective_plan(db: Database, plan_key: str) -> PlanDefinition:
    base = get_plan_definition(plan_key)
    override = get_override(db, plan_key)
    definition, _ = _apply_override(base, override)
    return definition


def to_plan_out(db: Database, plan_key: str) -> PlanOut:
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


def list_effective_plans(db: Database) -> list[PlanOut]:
    return [to_plan_out(db, key) for key in PLAN_CATALOG.keys()]


def upsert_override(db: Database, plan_key: str, data: dict) -> dict:
    if plan_key not in PLAN_CATALOG:
        raise ValueError(f"Unknown plan: {plan_key}")
    from app.core.mongo_utils import new_id, utcnow

    now = utcnow()
    override = get_override(db, plan_key)
    if override is None:
        doc = {"id": new_id(), "plan_key": plan_key, "created_at": now, "updated_at": now}
        doc.update(data)
        db["plan_overrides"].insert_one(doc)
        return db["plan_overrides"].find_one({"plan_key": plan_key})

    data["updated_at"] = now
    db["plan_overrides"].update_one({"plan_key": plan_key}, {"$set": data})
    return db["plan_overrides"].find_one({"plan_key": plan_key})


# ── AI usage tracking / enforcement ─────────────────────────────────────────

def _month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def count_ai_usage_this_month(db: Database, workspace_id: str, kind: str) -> int:
    start = _month_start()
    pipeline = [
        {
            "$match": {
                "workspace_id": str(workspace_id),
                "kind": kind,
                "created_at": {"$gte": start},
            }
        },
        {"$group": {"_id": None, "total": {"$sum": "$quantity"}}},
    ]
    result = list(db["plans_ai_usage"].aggregate(pipeline))
    return int(result[0]["total"]) if result else 0


def record_ai_usage(
    db: Database,
    workspace_id: str,
    kind: str,
    *,
    user_id: str | None = None,
    quantity: int = 1,
) -> dict:
    from app.core.mongo_utils import new_id, utcnow

    event = {
        "id": new_id(),
        "workspace_id": str(workspace_id),
        "user_id": str(user_id) if user_id else None,
        "kind": kind,
        "quantity": quantity,
        "metadata_json": None,
        "created_at": utcnow(),
    }
    db["plans_ai_usage"].insert_one(event)
    return event
