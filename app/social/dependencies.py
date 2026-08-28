"""Auth + org-scope guards for Social Media endpoints."""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.workspaces.models import Workspace
from app.workspaces.deps import require_workspace_access
from app.social.models import SocialAccount, SocialPost


def get_social_account_or_404(
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_workspace_access),
) -> SocialAccount:
    account = db.get(SocialAccount, account_id)
    if not account or account.workspace_id != workspace.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Social account not found")
    return account


def get_social_post_or_404(
    post_id: uuid.UUID,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_workspace_access),
) -> SocialPost:
    post = db.get(SocialPost, post_id)
    if not post or post.workspace_id != workspace.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Social post not found")
    return post
