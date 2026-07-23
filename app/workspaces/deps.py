from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.core.database import get_db
from app.users.models import User
from app.workspaces.models import SocialLevel, Workspace, WorkspaceMember, WorkspaceRole

# Ordering used to compare "at least X" access levels.
_SOCIAL_LEVEL_RANK = {
    SocialLevel.VIEWER: 0,
    SocialLevel.EDITOR: 1,
    SocialLevel.PUBLISHER: 2,
    SocialLevel.ADMIN: 3,
}


def get_workspace_or_404(db: Session, workspace_id: uuid.UUID) -> Workspace:
    workspace = db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return workspace


def get_membership(db: Session, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceMember | None:
    stmt = select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == user_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def require_workspace_access(
    workspace_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Workspace:
    """Ensure current_user belongs to workspace_id (or is a platform admin)."""
    workspace = get_workspace_or_404(db, workspace_id)

    if current_user.is_platform_admin:
        return workspace

    membership = get_membership(db, workspace_id, current_user.id)
    if not membership:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    if not workspace.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace is suspended")

    return workspace


def require_workspace_membership(
    workspace_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkspaceMember:
    """Return the caller's membership row (creating a virtual admin membership
    for platform admins who are not members)."""
    workspace = get_workspace_or_404(db, workspace_id)
    membership = get_membership(db, workspace_id, current_user.id)

    if membership is None:
        if current_user.is_platform_admin:
            return WorkspaceMember(
                user_id=current_user.id,
                workspace_id=workspace.id,
                role=WorkspaceRole.OWNER,
                social_level=SocialLevel.ADMIN,
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    if not workspace.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace is suspended")

    return membership


def require_social_level(minimum: SocialLevel):
    """Dependency factory: require the caller's social_level >= minimum."""

    def _dep(
        membership: WorkspaceMember = Depends(require_workspace_membership),
    ) -> WorkspaceMember:
        if _SOCIAL_LEVEL_RANK[membership.social_level] < _SOCIAL_LEVEL_RANK[minimum]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires social access level '{minimum.value}' or higher",
            )
        return membership

    return _dep
