"""Token refresh and approval reminder Celery tasks."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import SessionLocal
from app.social.audit import write_social_audit
from app.social.models import (
    SocialAccount,
    SocialApprovalStatus,
    SocialPost,
    SocialPostStatus,
    SocialSettings,
)
from workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.social.tasks.maintenance.refresh_expiring_tokens",
    queue="social_maintenance",
)
def refresh_expiring_tokens() -> dict:
    """Mark accounts with tokens expiring within 48h; attempt refresh when possible."""
    db = SessionLocal()
    marked = 0
    try:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=48)
        accounts = db.scalars(
            select(SocialAccount).where(
                SocialAccount.is_active.is_(True),
                SocialAccount.token_expires_at.is_not(None),
                SocialAccount.token_expires_at <= horizon,
            )
        ).all()
        for account in accounts:
            # Full refresh requires platform-specific refresh tokens.
            # For now, audit and leave token status to be detected by UI.
            if account.token_expires_at and account.token_expires_at.replace(
                tzinfo=timezone.utc
            ) <= now:
                write_social_audit(
                    db,
                    workspace_id=account.workspace_id,
                    action="account.token_expired",
                    entity_type="account",
                    entity_id=account.id,
                    metadata={"platform": account.platform.value},
                )
                marked += 1
            else:
                write_social_audit(
                    db,
                    workspace_id=account.workspace_id,
                    action="account.token_expiring",
                    entity_type="account",
                    entity_id=account.id,
                    metadata={"platform": account.platform.value},
                )
                marked += 1
        db.commit()
        return {"marked": marked}
    finally:
        db.close()


@celery_app.task(
    name="app.social.tasks.maintenance.send_approval_reminders",
    queue="social_maintenance",
)
def send_approval_reminders() -> dict:
    """Auto-approve/reject posts past SLA, or log reminders."""
    db = SessionLocal()
    acted = 0
    try:
        settings_rows = db.scalars(
            select(SocialSettings).where(SocialSettings.approval_required.is_(True))
        ).all()
        settings_by_org = {r.workspace_id: r for r in settings_rows}
        pending = db.scalars(
            select(SocialPost).where(
                SocialPost.status == SocialPostStatus.PENDING_APPROVAL,
                SocialPost.approval_status == SocialApprovalStatus.PENDING,
            )
        ).all()
        now = datetime.now(timezone.utc)
        for post in pending:
            settings = settings_by_org.get(post.workspace_id)
            if not settings:
                continue
            sla = settings.approval_sla_hours or 24
            age = now - (post.updated_at.replace(tzinfo=timezone.utc) if post.updated_at.tzinfo is None else post.updated_at)
            if age < timedelta(hours=sla):
                continue
            action = settings.approval_sla_action or "none"
            if action == "auto_approve":
                post.approval_status = SocialApprovalStatus.APPROVED
                post.status = SocialPostStatus.DRAFT
                write_social_audit(
                    db,
                    workspace_id=post.workspace_id,
                    action="post.auto_approved",
                    entity_id=post.id,
                )
                acted += 1
            elif action == "auto_reject":
                post.approval_status = SocialApprovalStatus.REJECTED
                post.status = SocialPostStatus.DRAFT
                write_social_audit(
                    db,
                    workspace_id=post.workspace_id,
                    action="post.auto_rejected",
                    entity_id=post.id,
                )
                acted += 1
            else:
                write_social_audit(
                    db,
                    workspace_id=post.workspace_id,
                    action="post.approval_reminder",
                    entity_id=post.id,
                )
                acted += 1
        db.commit()
        return {"acted": acted}
    finally:
        db.close()
