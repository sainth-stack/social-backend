"""Celery beat: enqueue due scheduled social posts."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core.database import get_database
from app.social.models import SocialPlatformPostStatus, SocialPostStatus
from workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.social.tasks.scheduler.enqueue_due_social_posts",
    queue="social_publish",
)
def enqueue_due_social_posts() -> dict:
    """Find scheduled posts past their ETA and enqueue publish_post."""
    from app.social.tasks.publish import publish_post

    db = get_database()
    enqueued = 0
    recovered = 0
    now = datetime.now(timezone.utc)
    due = list(
        db["social_posts"].find(
            {
                "status": SocialPostStatus.SCHEDULED.value,
                "scheduled_at": {"$ne": None, "$lte": now},
            }
        )
    )
    for post in due:
        publish_post.delay(str(post["id"]))
        enqueued += 1
        logger.info("Enqueued due social post %s", post["id"])

    stale_cutoff = now - timedelta(minutes=2)
    stuck = list(
        db["social_posts"].find({"status": SocialPostStatus.PUBLISHING.value})
    )
    for post in stuck:
        updated = post.get("updated_at")
        if updated is None:
            continue
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated > stale_cutoff:
            continue
        platforms = list(db["social_post_platforms"].find({"post_id": post["id"]}))
        if not platforms:
            continue
        if any(
            pp.get("status") == SocialPlatformPostStatus.PUBLISHING.value for pp in platforms
        ):
            continue
        if all(
            pp.get("status")
            in (
                SocialPlatformPostStatus.PUBLISHED.value,
                SocialPlatformPostStatus.SKIPPED.value,
            )
            for pp in platforms
        ):
            continue
        publish_post.delay(str(post["id"]))
        recovered += 1
        logger.info("Re-enqueued stuck publishing post %s", post["id"])

    return {"enqueued": enqueued, "recovered": recovered}
