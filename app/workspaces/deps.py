from __future__ import annotations

from fastapi import Depends, HTTPException, status
from pymongo.database import Database

from app.auth.deps import get_current_user
from app.core.database import get_db
from app.workspaces.models import SocialLevel, WorkspaceRole

# Ordering used to compare "at least X" access levels.
_SOCIAL_LEVEL_RANK: dict[str, int] = {
    SocialLevel.VIEWER.value: 0,
    SocialLevel.EDITOR.value: 1,
    SocialLevel.PUBLISHER.value: 2,
    SocialLevel.ADMIN.value: 3,
}


def get_workspace_or_404(db: Database, workspace_id: str) -> dict:
    workspace = db["workspaces"].find_one({"id": workspace_id})
    if not workspace:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return workspace


def get_membership(db: Database, workspace_id: str, user_id: str) -> dict | None:
    return db["workspace_members"].find_one(
        {"workspace_id": workspace_id, "user_id": user_id}
    )


def require_workspace_access(
    workspace_id: str,
    db: Database = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Ensure current_user belongs to workspace_id (or is a platform admin)."""
    workspace = get_workspace_or_404(db, workspace_id)

    if current_user.get("is_platform_admin"):
        return workspace

    membership = get_membership(db, workspace_id, current_user["id"])
    if not membership:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    if not workspace.get("is_active"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace is suspended")

    return workspace


def require_workspace_membership(
    workspace_id: str,
    db: Database = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Return the caller's membership row (creating a virtual admin membership
    for platform admins who are not members)."""
    from app.core.mongo_utils import new_id, utcnow

    workspace = get_workspace_or_404(db, workspace_id)
    membership = get_membership(db, workspace_id, current_user["id"])

    if membership is None:
        if current_user.get("is_platform_admin"):
            return {
                "id": new_id(),
                "user_id": current_user["id"],
                "workspace_id": workspace["id"],
                "role": WorkspaceRole.OWNER.value,
                "social_level": SocialLevel.ADMIN.value,
                "created_at": utcnow(),
            }
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    if not workspace.get("is_active"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace is suspended")

    return membership


def require_social_level(minimum: SocialLevel):
    """Dependency factory: require the caller's social_level >= minimum."""

    def _dep(
        membership: dict = Depends(require_workspace_membership),
    ) -> dict:
        member_level = membership.get("social_level", SocialLevel.VIEWER.value)
        if _SOCIAL_LEVEL_RANK.get(member_level, 0) < _SOCIAL_LEVEL_RANK[minimum.value]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires social access level '{minimum.value}' or higher",
            )
        return membership

    return _dep
