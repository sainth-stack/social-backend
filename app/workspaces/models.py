"""Workspace enums — no ORM models (MongoDB is schemaless)."""

from __future__ import annotations

import enum


class WorkspacePlan(str, enum.Enum):
    STARTER = "starter"
    GROWTH = "growth"
    ENTERPRISE = "enterprise"


class WorkspaceRole(str, enum.Enum):
    OWNER = "owner"
    MEMBER = "member"


class SocialLevel(str, enum.Enum):
    VIEWER = "viewer"
    EDITOR = "editor"
    PUBLISHER = "publisher"
    ADMIN = "admin"
