"""Celery tasks for social analytics sync."""

from __future__ import annotations

import logging

from app.core.database import SessionLocal
from app.social.analytics.sync import (
    sync_all_orgs_platform_analytics,
    sync_recent_post_metrics,
)
from workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.social.tasks.analytics_sync.sync_platform_analytics",
    queue="social_analytics",
)
def sync_platform_analytics(workspace_id: str | None = None) -> dict:
    """Daily platform analytics rollup (beat: 2am IST = 20:30 UTC)."""
    db = SessionLocal()
    try:
        if workspace_id:
            import uuid

            from app.social.analytics.sync import sync_org_platform_analytics

            count = sync_org_platform_analytics(db, uuid.UUID(workspace_id))
        else:
            count = sync_all_orgs_platform_analytics(db)
        logger.info("sync_platform_analytics accounts=%s", count)
        return {"accounts": count}
    finally:
        db.close()


@celery_app.task(
    name="app.social.tasks.analytics_sync.sync_post_metrics",
    queue="social_analytics",
)
def sync_post_metrics() -> dict:
    """Refresh metrics for posts published in the last 7 days."""
    db = SessionLocal()
    try:
        updated = sync_recent_post_metrics(db, days=7)
        logger.info("sync_post_metrics updated=%s", updated)
        return {"updated": updated}
    finally:
        db.close()
