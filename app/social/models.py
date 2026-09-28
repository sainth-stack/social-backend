"""Social Media module enums — no ORM models (MongoDB is schemaless)."""

from __future__ import annotations

import enum


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


class SocialPermission(str, enum.Enum):
    VIEWER = "viewer"
    EDITOR = "editor"
    PUBLISHER = "publisher"
    ADMIN = "admin"


class SentenceLength(str, enum.Enum):
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


class EmojiUsage(str, enum.Enum):
    NEVER = "never"
    SOMETIMES = "sometimes"
    OFTEN = "often"
