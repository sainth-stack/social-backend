"""Token refresh and approval reminder Celery tasks."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core.database import get_database
from app.core.mongo_utils import utcnow
from app.social.audit import write_social_audit
from app.social.models import (
    SocialApprovalStatus,
    SocialPostStatus,
)
from workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.social.tasks.maintenance.refresh_expiring_tokens",
    queue="social_maintenance",
)
def refresh_expiring_tokens() -> dict:
    """Mark accounts with tokens expiring within 48h; attempt refresh when possible."""
    db = get_database()
    marked = 0
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=48)
    accounts = list(
        db["social_accounts"].find(
            {
                "is_active": True,
                "token_expires_at": {"$ne": None, "$lte": horizon},
            }
        )
    )
    for account in accounts:
        token_expires = account.get("token_expires_at")
        if not token_expires:
            continue
        if token_expires.tzinfo is None:
            token_expires = token_expires.replace(tzinfo=timezone.utc)
        if token_expires <= now:
            write_social_audit(
                db,
                workspace_id=account["workspace_id"],
                action="account.token_expired",
                entity_type="account",
                entity_id=account["id"],
                metadata={"platform": account.get("platform")},
            )
        else:
            write_social_audit(
                db,
                workspace_id=account["workspace_id"],
                action="account.token_expiring",
                entity_type="account",
                entity_id=account["id"],
                metadata={"platform": account.get("platform")},
            )
        marked += 1
    return {"marked": marked}


@celery_app.task(
    name="app.social.tasks.maintenance.send_approval_reminders",
    queue="social_maintenance",
)
def send_approval_reminders() -> dict:
    """Auto-approve/reject posts past SLA, or log reminders."""
    db = get_database()
    acted = 0
    settings_rows = list(db["social_settings"].find({"approval_required": True}))
    settings_by_org = {r["workspace_id"]: r for r in settings_rows}
    pending = list(
        db["social_posts"].find(
            {
                "status": SocialPostStatus.PENDING_APPROVAL.value,
                "approval_status": SocialApprovalStatus.PENDING.value,
            }
        )
    )
    now = datetime.now(timezone.utc)
    for post in pending:
        settings = settings_by_org.get(post["workspace_id"])
        if not settings:
            continue
        sla = settings.get("approval_sla_hours") or 24
        updated_at = post.get("updated_at")
        if updated_at is None:
            continue
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age = now - updated_at
        if age < timedelta(hours=sla):
            continue
        action = settings.get("approval_sla_action") or "none"
        if action == "auto_approve":
            db["social_posts"].update_one(
                {"id": post["id"]},
                {"$set": {
                    "approval_status": SocialApprovalStatus.APPROVED.value,
                    "status": SocialPostStatus.DRAFT.value,
                    "updated_at": utcnow(),
                }},
            )
            write_social_audit(
                db,
                workspace_id=post["workspace_id"],
                action="post.auto_approved",
                entity_id=post["id"],
            )
            acted += 1
        elif action == "auto_reject":
            db["social_posts"].update_one(
                {"id": post["id"]},
                {"$set": {
                    "approval_status": SocialApprovalStatus.REJECTED.value,
                    "status": SocialPostStatus.DRAFT.value,
                    "updated_at": utcnow(),
                }},
            )
            write_social_audit(
                db,
                workspace_id=post["workspace_id"],
                action="post.auto_rejected",
                entity_id=post["id"],
            )
            acted += 1
        else:
            write_social_audit(
                db,
                workspace_id=post["workspace_id"],
                action="post.approval_reminder",
                entity_id=post["id"],
            )
            acted += 1
    return {"acted": acted}
