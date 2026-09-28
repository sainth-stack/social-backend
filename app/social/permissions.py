"""Social Media permission checks."""

from __future__ import annotations

from fastapi import HTTPException, status
from pymongo.database import Database

from app.social.models import SocialPermission
from app.workspaces.deps import get_membership
from app.workspaces.models import SocialLevel

_RANK = {
    SocialPermission.VIEWER: 1,
    SocialPermission.EDITOR: 2,
    SocialPermission.PUBLISHER: 3,
    SocialPermission.ADMIN: 4,
}

_LEVEL_TO_PERMISSION = {
    SocialLevel.VIEWER.value: SocialPermission.VIEWER,
    SocialLevel.EDITOR.value: SocialPermission.EDITOR,
    SocialLevel.PUBLISHER.value: SocialPermission.PUBLISHER,
    SocialLevel.ADMIN.value: SocialPermission.ADMIN,
}


def get_user_permission(db: Database, workspace: dict, user: dict) -> SocialPermission:
    if user.get("is_platform_admin") or workspace.get("owner_user_id") == user["id"]:
        return SocialPermission.ADMIN
    membership = get_membership(db, workspace["id"], user["id"])
    if not membership:
        return SocialPermission.VIEWER
    level = membership.get("social_level", SocialLevel.VIEWER.value)
    return _LEVEL_TO_PERMISSION.get(level, SocialPermission.VIEWER)


def require_permission(
    db: Database,
    workspace: dict,
    user: dict,
    minimum: SocialPermission,
) -> SocialPermission:
    current = get_user_permission(db, workspace, user)
    if _RANK[current] < _RANK[minimum]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires social permission: {minimum.value}",
        )
    return current


def can_manage_team(user: dict, workspace: dict, db: Database) -> bool:
    """Workspace owners and platform admins can manage team social permissions."""
    if user.get("is_platform_admin") or workspace.get("owner_user_id") == user["id"]:
        return True
    return get_user_permission(db, workspace, user) == SocialPermission.ADMIN
