"""OpsBrain AI Social Media Manager — pricing & plan-limits catalog.

This is the single source of truth for what each plan tier includes. Limits
were sized against Azure OpenAI COGS at roughly a 50% gross-margin budget:

  - Text generation (gpt-4o-mini class model) is cheap — generous quotas.
  - Image generation (gpt-image-2) costs ~$0.02-0.08/image — moderate quotas.
  - Video generation (Sora 2) costs $1-2+/clip — small quotas, gated to
    Growth+ only (Starter gets 0 — too expensive to give away at $399/mo).

Admins can override the display price and any limit per plan via the
``PlanOverride`` DB table (see ``app.plans.models``) without touching this
file — ``app.plans.service.get_effective_plan()`` merges DB overrides on top
of these defaults at request time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    monthly_price_usd: Optional[float]  # None == "custom" (Enterprise)
    annual_price_usd: Optional[float]  # None == "custom" (Enterprise)
    limits: PlanLimits
    description: str = ""


UNLIMITED = -1

PLAN_CATALOG: dict[str, PlanDefinition] = {
    WorkspacePlan.STARTER.value: PlanDefinition(
        key=WorkspacePlan.STARTER.value,
        name="Starter",
        monthly_price_usd=399.0,
        annual_price_usd=3830.0,  # ~20% off 399*12=4788
        description="For solo operators and small teams getting started with AI social publishing.",
        limits=PlanLimits(
            connected_accounts=5,
            posts_per_month=100,
            ai_text_per_month=200,
            ai_images_per_month=50,
            ai_videos_per_month=0,
            templates=20,
            brand_voice=False,
            approval_workflow=False,
        ),
    ),
    WorkspacePlan.GROWTH.value: PlanDefinition(
        key=WorkspacePlan.GROWTH.value,
        name="Growth",
        monthly_price_usd=1499.0,
        annual_price_usd=14390.0,  # ~20% off 1499*12=17988
        description="For growing marketing teams that need brand voice, approvals, and AI video.",
        limits=PlanLimits(
            connected_accounts=20,
            posts_per_month=500,
            ai_text_per_month=1000,
            ai_images_per_month=200,
            ai_videos_per_month=20,
            templates=100,
            brand_voice=True,
            approval_workflow=True,
        ),
    ),
    WorkspacePlan.ENTERPRISE.value: PlanDefinition(
        key=WorkspacePlan.ENTERPRISE.value,
        name="Enterprise",
        monthly_price_usd=None,
        annual_price_usd=None,
        description="Custom pricing for large organizations — unlimited usage, dedicated support.",
        limits=PlanLimits(
            connected_accounts=UNLIMITED,
            posts_per_month=UNLIMITED,
            ai_text_per_month=UNLIMITED,
            ai_images_per_month=UNLIMITED,
            ai_videos_per_month=UNLIMITED,
            templates=UNLIMITED,
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
