"""SQLAlchemy ORM models for the Social Media module (Sprint 1 tables)."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.security import utcnow


class SocialPlatform(str, enum.Enum):
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    LINKEDIN = "linkedin"
    X = "x"


class SocialAccountType(str, enum.Enum):
    PAGE = "page"
    PROFILE = "profile"
    GROUP = "group"


class SocialPostStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    ARCHIVED = "archived"


class SocialApprovalStatus(str, enum.Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class SocialImageSource(str, enum.Enum):
    UPLOADED = "uploaded"
    AI_GENERATED = "ai_generated"
    NONE = "none"


class SocialMediaAssetType(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"


class SocialPlatformPostStatus(str, enum.Enum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    SKIPPED = "skipped"


# Shared PG enum types (created once in migration; reused across tables)
SocialPlatformColumn = Enum(
    SocialPlatform,
    name="social_platform",
    values_callable=lambda e: [m.value for m in e],
    create_constraint=False,
)
SocialAccountTypeColumn = Enum(
    SocialAccountType,
    name="social_account_type",
    values_callable=lambda e: [m.value for m in e],
    create_constraint=False,
)
SocialPostStatusColumn = Enum(
    SocialPostStatus,
    name="social_post_status",
    values_callable=lambda e: [m.value for m in e],
    create_constraint=False,
)
SocialApprovalStatusColumn = Enum(
    SocialApprovalStatus,
    name="social_approval_status",
    values_callable=lambda e: [m.value for m in e],
    create_constraint=False,
)
SocialImageSourceColumn = Enum(
    SocialImageSource,
    name="social_image_source",
    values_callable=lambda e: [m.value for m in e],
    create_constraint=False,
)
SocialMediaAssetTypeColumn = Enum(
    SocialMediaAssetType,
    name="social_media_asset_type",
    values_callable=lambda e: [m.value for m in e],
    create_constraint=False,
)
SocialPlatformPostStatusColumn = Enum(
    SocialPlatformPostStatus,
    name="social_platform_post_status",
    values_callable=lambda e: [m.value for m in e],
    create_constraint=False,
)


class SentenceLength(str, enum.Enum):
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


class EmojiUsage(str, enum.Enum):
    NEVER = "never"
    SOMETIMES = "sometimes"
    OFTEN = "often"


SentenceLengthColumn = Enum(
    SentenceLength,
    name="social_sentence_length",
    values_callable=lambda e: [m.value for m in e],
    create_constraint=False,
)
EmojiUsageColumn = Enum(
    EmojiUsage,
    name="social_emoji_usage",
    values_callable=lambda e: [m.value for m in e],
    create_constraint=False,
)


class SocialBrandVoice(Base):
    __tablename__ = "social_brand_voices"
    __table_args__ = (UniqueConstraint("workspace_id", name="uq_social_brand_voices_org_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    brand_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    industry: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    tagline: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    target_audience: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tones: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    words_to_use: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    words_to_avoid: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    cta_phrases: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    sentence_length: Mapped[SentenceLength] = mapped_column(
        SentenceLengthColumn,
        nullable=False,
        default=SentenceLength.MEDIUM,
    )
    emoji_usage: Mapped[EmojiUsage] = mapped_column(
        EmojiUsageColumn,
        nullable=False,
        default=EmojiUsage.SOMETIMES,
    )
    primary_language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    system_prompt_override: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    logo_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class SocialAccount(Base):
    __tablename__ = "social_accounts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "platform",
            "platform_account_id",
            name="uq_social_accounts_org_platform_account",
        ),
        Index("ix_social_accounts_org_id", "workspace_id"),
        Index("ix_social_accounts_org_platform", "workspace_id", "platform"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[SocialPlatform] = mapped_column(SocialPlatformColumn, nullable=False)
    account_type: Mapped[SocialAccountType] = mapped_column(
        SocialAccountTypeColumn,
        nullable=False,
        default=SocialAccountType.PAGE,
    )
    platform_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_picture_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    follower_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    access_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    post_platforms: Mapped[list["SocialPostPlatform"]] = relationship(
        "SocialPostPlatform",
        back_populates="social_account",
    )


class SocialPost(Base):
    __tablename__ = "social_posts"
    __table_args__ = (
        Index("ix_social_posts_org_id", "workspace_id"),
        Index("ix_social_posts_org_status", "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[SocialPostStatus] = mapped_column(
        SocialPostStatusColumn,
        nullable=False,
        default=SocialPostStatus.DRAFT,
    )
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_status: Mapped[SocialApprovalStatus] = mapped_column(
        SocialApprovalStatusColumn,
        nullable=False,
        default=SocialApprovalStatus.NOT_REQUIRED,
    )
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    template_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    ai_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_source: Mapped[SocialImageSource] = mapped_column(
        SocialImageSourceColumn,
        nullable=False,
        default=SocialImageSource.NONE,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    platforms: Mapped[list["SocialPostPlatform"]] = relationship(
        "SocialPostPlatform",
        back_populates="post",
        cascade="all, delete-orphan",
    )


class SocialMediaAsset(Base):
    __tablename__ = "social_media_assets"
    __table_args__ = (
        Index("ix_social_media_assets_org_id", "workspace_id"),
        Index("ix_social_media_assets_org_type", "workspace_id", "media_type"),
        Index("ix_social_media_assets_org_created", "workspace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    media_type: Mapped[SocialMediaAssetType] = mapped_column(
        SocialMediaAssetTypeColumn,
        nullable=False,
    )
    source: Mapped[SocialImageSource] = mapped_column(
        SocialImageSourceColumn,
        nullable=False,
    )
    blob_key: Mapped[str] = mapped_column(String(512), nullable=False)
    blob_url: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/octet-stream")
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sora_video_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    post_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_posts.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class SocialPostPlatform(Base):
    __tablename__ = "social_post_platforms"
    __table_args__ = (Index("ix_social_post_platforms_post_id", "post_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_posts.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[SocialPlatform] = mapped_column(SocialPlatformColumn, nullable=False)
    social_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    caption: Mapped[str] = mapped_column(Text, nullable=False, default="")
    hashtags: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    first_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    character_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[SocialPlatformPostStatus] = mapped_column(
        SocialPlatformPostStatusColumn,
        nullable=False,
        default=SocialPlatformPostStatus.PENDING,
    )
    platform_post_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reach: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shares: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engagement_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    post: Mapped["SocialPost"] = relationship("SocialPost", back_populates="platforms")
    social_account: Mapped["SocialAccount"] = relationship(
        "SocialAccount",
        back_populates="post_platforms",
    )


class SocialAnalyticsDaily(Base):
    __tablename__ = "social_analytics_daily"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "social_account_id",
            "date",
            name="uq_social_analytics_daily_org_account_date",
        ),
        Index("ix_social_analytics_daily_org_date", "workspace_id", "date"),
        Index("ix_social_analytics_daily_account_date", "social_account_id", "date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[SocialPlatform] = mapped_column(SocialPlatformColumn, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    follower_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_followers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    posts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_reach: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_engagements: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SocialPermission(str, enum.Enum):
    VIEWER = "viewer"
    EDITOR = "editor"
    PUBLISHER = "publisher"
    ADMIN = "admin"


SocialPermissionColumn = Enum(
    SocialPermission,
    name="social_permission",
    values_callable=lambda e: [m.value for m in e],
    create_constraint=False,
)


class SocialSettings(Base):
    __tablename__ = "social_settings"
    __table_args__ = (UniqueConstraint("workspace_id", name="uq_social_settings_org_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    default_language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approver_user_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    approval_sla_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    approval_sla_action: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    default_posting_times: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    queue_gap_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    blackout_dates: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    default_tone: Mapped[str] = mapped_column(String(64), nullable=False, default="Professional")
    default_cta: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    hashtag_count: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    auto_first_comment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    image_generation_style: Mapped[str] = mapped_column(String(64), nullable=False, default="Photographic")
    openai_model: Mapped[str] = mapped_column(String(64), nullable=False, default="gpt-4o-mini")
    system_prompt_override: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enabled_platforms: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    notification_events: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    notification_delivery: Mapped[str] = mapped_column(String(32), nullable=False, default="in_app")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class SocialTemplate(Base):
    __tablename__ = "social_templates"
    __table_args__ = (Index("ix_social_templates_org_id", "workspace_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    platforms: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    caption_template: Mapped[str] = mapped_column(Text, nullable=False, default="")
    hashtags: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    system_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    goal: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    placeholders: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    image_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    generate_image: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suggested_tone: Mapped[str] = mapped_column(String(64), nullable=False, default="Professional")
    suggested_cta: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    first_comment_template: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class SocialAuditLog(Base):
    __tablename__ = "social_audit_logs"
    __table_args__ = (
        Index("ix_social_audit_logs_org_id", "workspace_id"),
        Index("ix_social_audit_logs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, default="post")
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
