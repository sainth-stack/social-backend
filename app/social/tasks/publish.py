"""Celery task: publish a social post to connected platforms."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import SessionLocal
from app.core.encryption import decrypt
from app.social.models import (
    SocialPlatform,
    SocialPlatformPostStatus,
    SocialPost,
    SocialPostPlatform,
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
    db = SessionLocal()
    try:
        post = db.scalars(
            select(SocialPost)
            .options(
                selectinload(SocialPost.platforms).selectinload(SocialPostPlatform.social_account)
            )
            .where(SocialPost.id == uuid.UUID(post_id))
        ).first()
        if not post:
            logger.warning("publish_post: post %s not found", post_id)
            return {"ok": False, "reason": "not_found"}

        if post.status in (SocialPostStatus.PUBLISHED, SocialPostStatus.ARCHIVED):
            return {"ok": True, "reason": "already_done"}

        if post.status == SocialPostStatus.PUBLISHING:
            updated = post.updated_at
            if updated:
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - updated
                if age < timedelta(minutes=15):
                    logger.info("publish_post: post %s already publishing", post_id)
                    return {"ok": True, "reason": "already_publishing"}
            # Stale publishing lock — allow retry below

        post.status = SocialPostStatus.PUBLISHING
        for pp in post.platforms:
            if pp.status not in (
                SocialPlatformPostStatus.PUBLISHED,
                SocialPlatformPostStatus.SKIPPED,
            ):
                pp.status = SocialPlatformPostStatus.PUBLISHING

        # Ensure Instagram/Meta can fetch the image (data: URIs → Azure Blob SAS URL).
        if post.image_url:
            try:
                from app.social.media import ensure_public_image_url

                public_url = ensure_public_image_url(post.workspace_id, post.image_url)
                if public_url and public_url != post.image_url:
                    post.image_url = public_url
            except Exception as exc:
                logger.warning(
                    "publish_post: could not promote image to Azure Blob for %s: %s",
                    post_id,
                    exc,
                )

        db.commit()

        now = datetime.now(timezone.utc)
        any_success = False
        any_failure = False

        for pp in list(post.platforms):
            if pp.status == SocialPlatformPostStatus.PUBLISHED:
                any_success = True
                continue

            if pp.platform not in PUBLISHABLE:
                pp.status = SocialPlatformPostStatus.SKIPPED
                pp.error_code = "UNSUPPORTED_PLATFORM"
                pp.error_message = f"{pp.platform.value} publishing is not available yet"
                continue

            account = pp.social_account
            if not account or not account.is_active or not account.access_token_enc:
                pp.status = SocialPlatformPostStatus.FAILED
                pp.error_code = "ACCOUNT_NOT_FOUND"
                pp.error_message = "No connected account for this platform"
                any_failure = True
                continue

            if account.token_expires_at:
                expires = account.token_expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= now:
                    pp.status = SocialPlatformPostStatus.FAILED
                    pp.error_code = "TOKEN_EXPIRED"
                    pp.error_message = "Account token expired — reconnect the account"
                    any_failure = True
                    continue

            token = decrypt(account.access_token_enc)
            try:
                publisher = get_publisher(pp.platform)
                result = publisher.publish(
                    platform_account_id=account.platform_account_id,
                    access_token=token,
                    caption=pp.caption or "",
                    hashtags=list(pp.hashtags or []),
                    image_url=post.image_url,
                    first_comment=pp.first_comment,
                )
            except ValueError as exc:
                result = PublishResult(
                    success=False,
                    error_code="UNSUPPORTED_PLATFORM",
                    error_message=str(exc),
                )

            if result.success:
                pp.status = SocialPlatformPostStatus.PUBLISHED
                pp.platform_post_id = result.platform_post_id
                pp.published_at = now
                pp.error_code = None
                pp.error_message = None
                pp.next_retry_at = None
                any_success = True
            else:
                pp.status = SocialPlatformPostStatus.FAILED
                pp.error_code = result.error_code or "API_ERROR"
                pp.error_message = result.error_message or "Publish failed"
                any_failure = True
                if (
                    is_retryable_error(pp.error_code, result.retryable)
                    and (pp.retry_count or 0) < MAX_RETRIES
                ):
                    from app.social.tasks.retry import schedule_platform_retry

                    schedule_platform_retry(pp)

        if any_success and not any_failure:
            post.status = SocialPostStatus.PUBLISHED
            post.published_at = now
        elif any_success and any_failure:
            post.status = SocialPostStatus.PUBLISHED
            post.published_at = now
        elif any_failure:
            post.status = SocialPostStatus.FAILED
        else:
            post.status = SocialPostStatus.FAILED

        from app.social.audit import write_social_audit

        write_social_audit(
            db,
            workspace_id=post.workspace_id,
            action=f"post.{post.status.value}",
            entity_id=post.id,
            metadata={"post_id": post_id},
        )
        db.commit()
        logger.info(
            "publish_post complete post_id=%s status=%s",
            post_id,
            post.status.value,
        )
        return {"ok": True, "status": post.status.value}
    except Exception:
        logger.exception("publish_post failed post_id=%s", post_id)
        try:
            post = db.get(SocialPost, uuid.UUID(post_id))
            if post and post.status == SocialPostStatus.PUBLISHING:
                post.status = SocialPostStatus.FAILED
                db.commit()
        except Exception:
            db.rollback()
        raise
    finally:
        db.close()
