"""Social Media permission checks.

``WorkspaceMember.social_level`` (viewer < editor < publisher < admin) is the
single source of truth for what a user can do inside a workspace's social
module — there is no separate per-feature permissions table. The workspace
owner always has admin-equivalent access even if their membership row was
never explicitly set to "admin".
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.social.models import SocialPermission
from app.users.models import User
from app.workspaces.deps import get_membership
from app.workspaces.models import SocialLevel, Workspace

_RANK = {
    SocialPermission.VIEWER: 1,
    SocialPermission.EDITOR: 2,
    SocialPermission.PUBLISHER: 3,
    SocialPermission.ADMIN: 4,
}

_LEVEL_TO_PERMISSION = {
    SocialLevel.VIEWER: SocialPermission.VIEWER,
    SocialLevel.EDITOR: SocialPermission.EDITOR,
    SocialLevel.PUBLISHER: SocialPermission.PUBLISHER,
    SocialLevel.ADMIN: SocialPermission.ADMIN,
}


def get_user_permission(db: Session, workspace: Workspace, user: User) -> SocialPermission:
    if user.is_platform_admin or workspace.owner_user_id == user.id:
        return SocialPermission.ADMIN
    membership = get_membership(db, workspace.id, user.id)
    if not membership:
        return SocialPermission.VIEWER
    return _LEVEL_TO_PERMISSION[membership.social_level]


def require_permission(
    db: Session,
    workspace: Workspace,
    user: User,
    minimum: SocialPermission,
) -> SocialPermission:
    current = get_user_permission(db, workspace, user)
    if _RANK[current] < _RANK[minimum]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires social permission: {minimum.value}",
        )
    return current


def can_manage_team(user: User, workspace: Workspace, db: Session) -> bool:
    """Workspace owners and platform admins can manage team social permissions."""
    if user.is_platform_admin or workspace.owner_user_id == user.id:
        return True
    return get_user_permission(db, workspace, user) == SocialPermission.ADMIN
