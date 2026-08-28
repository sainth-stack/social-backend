"""OpsBrain AI Social Media Manager — pricing & plan-limits catalog.

Three tiers (DB keys kept for compatibility):
  - starter  → Free
  - growth   → Pro
  - enterprise → Growth

New registrations always land on Free. Paid plans are admin-assigned only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.workspaces.models import WorkspacePlan


@dataclass(frozen=True)
class PlanLimits:
    connected_accounts: int  # -1 == unlimited
    posts_per_month: int
    ai_text_per_month: int
    ai_images_per_month: int
    ai_videos_per_month: int
    templates: int
    brand_voice: bool
    approval_workflow: bool


@dataclass(frozen=True)
class PlanDefinition:
    key: str
    name: str
    monthly_price_usd: Optional[float]
    annual_price_usd: Optional[float]
    limits: PlanLimits
    description: str = ""


UNLIMITED = -1

PLAN_CATALOG: dict[str, PlanDefinition] = {
    WorkspacePlan.STARTER.value: PlanDefinition(
        key=WorkspacePlan.STARTER.value,
        name="Free",
        monthly_price_usd=0.0,
        annual_price_usd=0.0,
        description="Default plan for new workspaces.",
        limits=PlanLimits(
            connected_accounts=2,
            posts_per_month=10,
            ai_text_per_month=30,
            ai_images_per_month=5,
            ai_videos_per_month=0,
            templates=3,
            brand_voice=False,
            approval_workflow=False,
        ),
    ),
    WorkspacePlan.GROWTH.value: PlanDefinition(
        key=WorkspacePlan.GROWTH.value,
        name="Pro",
        monthly_price_usd=99.0,
        annual_price_usd=990.0,
        description="Pro plan for teams that need more posts, brand voice, and multi-version AI.",
        limits=PlanLimits(
            connected_accounts=10,
            posts_per_month=100,
            ai_text_per_month=300,
            ai_images_per_month=50,
            ai_videos_per_month=5,
            templates=30,
            brand_voice=True,
            approval_workflow=True,
        ),
    ),
    WorkspacePlan.ENTERPRISE.value: PlanDefinition(
        key=WorkspacePlan.ENTERPRISE.value,
        name="Growth",
        monthly_price_usd=299.0,
        annual_price_usd=2990.0,
        description="Growth plan for agencies and larger brands.",
        limits=PlanLimits(
            connected_accounts=50,
            posts_per_month=500,
            ai_text_per_month=1500,
            ai_images_per_month=200,
            ai_videos_per_month=20,
            templates=100,
            brand_voice=True,
            approval_workflow=True,
        ),
    ),
}

PLAN_ORDER: list[str] = [
    WorkspacePlan.STARTER.value,
    WorkspacePlan.GROWTH.value,
    WorkspacePlan.ENTERPRISE.value,
]


def get_plan_definition(plan_key: str) -> PlanDefinition:
    try:
        return PLAN_CATALOG[plan_key]
    except KeyError as exc:
        raise ValueError(f"Unknown plan: {plan_key}") from exc


def is_unlimited(value: int) -> bool:
    return value is None or value < 0
