from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.security import utcnow


class PlanOverride(Base):
    """Admin-editable overrides for a plan's display price and limits.

    One row per plan key (starter | growth | enterprise). Any column left
    NULL falls back to the hardcoded default in ``app.plans.catalog``. This
    lets platform admins tweak pricing/limits live without a deploy.
    """

    __tablename__ = "plan_overrides"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_key: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    monthly_price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    annual_price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    connected_accounts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    posts_per_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ai_text_per_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ai_images_per_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ai_videos_per_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    templates: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    brand_voice: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    approval_workflow: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class AiUsageEvent(Base):
    """One row per AI generation attempt — used to enforce monthly quotas.

    kind is one of: text | image | video. Recorded regardless of whether the
    generated content is ultimately used in a post, since the AI provider
    cost is incurred at generation time.
    """

    __tablename__ = "social_ai_usage_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # text | image | video
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metadata_json: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
