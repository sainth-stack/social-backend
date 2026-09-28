"""Celery task: publish a social post to connected platforms."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core.database import get_database
from app.core.encryption import decrypt
from app.core.mongo_utils import utcnow
from app.social.models import (
    SocialPlatform,
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

PUBLISHABLE = {
    SocialPlatform.FACEBOOK,
    SocialPlatform.INSTAGRAM,
    SocialPlatform.LINKEDIN,
    SocialPlatform.X,
}


@celery_app.task(
    bind=True,
    name="app.social.tasks.publish.publish_post",
    max_retries=0,
    queue="social_publish",
)
def publish_post(self, post_id: str) -> dict:
    """Publish a social post to all platform rows."""
    db = get_database()

    post = db["social_posts"].find_one({"id": str(post_id)})
    if not post:
        logger.warning("publish_post: post %s not found", post_id)
        return {"ok": False, "reason": "not_found"}

    if post.get("status") in (SocialPostStatus.PUBLISHED.value, SocialPostStatus.ARCHIVED.value):
        return {"ok": True, "reason": "already_done"}

    if post.get("status") == SocialPostStatus.PUBLISHING.value:
        platforms_snapshot = list(db["social_post_platforms"].find({"post_id": post_id}))
        any_platform_active = any(
            pp.get("status") == SocialPlatformPostStatus.PUBLISHING.value
            for pp in platforms_snapshot
        )
        updated = post.get("updated_at")
        if updated and any_platform_active:
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - updated
            if age < timedelta(minutes=15):
                logger.info("publish_post: post %s already publishing", post_id)
                return {"ok": True, "reason": "already_publishing"}

    db["social_posts"].update_one(
        {"id": post_id}, {"$set": {"status": SocialPostStatus.PUBLISHING.value, "updated_at": utcnow()}}
    )
    platforms = list(db["social_post_platforms"].find({"post_id": post_id}))
    for pp in platforms:
        if pp.get("status") not in (
            SocialPlatformPostStatus.PUBLISHED.value,
            SocialPlatformPostStatus.SKIPPED.value,
        ):
            db["social_post_platforms"].update_one(
                {"id": pp["id"]},
                {"$set": {"status": SocialPlatformPostStatus.PUBLISHING.value}},
            )

    # Ensure Instagram/Meta can fetch the image
    if post.get("image_url"):
        try:
            from app.social.media import ensure_public_image_url

            public_url = ensure_public_image_url(post["workspace_id"], post["image_url"])
            if public_url and public_url != post["image_url"]:
                db["social_posts"].update_one(
                    {"id": post_id}, {"$set": {"image_url": public_url}}
                )
                post["image_url"] = public_url
        except Exception as exc:
            logger.warning("publish_post: could not promote image for %s: %s", post_id, exc)

    now = datetime.now(timezone.utc)
    any_success = False
    any_failure = False

    for pp in platforms:
        # Re-fetch latest status
        pp = db["social_post_platforms"].find_one({"id": pp["id"]})
        if pp.get("status") == SocialPlatformPostStatus.PUBLISHED.value:
            any_success = True
            continue

        platform_str = pp.get("platform", "")
        try:
            platform_enum = SocialPlatform(platform_str)
        except ValueError:
            db["social_post_platforms"].update_one(
                {"id": pp["id"]},
                {"$set": {
                    "status": SocialPlatformPostStatus.SKIPPED.value,
                    "error_code": "UNSUPPORTED_PLATFORM",
                    "error_message": f"{platform_str} publishing is not available yet",
                }},
            )
            continue

        if platform_enum not in PUBLISHABLE:
            db["social_post_platforms"].update_one(
                {"id": pp["id"]},
                {"$set": {
                    "status": SocialPlatformPostStatus.SKIPPED.value,
                    "error_code": "UNSUPPORTED_PLATFORM",
                    "error_message": f"{platform_str} publishing is not available yet",
                }},
            )
            continue

        account = db["social_accounts"].find_one({"id": pp.get("social_account_id")})
        if not account or not account.get("is_active") or not account.get("access_token_enc"):
            db["social_post_platforms"].update_one(
                {"id": pp["id"]},
                {"$set": {
                    "status": SocialPlatformPostStatus.FAILED.value,
                    "error_code": "ACCOUNT_NOT_FOUND",
                    "error_message": "No connected account for this platform",
                }},
            )
            any_failure = True
            continue

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
                any_failure = True
                continue

        token = decrypt(account["access_token_enc"])
        try:
            publisher = get_publisher(platform_enum)
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

        if result.success:
            db["social_post_platforms"].update_one(
                {"id": pp["id"]},
                {"$set": {
                    "status": SocialPlatformPostStatus.PUBLISHED.value,
                    "platform_post_id": result.platform_post_id,
                    "published_at": now,
                    "error_code": None,
                    "error_message": None,
                    "next_retry_at": None,
                }},
            )
            any_success = True
        else:
            db["social_post_platforms"].update_one(
                {"id": pp["id"]},
                {"$set": {
                    "status": SocialPlatformPostStatus.FAILED.value,
                    "error_code": result.error_code or "API_ERROR",
                    "error_message": result.error_message or "Publish failed",
                }},
            )
            any_failure = True
            pp = db["social_post_platforms"].find_one({"id": pp["id"]})
            if (
                is_retryable_error(pp.get("error_code"), result.retryable)
                and (pp.get("retry_count") or 0) < MAX_RETRIES
            ):
                from app.social.tasks.retry import schedule_platform_retry
                schedule_platform_retry(pp, db)

    if any_success:
        final_status = SocialPostStatus.PUBLISHED.value
        db["social_posts"].update_one(
            {"id": post_id},
            {"$set": {"status": final_status, "published_at": now, "updated_at": utcnow()}},
        )
    elif any_failure:
        db["social_posts"].update_one(
            {"id": post_id},
            {"$set": {"status": SocialPostStatus.FAILED.value, "updated_at": utcnow()}},
        )
    else:
        db["social_posts"].update_one(
            {"id": post_id},
            {"$set": {"status": SocialPostStatus.FAILED.value, "updated_at": utcnow()}},
        )

    post = db["social_posts"].find_one({"id": post_id})

    from app.social.audit import write_social_audit
    write_social_audit(
        db,
        workspace_id=post["workspace_id"],
        action=f"post.{post.get('status')}",
        entity_id=post_id,
        metadata={"post_id": post_id},
    )

    logger.info(
        "publish_post complete post_id=%s status=%s", post_id, post.get("status")
    )
    return {"ok": True, "status": post.get("status")}
