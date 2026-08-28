"""Celery beat: enqueue due scheduled social posts."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.database import SessionLocal
from app.social.models import SocialPost, SocialPostStatus
from workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.social.tasks.scheduler.enqueue_due_social_posts",
    queue="social_publish",
)
def enqueue_due_social_posts() -> dict:
    """Find scheduled posts past their ETA and enqueue publish_post."""
    from app.social.tasks.publish import publish_post

    db = SessionLocal()
    enqueued = 0
    try:
        now = datetime.now(timezone.utc)
        due = db.scalars(
            select(SocialPost).where(
                SocialPost.status == SocialPostStatus.SCHEDULED,
                SocialPost.scheduled_at.is_not(None),
                SocialPost.scheduled_at <= now,
            )
        ).all()
        for post in due:
            post.status = SocialPostStatus.PUBLISHING
            db.commit()
            publish_post.delay(str(post.id))
            enqueued += 1
            logger.info("Enqueued due social post %s", post.id)
        return {"enqueued": enqueued}
    finally:
        db.close()
