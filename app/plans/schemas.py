from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class PlanLimitsOut(BaseModel):
    connected_accounts: int
    posts_per_month: int
    ai_text_per_month: int
    ai_images_per_month: int
    ai_videos_per_month: int
    templates: int
    brand_voice: bool
    approval_workflow: bool


class PlanOut(BaseModel):
    key: str
    name: str
    monthly_price_usd: Optional[float]
    annual_price_usd: Optional[float]
    description: str
    limits: PlanLimitsOut
    is_override: bool = False


class PlanUpdateRequest(BaseModel):
    """PUT /admin/plans/{plan_key} — any field left unset clears that override."""

    monthly_price_usd: Optional[float] = None
    annual_price_usd: Optional[float] = None
    connected_accounts: Optional[int] = None
    posts_per_month: Optional[int] = None
    ai_text_per_month: Optional[int] = None
    ai_images_per_month: Optional[int] = None
    ai_videos_per_month: Optional[int] = None
    templates: Optional[int] = None
    brand_voice: Optional[bool] = None
    approval_workflow: Optional[bool] = None


class WorkspaceUsageOut(BaseModel):
    plan: PlanOut
    posts_this_month: int
    ai_text_this_month: int
    ai_images_this_month: int
    ai_videos_this_month: int
    connected_accounts_count: int
    templates_count: int
