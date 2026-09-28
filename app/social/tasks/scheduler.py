"""Celery beat: enqueue due scheduled social posts."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.database import get_database
from app.core.mongo_utils import utcnow
from app.social.models import SocialPostStatus
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
        db["social_posts"].update_one(
            {"id": post["id"]},
            {"$set": {"status": SocialPostStatus.PUBLISHING.value, "updated_at": utcnow()}},
        )
        publish_post.delay(str(post["id"]))
        enqueued += 1
        logger.info("Enqueued due social post %s", post["id"])
    return {"enqueued": enqueued}
