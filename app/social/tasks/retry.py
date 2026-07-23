"""Celery task: retry a failed social post platform row."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import SessionLocal
from app.core.encryption import decrypt
from app.social.models import (
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


@celery_app.task(
    bind=True,
    name="app.social.tasks.retry.retry_failed_post",
    max_retries=0,
    queue="social_publish",
)
def retry_failed_post(self, post_platform_id: str) -> dict:
    """Retry a single failed platform row (manual or auto-scheduled)."""
    db = SessionLocal()
    try:
        pp = db.scalars(
            select(SocialPostPlatform)
            .options(
                selectinload(SocialPostPlatform.social_account),
                selectinload(SocialPostPlatform.post),
            )
            .where(SocialPostPlatform.id == uuid.UUID(post_platform_id))
        ).first()
        if not pp:
            return {"ok": False, "reason": "not_found"}

        if pp.status == SocialPlatformPostStatus.PUBLISHED:
            return {"ok": True, "reason": "already_published"}

        if pp.retry_count >= MAX_RETRIES:
            pp.error_message = (pp.error_message or "") + " (max retries reached)"
            db.commit()
            return {"ok": False, "reason": "max_retries"}

        if not is_retryable_error(pp.error_code, True):
            return {"ok": False, "reason": "non_retryable", "error_code": pp.error_code}

        post = pp.post
        if not post:
            post = db.get(SocialPost, pp.post_id)
        if not post:
            return {"ok": False, "reason": "post_missing"}

        account = pp.social_account
        if not account or not account.access_token_enc:
            pp.status = SocialPlatformPostStatus.FAILED
            pp.error_code = "ACCOUNT_NOT_FOUND"
            pp.error_message = "No connected account for this platform"
            post.status = SocialPostStatus.FAILED
            db.commit()
            return {"ok": False, "reason": "no_account"}

        now = datetime.now(timezone.utc)
        if account.token_expires_at:
            expires = account.token_expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= now:
                pp.status = SocialPlatformPostStatus.FAILED
                pp.error_code = "TOKEN_EXPIRED"
                pp.error_message = "Account token expired — reconnect the account"
                post.status = SocialPostStatus.FAILED
                db.commit()
                return {"ok": False, "reason": "token_expired"}

        pp.status = SocialPlatformPostStatus.PUBLISHING
        pp.next_retry_at = None
        post.status = SocialPostStatus.PUBLISHING
        db.commit()

        if post.image_url:
            try:
                from app.social.media import ensure_public_image_url

                public_url = ensure_public_image_url(post.workspace_id, post.image_url)
                if public_url and public_url != post.image_url:
                    post.image_url = public_url
                    db.commit()
            except Exception as exc:
                logger.warning(
                    "retry_platform_post: image blob promote failed for %s: %s",
                    platform_post_id,
                    exc,
                )

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

        pp.retry_count = (pp.retry_count or 0) + 1

        if result.success:
            pp.status = SocialPlatformPostStatus.PUBLISHED
            pp.platform_post_id = result.platform_post_id
            pp.published_at = now
            pp.error_code = None
            pp.error_message = None
        else:
            pp.status = SocialPlatformPostStatus.FAILED
            pp.error_code = result.error_code or "API_ERROR"
            pp.error_message = result.error_message or "Retry failed"
            if is_retryable_error(pp.error_code, result.retryable) and pp.retry_count < MAX_RETRIES:
                schedule_platform_retry(pp)

        # Recompute parent status
        platforms = db.scalars(
            select(SocialPostPlatform).where(SocialPostPlatform.post_id == post.id)
        ).all()
        statuses = [p.status for p in platforms]
        if all(s == SocialPlatformPostStatus.PUBLISHED for s in statuses):
            post.status = SocialPostStatus.PUBLISHED
            post.published_at = now
        elif any(s == SocialPlatformPostStatus.PUBLISHED for s in statuses) and any(
            s == SocialPlatformPostStatus.FAILED for s in statuses
        ):
            post.status = SocialPostStatus.PUBLISHED
            post.published_at = post.published_at or now
        elif any(s == SocialPlatformPostStatus.FAILED for s in statuses):
            post.status = SocialPostStatus.FAILED
        else:
            post.status = SocialPostStatus.PUBLISHING

        db.commit()
        return {"ok": result.success, "status": post.status.value, "retry_count": pp.retry_count}
    except Exception:
        logger.exception("retry_failed_post failed id=%s", post_platform_id)
        raise
    finally:
        db.close()


def schedule_platform_retry(pp: SocialPostPlatform) -> None:
    """Schedule an ETA retry for a failed platform row."""
    from datetime import timedelta

    from app.social.publishers.base import backoff_seconds

    delay = backoff_seconds(pp.retry_count, pp.error_code)
    eta = datetime.now(timezone.utc) + timedelta(seconds=delay)
    pp.next_retry_at = eta
    try:
        retry_failed_post.apply_async(
            args=[str(pp.id)],
            eta=eta,
            queue="social_publish",
        )
        logger.info(
            "Scheduled retry for platform row %s in %ss (attempt %s)",
            pp.id,
            delay,
            pp.retry_count,
        )
    except Exception as exc:
        logger.warning("Failed to schedule retry for %s: %s", pp.id, exc)
