from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

CamelModel = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class AdminUserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: Optional[str]
    is_active: bool
    is_platform_admin: bool
    created_at: datetime
    workspace_count: int = 0

    class Config:
        from_attributes = True


class AdminUserUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    is_active: Optional[bool] = None
    is_platform_admin: Optional[bool] = None


class AdminWorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    plan: str
    is_active: bool
    owner_email: str
    member_count: int
    created_at: datetime


class AdminWorkspacePlanUpdateRequest(BaseModel):
    plan: str


class AdminWorkspaceStatusUpdateRequest(BaseModel):
    is_active: bool


class PlanMixEntry(BaseModel):
    plan: str
    workspace_count: int


class AnalyticsOverviewOut(BaseModel):
    total_users: int
    total_workspaces: int
    active_workspaces: int
    posts_published_30d: int
    ai_text_generations_30d: int
    ai_image_generations_30d: int
    ai_video_generations_30d: int
    failed_publishes_30d: int
    plan_mix: list[PlanMixEntry]


# ── Frontend-facing camelCase shapes ────────────────────────────────────────
# All models below mirror `social-media/frontend/src/types/admin.ts` 1:1 and use
# a camelCase alias generator so FastAPI's default `response_model_by_alias=True`
# serializes them exactly as the frontend expects, while still letting server
# code construct instances using regular snake_case keyword arguments.


class PlanDistributionRow(BaseModel):
    model_config = CamelModel

    plan: str
    count: int


class PostsOverTimePoint(BaseModel):
    model_config = CamelModel

    date: date
    posts: int


class PlatformMixRow(BaseModel):
    model_config = CamelModel

    platform: str
    count: int


class AdminOverviewOut(BaseModel):
    # `to_camel` would turn "_30d" into "30D" (capital D) — override those 4
    # fields explicitly so the wire format matches FE `AdminOverviewStats`
    # exactly (`posts30d`, not `posts30D`).
    model_config = CamelModel

    total_users: int
    total_workspaces: int
    posts_30d: int = Field(serialization_alias="posts30d", validation_alias="posts30d")
    ai_usage_30d: int = Field(serialization_alias="aiUsage30d", validation_alias="aiUsage30d")
    failed_publishes_30d: int = Field(
        serialization_alias="failedPublishes30d", validation_alias="failedPublishes30d"
    )
    mrr_estimate_usd: float
    user_growth_30d_pct: float = Field(
        serialization_alias="userGrowth30dPct", validation_alias="userGrowth30dPct"
    )
    plan_distribution: list[PlanDistributionRow]


class AdminAnalyticsOut(BaseModel):
    model_config = CamelModel

    posts_over_time: list[PostsOverTimePoint]
    plan_distribution: list[PlanDistributionRow]
    platform_mix: list[PlatformMixRow]


class AdminUserListItem(BaseModel):
    model_config = CamelModel

    id: uuid.UUID
    email: str
    name: str
    workspace_id: Optional[uuid.UUID] = None
    workspace_name: str = ""
    plan: str = "starter"
    status: str
    social_permission_level: str = "viewer"
    is_platform_admin: bool
    created_at: datetime


class AdminUserListOut(BaseModel):
    model_config = CamelModel

    items: list[AdminUserListItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class AdminUserSuspendRequest(BaseModel):
    suspended: Optional[bool] = None


class AdminUserPlanUpdateRequest(BaseModel):
    plan: str


class PricingPlanLimits(BaseModel):
    """Mirrors FE `PlanLimits` — `null` means unlimited (catalog value -1)."""

    model_config = CamelModel

    accounts: Optional[int]
    posts_per_month: Optional[int]
    ai_text_generations: Optional[int]
    ai_image_generations: Optional[int]
    ai_video_generations: Optional[int]
    templates: Optional[int]
    brand_voice: bool
    approval_workflow: bool


class PricingPlanOut(BaseModel):
    model_config = CamelModel

    id: str
    name: str
    tagline: str
    price_monthly_usd: Optional[float]
    price_annual_usd: Optional[float]
    is_custom: bool
    recommended: bool = False
    limits: PricingPlanLimits


class PricingPlanLimitsUpdate(BaseModel):
    accounts: Optional[int] = None
    posts_per_month: Optional[int] = Field(default=None, alias="postsPerMonth")
    ai_text_generations: Optional[int] = Field(default=None, alias="aiTextGenerations")
    ai_image_generations: Optional[int] = Field(default=None, alias="aiImageGenerations")
    ai_video_generations: Optional[int] = Field(default=None, alias="aiVideoGenerations")
    templates: Optional[int] = None
    brand_voice: Optional[bool] = Field(default=None, alias="brandVoice")
    approval_workflow: Optional[bool] = Field(default=None, alias="approvalWorkflow")

    model_config = ConfigDict(populate_by_name=True)


class PricingPlanUpdateRequest(BaseModel):
    """PUT /admin/pricing/{plan_key} — mirrors FE `UpdatePricingPlanPayload`."""

    model_config = ConfigDict(populate_by_name=True)

    tagline: Optional[str] = None
    price_monthly_usd: Optional[float] = Field(default=None, alias="priceMonthlyUsd")
    price_annual_usd: Optional[float] = Field(default=None, alias="priceAnnualUsd")
    limits: Optional[PricingPlanLimitsUpdate] = None
