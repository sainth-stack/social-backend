"""Auth + org-scope guards for Social Media endpoints."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from pymongo.database import Database

from app.core.database import get_db
from app.workspaces.deps import require_workspace_access


def get_social_account_or_404(
    account_id: str,
    db: Database = Depends(get_db),
    workspace: dict = Depends(require_workspace_access),
) -> dict:
    account = db["social_accounts"].find_one({"id": account_id})
    if not account or account.get("workspace_id") != workspace["id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Social account not found")
    return account


def get_social_post_or_404(
    post_id: str,
    db: Database = Depends(get_db),
    workspace: dict = Depends(require_workspace_access),
) -> dict:
    post = db["social_posts"].find_one({"id": post_id})
    if not post or post.get("workspace_id") != workspace["id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Social post not found")
    return post
