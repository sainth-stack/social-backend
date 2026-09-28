"""Celery task: retry a failed social post platform row."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.database import get_database
from app.core.encryption import decrypt
from app.core.mongo_utils import utcnow
from app.social.models import (
    SocialPlatformPostStatus,
    SocialPostStatus,
)
from app.social.publishers.base import (
    MAX_RETRIES,
    PublishResult,
    get_publisher,
    is_retryable_error,
)
from workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="app.social.tasks.retry.retry_failed_post",
    max_retries=0,
    queue="social_publish",
)
def retry_failed_post(self, post_platform_id: str) -> dict:
    """Retry a single failed platform row (manual or auto-scheduled)."""
    db = get_database()

    pp = db["social_post_platforms"].find_one({"id": str(post_platform_id)})
    if not pp:
        return {"ok": False, "reason": "not_found"}

    if pp.get("status") == SocialPlatformPostStatus.PUBLISHED.value:
        return {"ok": True, "reason": "already_published"}

    if (pp.get("retry_count") or 0) >= MAX_RETRIES:
        db["social_post_platforms"].update_one(
            {"id": pp["id"]},
            {"$set": {"error_message": (pp.get("error_message") or "") + " (max retries reached)"}},
        )
        return {"ok": False, "reason": "max_retries"}

    if not is_retryable_error(pp.get("error_code"), True):
        return {"ok": False, "reason": "non_retryable", "error_code": pp.get("error_code")}

    post = db["social_posts"].find_one({"id": pp.get("post_id")})
    if not post:
        return {"ok": False, "reason": "post_missing"}

    account = db["social_accounts"].find_one({"id": pp.get("social_account_id")})
    if not account or not account.get("access_token_enc"):
        db["social_post_platforms"].update_one(
            {"id": pp["id"]},
            {"$set": {
                "status": SocialPlatformPostStatus.FAILED.value,
                "error_code": "ACCOUNT_NOT_FOUND",
                "error_message": "No connected account for this platform",
            }},
        )
        db["social_posts"].update_one(
            {"id": post["id"]},
            {"$set": {"status": SocialPostStatus.FAILED.value, "updated_at": utcnow()}},
        )
        return {"ok": False, "reason": "no_account"}

    now = datetime.now(timezone.utc)
    token_expires = account.get("token_expires_at")
    if token_expires:
        if token_expires.tzinfo is None:
            token_expires = token_expires.replace(tzinfo=timezone.utc)
        if token_expires <= now:
            db["social_post_platforms"].update_one(
                {"id": pp["id"]},
                {"$set": {
                    "status": SocialPlatformPostStatus.FAILED.value,
                    "error_code": "TOKEN_EXPIRED",
                    "error_message": "Account token expired — reconnect the account",
                }},
            )
            db["social_posts"].update_one(
                {"id": post["id"]},
                {"$set": {"status": SocialPostStatus.FAILED.value, "updated_at": utcnow()}},
            )
            return {"ok": False, "reason": "token_expired"}

    db["social_post_platforms"].update_one(
        {"id": pp["id"]},
        {"$set": {"status": SocialPlatformPostStatus.PUBLISHING.value, "next_retry_at": None}},
    )
    db["social_posts"].update_one(
        {"id": post["id"]},
        {"$set": {"status": SocialPostStatus.PUBLISHING.value, "updated_at": utcnow()}},
    )

    if post.get("image_url"):
        try:
            from app.social.media import ensure_public_image_url
            public_url = ensure_public_image_url(post["workspace_id"], post["image_url"])
            if public_url and public_url != post["image_url"]:
                db["social_posts"].update_one({"id": post["id"]}, {"$set": {"image_url": public_url}})
                post["image_url"] = public_url
        except Exception as exc:
            logger.warning("retry_platform_post: image blob promote failed for %s: %s", post_platform_id, exc)

    token = decrypt(account["access_token_enc"])
    from app.social.models import SocialPlatform
    try:
        publisher = get_publisher(SocialPlatform(pp.get("platform")))
        result = publisher.publish(
            platform_account_id=account["platform_account_id"],
            access_token=token,
            caption=pp.get("caption") or "",
            hashtags=list(pp.get("hashtags") or []),
            image_url=post.get("image_url"),
            first_comment=pp.get("first_comment"),
        )
    except ValueError as exc:
        result = PublishResult(
            success=False, error_code="UNSUPPORTED_PLATFORM", error_message=str(exc)
        )

    new_retry_count = (pp.get("retry_count") or 0) + 1
    db["social_post_platforms"].update_one(
        {"id": pp["id"]}, {"$set": {"retry_count": new_retry_count}}
    )

    if result.success:
        db["social_post_platforms"].update_one(
            {"id": pp["id"]},
            {"$set": {
                "status": SocialPlatformPostStatus.PUBLISHED.value,
                "platform_post_id": result.platform_post_id,
                "published_at": now,
                "error_code": None,
                "error_message": None,
            }},
        )
    else:
        db["social_post_platforms"].update_one(
            {"id": pp["id"]},
            {"$set": {
                "status": SocialPlatformPostStatus.FAILED.value,
                "error_code": result.error_code or "API_ERROR",
                "error_message": result.error_message or "Retry failed",
            }},
        )
        pp = db["social_post_platforms"].find_one({"id": pp["id"]})
        if is_retryable_error(pp.get("error_code"), result.retryable) and new_retry_count < MAX_RETRIES:
            schedule_platform_retry(pp, db)

    # Recompute parent status
    all_platforms = list(db["social_post_platforms"].find({"post_id": post["id"]}))
    statuses = [p.get("status") for p in all_platforms]
    if all(s == SocialPlatformPostStatus.PUBLISHED.value for s in statuses):
        db["social_posts"].update_one(
            {"id": post["id"]},
            {"$set": {"status": SocialPostStatus.PUBLISHED.value, "published_at": now, "updated_at": utcnow()}},
        )
    elif any(s == SocialPlatformPostStatus.PUBLISHED.value for s in statuses) and any(
        s == SocialPlatformPostStatus.FAILED.value for s in statuses
    ):
        db["social_posts"].update_one(
            {"id": post["id"]},
            {"$set": {
                "status": SocialPostStatus.PUBLISHED.value,
                "published_at": post.get("published_at") or now,
                "updated_at": utcnow(),
            }},
        )
    elif any(s == SocialPlatformPostStatus.FAILED.value for s in statuses):
        db["social_posts"].update_one(
            {"id": post["id"]},
            {"$set": {"status": SocialPostStatus.FAILED.value, "updated_at": utcnow()}},
        )
    else:
        db["social_posts"].update_one(
            {"id": post["id"]},
            {"$set": {"status": SocialPostStatus.PUBLISHING.value, "updated_at": utcnow()}},
        )

    post = db["social_posts"].find_one({"id": post["id"]})
    return {"ok": result.success, "status": post.get("status"), "retry_count": new_retry_count}


def schedule_platform_retry(pp: dict, db=None) -> None:
    """Schedule an ETA retry for a failed platform row."""
    from datetime import timedelta
    from app.social.publishers.base import backoff_seconds

    if db is None:
        db = get_database()

    delay = backoff_seconds(pp.get("retry_count") or 0, pp.get("error_code"))
    eta = datetime.now(timezone.utc) + timedelta(seconds=delay)
    db["social_post_platforms"].update_one({"id": pp["id"]}, {"$set": {"next_retry_at": eta}})
    try:
        retry_failed_post.apply_async(args=[str(pp["id"])], eta=eta, queue="social_publish")
        logger.info(
            "Scheduled retry for platform row %s in %ss (attempt %s)",
            pp["id"], delay, pp.get("retry_count"),
        )
    except Exception as exc:
        logger.warning("Failed to schedule retry for %s: %s", pp.get("id"), exc)
