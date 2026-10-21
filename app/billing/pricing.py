"""INR checkout prices for paid social plans (DB keys: growth → Pro, enterprise → Growth)."""

from __future__ import annotations

from app.core.config import settings
from app.plans.catalog import PLAN_ORDER
from app.workspaces.models import WorkspacePlan

# Paise = INR × 100
DEFAULT_MONTHLY_INR_PAISE: dict[str, int] = {
    WorkspacePlan.GROWTH.value: 49900,  # Pro — ₹499/mo
    WorkspacePlan.ENTERPRISE.value: 99900,  # Growth — ₹999/mo
}

PAID_PLAN_KEYS = frozenset(DEFAULT_MONTHLY_INR_PAISE.keys())

_PLAN_RANK = {key: idx for idx, key in enumerate(PLAN_ORDER)}


def monthly_price_paise(db, plan_key: str) -> int | None:
    """Effective monthly price in paise (admin override → env → catalog default)."""
    if plan_key not in PAID_PLAN_KEYS:
        return None

    from app.plans import service as plans_service

    override = plans_service.get_override(db, plan_key) or {}
    if override.get("monthly_price_inr") is not None:
        return int(round(float(override["monthly_price_inr"]) * 100))
    if override.get("monthly_price_inr_paise") is not None:
        return int(override["monthly_price_inr_paise"])

    env_map = {
        WorkspacePlan.GROWTH.value: settings.billing_pro_monthly_paise,
        WorkspacePlan.ENTERPRISE.value: settings.billing_growth_monthly_paise,
    }
    env_val = env_map.get(plan_key)
    if env_val is not None:
        return int(env_val)

    return DEFAULT_MONTHLY_INR_PAISE[plan_key]


def plan_rank(plan_key: str) -> int:
    return _PLAN_RANK.get(plan_key, -1)


def assert_upgrade_allowed(current_plan: str, target_plan: str) -> None:
    from fastapi import HTTPException, status

    if target_plan not in PAID_PLAN_KEYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only Pro and Growth plans are available for self-serve checkout.",
        )
    if plan_rank(target_plan) <= plan_rank(current_plan):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select a higher plan than your current subscription.",
        )
