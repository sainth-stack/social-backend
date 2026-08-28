"""Org-scoped business logic for Social Media accounts and posts."""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.encryption import decrypt, encrypt
from app.workspaces.models import Workspace
from app.users.models import User
from app.social.ai.generator import generate_brand_voice_sample, generate_platform_content
from app.social.ai.image_generator import generate_post_image_from_url
from app.social.models import (
    SocialAccount,
    SocialBrandVoice,
    SocialImageSource,
    SocialMediaAsset,
    SocialMediaAssetType,
    SocialPlatform,
    SocialPlatformPostStatus,
    SocialPost,
    SocialPostPlatform,
    SocialPostStatus,
)

PUBLISHABLE_PLATFORMS = {
    SocialPlatform.FACEBOOK,
    SocialPlatform.INSTAGRAM,
    SocialPlatform.LINKEDIN,
    SocialPlatform.X,
}
from app.social.oauth.base import OAuthAccountProfile, generate_state_token, get_oauth_handler
from app.social.schemas import (
    BrandVoiceOut,
    BrandVoiceTestResponse,
    BrandVoiceUpdateRequest,
    CalendarPostOut,
    CalendarResponse,
    CreateSocialAccountRequest,
    CreateSocialPostRequest,
    GenerateImageResponse,
    GeneratePostRequest,
    GeneratePostResponse,
    GeneratedPlatformContent,
    GeneratedSlide,
    GeneratedTweet,
    MediaAssetListParams,
    MediaAssetListResponse,
    MediaAssetOut,
    SchedulePostRequest,
    SocialAccountOut,
    SocialPostListParams,
    SocialPostListResponse,
    SocialPostOut,
    SocialPostPlatformOut,
    UpdateSocialAccountRequest,
    UpdateSocialPostRequest,
)
from workers.redis.client import get_redis_client

OAUTH_STATE_TTL_SECONDS = 600
TOKEN_EXPIRES_SOON_DAYS = 7


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _token_status(account: SocialAccount) -> str:
    if not account.is_active or not account.access_token_enc:
        return "disconnected"
    if not account.token_expires_at:
        return "active"
    expires = account.token_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if expires <= now:
        return "expired"
    if expires <= now + timedelta(days=TOKEN_EXPIRES_SOON_DAYS):
        return "expires_soon"
    return "active"


class SocialMediaService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── Accounts ──────────────────────────────────────────────────────────────

    def list_accounts(
        self,
        workspace: Workspace,
        platform: Optional[SocialPlatform] = None,
    ) -> list[SocialAccountOut]:
        stmt = select(SocialAccount).where(SocialAccount.workspace_id == workspace.id)
        if platform is not None:
            stmt = stmt.where(SocialAccount.platform == platform)
        stmt = stmt.order_by(SocialAccount.platform, SocialAccount.account_name)
        rows = self.db.scalars(stmt).all()
        return [self._serialize_account(a) for a in rows]

    def create_account(
        self,
        workspace: Workspace,
        payload: CreateSocialAccountRequest,
    ) -> SocialAccountOut:
        account = self._upsert_account(
            workspace_id=workspace.id,
            platform=payload.platform,
            profile=OAuthAccountProfile(
                platform_account_id=payload.platformAccountId,
                account_name=payload.accountName,
                account_type=payload.accountType,
                account_picture_url=payload.accountPictureUrl,
                follower_count=payload.followerCount,
                access_token=payload.accessToken,
                refresh_token=payload.refreshToken,
                token_expires_at=_parse_dt(payload.tokenExpiresAt),
            ),
            is_default=payload.isDefault,
        )
        self.db.commit()
        self.db.refresh(account)
        return self._serialize_account(account)

    def update_account(
        self,
        account: SocialAccount,
        payload: UpdateSocialAccountRequest,
    ) -> SocialAccountOut:
        if payload.accountName is not None:
            account.account_name = payload.accountName
        if payload.isActive is not None:
            account.is_active = payload.isActive
            if not payload.isActive:
                account.access_token_enc = None
                account.refresh_token_enc = None
        if payload.isDefault is True:
            self._clear_default(account.workspace_id, account.platform)
            account.is_default = True
        elif payload.isDefault is False:
            account.is_default = False
        self.db.commit()
        self.db.refresh(account)
        return self._serialize_account(account)

    def delete_account(self, account: SocialAccount) -> None:
        account.is_active = False
        account.access_token_enc = None
        account.refresh_token_enc = None
        account.is_default = False
        self.db.commit()

    def sync_account(self, account: SocialAccount) -> SocialAccountOut:
        try:
            get_oauth_handler(account.platform)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        if not account.access_token_enc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Account is disconnected — reconnect before syncing",
            )
        handler = get_oauth_handler(account.platform)
        token = decrypt(account.access_token_enc)

        # Refresh expired / near-expiry tokens when a refresh_token is available (X, LinkedIn).
        token = self._ensure_fresh_token(account, handler, token)

        try:
            stats = handler.sync_account_stats(account.platform_account_id, token)
        except Exception as first_exc:
            # One retry after forced refresh on auth failures.
            refreshed = self._force_refresh_token(account, handler)
            if not refreshed:
                raise
            token = refreshed
            try:
                stats = handler.sync_account_stats(account.platform_account_id, token)
            except Exception:
                raise first_exc from None

        if stats.get("account_name"):
            account.account_name = stats["account_name"]
        if stats.get("account_picture_url") is not None:
            account.account_picture_url = stats["account_picture_url"]
        if stats.get("follower_count") is not None:
            account.follower_count = int(stats["follower_count"])
        account.last_synced_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(account)

        # Keep analytics daily row in sync so Audience / Overview charts update.
        try:
            from app.social.analytics.sync import sync_account_daily

            sync_account_daily(self.db, account)
            self.db.commit()
            self.db.refresh(account)
        except Exception as exc:
            logger.warning("Analytics daily sync after account sync failed: %s", exc)

        return self._serialize_account(account)

    def _ensure_fresh_token(self, account: SocialAccount, handler, token: str) -> str:
        expires = account.token_expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        needs_refresh = bool(
            expires and expires <= datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        if not needs_refresh:
            return token
        refreshed = self._force_refresh_token(account, handler)
        return refreshed or token

    def _force_refresh_token(self, account: SocialAccount, handler) -> Optional[str]:
        if not account.refresh_token_enc:
            return None
        try:
            refresh = decrypt(account.refresh_token_enc)
            payload = handler.refresh_access_token(refresh)
            if not payload or not payload.get("access_token"):
                return None
            account.access_token_enc = encrypt(payload["access_token"])
            if payload.get("refresh_token"):
                account.refresh_token_enc = encrypt(str(payload["refresh_token"]))
            expires_in = int(payload.get("expires_in") or 0)
            if expires_in > 0:
                account.token_expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=expires_in
                )
            self.db.commit()
            self.db.refresh(account)
            logger.info("Refreshed OAuth token for %s account %s", account.platform.value, account.id)
            return payload["access_token"]
        except Exception as exc:
            logger.warning(
                "Token refresh failed for %s %s: %s",
                account.platform.value,
                account.id,
                exc,
            )
            return None

    def get_oauth_url(
        self,
        workspace: Workspace,
        platform: SocialPlatform,
        *,
        reconnect_account_id: Optional[uuid.UUID] = None,
    ) -> str:
        from app.social.limits import enforce_account_limit

        enforce_account_limit(self.db, workspace)
        try:
            handler = get_oauth_handler(platform)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        state = generate_state_token()
        payload = {
            "workspace_id": str(workspace.id),
            "platform": platform.value,
            "reconnect_account_id": str(reconnect_account_id) if reconnect_account_id else None,
        }
        redis = get_redis_client()
        redis.setex(
            f"social_oauth_state:{state}",
            OAUTH_STATE_TTL_SECONDS,
            json.dumps(payload),
        )
        return handler.build_authorization_url(state)

    def handle_oauth_callback(
        self,
        platform: SocialPlatform,
        code: str,
        state: str,
    ) -> list[SocialAccountOut]:
        redis = get_redis_client()
        raw = redis.get(f"social_oauth_state:{state}")
        if not raw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired OAuth state",
            )
        redis.delete(f"social_oauth_state:{state}")

        data = json.loads(raw)
        if data.get("platform") != platform.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="OAuth platform mismatch",
            )

        workspace_id = uuid.UUID(data["workspace_id"])
        reconnect_id = data.get("reconnect_account_id")
        handler = get_oauth_handler(platform)
        result = handler.exchange_code(code, state=state)

        accounts: list[SocialAccount] = []
        if reconnect_id and result.accounts:
            existing = self.db.get(SocialAccount, uuid.UUID(reconnect_id))
            if not existing or existing.workspace_id != workspace_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Account to reconnect was not found",
                )
            profile = next(
                (
                    p
                    for p in result.accounts
                    if p.platform_account_id == existing.platform_account_id
                ),
                result.accounts[0],
            )
            accounts.append(self._apply_profile(existing, profile, reactivate=True))
        else:
            for profile in result.accounts:
                accounts.append(
                    self._upsert_account(workspace_id=workspace_id, platform=platform, profile=profile)
                )

        # Ensure one default per platform when none exists.
        for account in accounts:
            has_default = self.db.scalars(
                select(SocialAccount).where(
                    SocialAccount.workspace_id == workspace_id,
                    SocialAccount.platform == platform,
                    SocialAccount.is_default.is_(True),
                    SocialAccount.is_active.is_(True),
                )
            ).first()
            if not has_default:
                account.is_default = True

        self.db.commit()
        for account in accounts:
            self.db.refresh(account)
        return [self._serialize_account(a) for a in accounts]

    # ── Posts ─────────────────────────────────────────────────────────────────

    def list_posts(
        self,
        workspace: Workspace,
        params: SocialPostListParams,
    ) -> SocialPostListResponse:
        filters = [SocialPost.workspace_id == workspace.id]
        if params.status is not None:
            filters.append(SocialPost.status == params.status)
        if params.search:
            term = f"%{params.search.strip()}%"
            filters.append(
                or_(
                    SocialPost.title.ilike(term),
                    SocialPost.ai_prompt.ilike(term),
                    SocialPost.platforms.any(SocialPostPlatform.caption.ilike(term)),
                )
            )

        total = self.db.scalar(
            select(func.count()).select_from(SocialPost).where(*filters)
        ) or 0
        page_size = params.pageSize
        page = params.page
        total_pages = max(1, math.ceil(total / page_size)) if total else 0

        rows = self.db.scalars(
            select(SocialPost)
            .options(selectinload(SocialPost.platforms))
            .where(*filters)
            .order_by(SocialPost.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()

        return SocialPostListResponse(
            items=[self._serialize_post(p) for p in rows],
            total=total,
            page=page,
            pageSize=page_size,
            totalPages=total_pages,
        )

    def get_post(self, post: SocialPost) -> SocialPostOut:
        self.db.refresh(post, attribute_names=["platforms"])
        return self._serialize_post(post)

    def create_post(
        self,
        workspace: Workspace,
        user: User,
        payload: CreateSocialPostRequest,
    ) -> SocialPostOut:
        from app.social.audit import write_social_audit
        from app.social.limits import enforce_posts_limit
        from app.social.permissions import require_permission
        from app.social.models import SocialPermission

        require_permission(self.db, workspace, user, SocialPermission.EDITOR)
        enforce_posts_limit(self.db, workspace)
        from app.social.media import ensure_public_image_url

        image_url = ensure_public_image_url(workspace.id, payload.imageUrl)
        template_id = None
        if payload.templateId:
            try:
                template_id = uuid.UUID(payload.templateId)
            except ValueError:
                template_id = None
        post = SocialPost(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            created_by=user.id,
            title=payload.title,
            status=payload.status,
            scheduled_at=_parse_dt(payload.scheduledAt),
            image_url=image_url,
            image_source=payload.imageSource,
            ai_prompt=payload.aiPrompt,
            template_id=template_id,
        )
        self.db.add(post)
        self.db.flush()
        self._replace_platforms(post, workspace.id, payload.platforms)
        write_social_audit(
            self.db,
            workspace_id=workspace.id,
            user_id=user.id,
            action="post.created",
            entity_id=post.id,
            metadata={"title": post.title},
        )
        self.db.commit()
        self.db.refresh(post)
        return self._serialize_post(post)

    def update_post(
        self,
        post: SocialPost,
        workspace: Workspace,
        payload: UpdateSocialPostRequest,
    ) -> SocialPostOut:
        previous_status = post.status
        previous_scheduled = post.scheduled_at

        if payload.title is not None:
            post.title = payload.title
        if payload.status is not None:
            # Cancel schedule
            if (
                payload.status == SocialPostStatus.DRAFT
                and post.status == SocialPostStatus.SCHEDULED
            ):
                post.scheduled_at = None
            if payload.status == SocialPostStatus.SCHEDULED:
                self.db.refresh(post, attribute_names=["platforms"])
                self._validate_ready_to_publish(post)
            post.status = payload.status
        if payload.scheduledAt is not None:
            new_scheduled = _parse_dt(payload.scheduledAt)
            post.scheduled_at = new_scheduled
            becoming_scheduled = (
                payload.status == SocialPostStatus.SCHEDULED
                or post.status == SocialPostStatus.SCHEDULED
            )
            if becoming_scheduled and new_scheduled:
                if new_scheduled <= datetime.now(timezone.utc):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="scheduledAt must be in the future",
                    )
                self.db.refresh(post, attribute_names=["platforms"])
                self._validate_ready_to_publish(post)
        if payload.imageUrl is not None:
            from app.social.media import ensure_public_image_url

            post.image_url = ensure_public_image_url(workspace.id, payload.imageUrl)
        if payload.imageSource is not None:
            post.image_source = payload.imageSource
        if payload.aiPrompt is not None:
            post.ai_prompt = payload.aiPrompt
        if payload.templateId is not None:
            try:
                post.template_id = uuid.UUID(payload.templateId) if payload.templateId else None
            except ValueError:
                pass
        if payload.platforms is not None:
            self._replace_platforms(post, workspace.id, payload.platforms)
        self.db.commit()
        self.db.refresh(post)

        # Reschedule ETA when scheduled_at changes on a scheduled post
        if (
            post.status == SocialPostStatus.SCHEDULED
            and post.scheduled_at
            and post.scheduled_at != previous_scheduled
        ):
            self._enqueue_publish(post.id, eta=post.scheduled_at)
        elif (
            previous_status == SocialPostStatus.SCHEDULED
            and post.status == SocialPostStatus.DRAFT
        ):
            pass  # cancelled — beat tick will ignore non-scheduled posts

        return self._serialize_post(post)

    def regenerate_post_content(
        self,
        post: SocialPost,
        workspace: Workspace,
        user: User,
        *,
        prompt: Optional[str] = None,
        regenerate_image: bool = True,
    ) -> SocialPostOut:
        from app.plans.service import record_ai_usage
        from app.social.ai.image_generator import generate_post_image
        from app.social.limits import enforce_ai_image_limit, enforce_ai_text_limit
        from app.social.media import upload_social_image_bytes
        from app.social.polish import SocialPolishService
        from app.social.schemas import SocialPostPlatformIn, UpdateSocialPostRequest

        self.db.refresh(post, attribute_names=["platforms"])
        if not post.platforms:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Post has no platforms to regenerate",
            )

        platform_row = post.platforms[0]
        platform = platform_row.platform
        topic = (prompt or post.ai_prompt or post.title or "").strip()
        if len(topic) < 5:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Add a prompt or save a brief on this post before regenerating",
            )

        settings = SocialPolishService(self.db).get_settings(workspace)
        brand_voice = self._brand_voice_dict(workspace.id)
        tone = settings.get("defaultTone") or ((brand_voice.get("tones") or ["Professional"])[0])
        cta = settings.get("defaultCta") or None
        audience = brand_voice.get("target_audience")

        enforce_ai_text_limit(self.db, workspace)
        record_ai_usage(self.db, workspace.id, "text", user_id=user.id)
        self.db.commit()

        result = generate_platform_content(
            topic=topic,
            tone=tone,
            platforms=[platform.value],
            audience=audience,
            cta=cta,
            include_hashtags=True,
            include_comment=False,
            brand_voice=brand_voice,
        )
        pc = result.get(platform.value) or {}
        caption = (pc.get("caption") or "").strip()
        hashtags = list(pc.get("hashtags") or [])
        if not caption:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Regeneration produced empty caption",
            )

        update_kwargs: dict = {
            "platforms": [
                SocialPostPlatformIn(
                    platform=platform,
                    socialAccountId=str(platform_row.social_account_id),
                    caption=caption,
                    hashtags=hashtags,
                )
            ],
        }
        if prompt:
            update_kwargs["aiPrompt"] = prompt.strip()

        if regenerate_image:
            try:
                enforce_ai_image_limit(self.db, workspace)
                record_ai_usage(self.db, workspace.id, "image", user_id=user.id)
                self.db.commit()
                image_style = settings.get("imageGenerationStyle")
                img_data = generate_post_image(
                    topic=f"Social media image for: {caption[:180]}",
                    style=image_style,
                    size="1024x1024",
                    mode="create",
                )
                upload = None
                if img_data.get("imageB64"):
                    upload = upload_social_image_bytes(
                        workspace.id,
                        img_data["imageB64"],  # type: ignore[arg-type]
                        content_type="image/png",
                    )
                if upload:
                    self._record_media_asset(
                        workspace,
                        user,
                        media_type=SocialMediaAssetType.IMAGE,
                        source=SocialImageSource.AI_GENERATED,
                        blob_key=upload.blob_key,
                        blob_url=upload.url,
                        mime_type=upload.content_type,
                        file_size_bytes=upload.file_size,
                        prompt=topic,
                    )
                    update_kwargs["imageUrl"] = upload.url
                    update_kwargs["imageSource"] = SocialImageSource.AI_GENERATED
            except Exception as exc:
                logger.warning("Regenerate image failed for post %s: %s", post.id, exc)

        return self.update_post(post, workspace, UpdateSocialPostRequest(**update_kwargs))

    def delete_post(self, post: SocialPost) -> None:
        if post.status not in (SocialPostStatus.DRAFT, SocialPostStatus.ARCHIVED, SocialPostStatus.FAILED):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only draft, failed, or archived posts can be deleted",
            )
        self.db.delete(post)
        self.db.commit()

    def schedule_post(
        self,
        post: SocialPost,
        payload: SchedulePostRequest,
    ) -> SocialPostOut:
        self.db.refresh(post, attribute_names=["platforms"])
        self._validate_ready_to_publish(post)
        scheduled_at = _parse_dt(payload.scheduledAt)
        if not scheduled_at or scheduled_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scheduledAt must be a future datetime",
            )
        if post.status not in (
            SocialPostStatus.DRAFT,
            SocialPostStatus.SCHEDULED,
            SocialPostStatus.FAILED,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot schedule a post in status {post.status.value}",
            )
        post.scheduled_at = scheduled_at
        post.status = SocialPostStatus.SCHEDULED
        for pp in post.platforms:
            if pp.status != SocialPlatformPostStatus.PUBLISHED:
                pp.status = SocialPlatformPostStatus.PENDING
                pp.error_code = None
                pp.error_message = None
        self.db.commit()
        self.db.refresh(post)
        self._enqueue_publish(post.id, eta=scheduled_at)
        return self._serialize_post(post)

    def publish_now(self, post: SocialPost) -> SocialPostOut:
        self.db.refresh(post, attribute_names=["platforms"])
        self._validate_ready_to_publish(post)
        if post.status in (SocialPostStatus.PUBLISHING, SocialPostStatus.PUBLISHED):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Post is already {post.status.value}",
            )
        post.status = SocialPostStatus.PUBLISHING
        post.scheduled_at = post.scheduled_at or datetime.now(timezone.utc)
        for pp in post.platforms:
            if pp.status != SocialPlatformPostStatus.PUBLISHED:
                pp.status = SocialPlatformPostStatus.PENDING
                pp.error_code = None
                pp.error_message = None
        self.db.commit()
        self.db.refresh(post)
        self._enqueue_publish(post.id, eta=None)
        return self._serialize_post(post)

    def archive_post(self, post: SocialPost) -> SocialPostOut:
        if post.status not in (
            SocialPostStatus.PUBLISHED,
            SocialPostStatus.FAILED,
            SocialPostStatus.DRAFT,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only published, failed, or draft posts can be archived",
            )
        post.status = SocialPostStatus.ARCHIVED
        self.db.commit()
        self.db.refresh(post)
        return self._serialize_post(post)

    def retry_post(self, post: SocialPost) -> "RetryResponse":
        from app.social.publishers.base import MAX_RETRIES, is_retryable_error
        from app.social.schemas import RetryResponse
        from app.social.tasks.retry import retry_failed_post

        self.db.refresh(post, attribute_names=["platforms"])
        if post.status not in (SocialPostStatus.FAILED, SocialPostStatus.PUBLISHED):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only failed (or partially failed) posts can be retried",
            )

        retried = 0
        skipped = 0
        for pp in post.platforms:
            if pp.status != SocialPlatformPostStatus.FAILED:
                continue
            if not is_retryable_error(pp.error_code, True):
                skipped += 1
                continue
            if (pp.retry_count or 0) >= MAX_RETRIES:
                skipped += 1
                continue
            pp.status = SocialPlatformPostStatus.PENDING
            pp.next_retry_at = None
            retried += 1
            try:
                retry_failed_post.apply_async(args=[str(pp.id)], queue="social_publish")
            except Exception:
                retry_failed_post.run(str(pp.id))

        if retried == 0 and skipped > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "No retryable platform failures. "
                    "TOKEN_EXPIRED / INVALID_IMAGE require reconnect or content changes."
                ),
            )
        if retried == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No failed platforms to retry",
            )

        post.status = SocialPostStatus.PUBLISHING
        self.db.commit()
        self.db.refresh(post)
        return RetryResponse(
            post=self._serialize_post(post),
            retriedPlatforms=retried,
            skippedPlatforms=skipped,
        )

    def bulk_retry(self, workspace: Workspace, post_ids: list[str]) -> "BulkRetryResponse":
        from app.social.schemas import BulkRetryResponse, RetryResponse

        items: list[RetryResponse] = []
        total = 0
        for pid in post_ids:
            post = self.db.get(SocialPost, uuid.UUID(pid))
            if not post or post.workspace_id != workspace.id:
                continue
            try:
                result = self.retry_post(post)
                items.append(result)
                total += result.retriedPlatforms
            except HTTPException:
                continue
        return BulkRetryResponse(items=items, totalRetried=total)

    def cancel_schedule(self, post: SocialPost) -> SocialPostOut:
        if post.status != SocialPostStatus.SCHEDULED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only scheduled posts can be cancelled",
            )
        post.status = SocialPostStatus.DRAFT
        post.scheduled_at = None
        self.db.commit()
        self.db.refresh(post)
        return self._serialize_post(post)

    def analytics_overview(
        self,
        workspace: Workspace,
        from_date: Optional[str],
        to_date: Optional[str],
    ):
        from app.social.analytics.aggregator import AnalyticsAggregator
        from app.social.schemas import AnalyticsOverviewOut

        data = AnalyticsAggregator(self.db).overview(workspace.id, from_date, to_date)
        return AnalyticsOverviewOut(**data)

    def analytics_platform(
        self,
        workspace: Workspace,
        platform: SocialPlatform,
        from_date: Optional[str],
        to_date: Optional[str],
    ):
        from app.social.analytics.aggregator import AnalyticsAggregator
        from app.social.schemas import PlatformAnalyticsOut

        data = AnalyticsAggregator(self.db).platform(
            workspace.id, platform, from_date, to_date
        )
        return PlatformAnalyticsOut(**data)

    def analytics_posts(
        self,
        workspace: Workspace,
        from_date: Optional[str],
        to_date: Optional[str],
        sort: str = "engagementRate",
        order: str = "desc",
    ):
        from app.social.analytics.aggregator import AnalyticsAggregator
        from app.social.schemas import PostPerformanceOut

        data = AnalyticsAggregator(self.db).posts(
            workspace.id, from_date, to_date, sort=sort, order=order
        )
        return PostPerformanceOut(**data)

    def analytics_audience(
        self,
        workspace: Workspace,
        from_date: Optional[str],
        to_date: Optional[str],
    ):
        from app.social.analytics.aggregator import AnalyticsAggregator
        from app.social.schemas import AudienceGrowthOut

        data = AnalyticsAggregator(self.db).audience(workspace.id, from_date, to_date)
        return AudienceGrowthOut(**data)

    def calendar(
        self,
        workspace: Workspace,
        month: str,
    ) -> CalendarResponse:
        try:
            year_s, month_s = month.split("-")
            year, mon = int(year_s), int(month_s)
            start = datetime(year, mon, 1, tzinfo=timezone.utc)
            if mon == 12:
                end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                end = datetime(year, mon + 1, 1, tzinfo=timezone.utc)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="month must be YYYY-MM",
            ) from exc

        rows = self.db.scalars(
            select(SocialPost)
            .options(selectinload(SocialPost.platforms))
            .where(
                SocialPost.workspace_id == workspace.id,
                SocialPost.scheduled_at.is_not(None),
                SocialPost.scheduled_at >= start,
                SocialPost.scheduled_at < end,
                SocialPost.status.in_(
                    [
                        SocialPostStatus.DRAFT,
                        SocialPostStatus.SCHEDULED,
                        SocialPostStatus.PUBLISHING,
                        SocialPostStatus.PUBLISHED,
                        SocialPostStatus.FAILED,
                    ]
                ),
            )
            .order_by(SocialPost.scheduled_at.asc())
        ).all()

        items: list[CalendarPostOut] = []
        for post in rows:
            preview = ""
            platforms: list[SocialPlatform] = []
            for pp in post.platforms or []:
                platforms.append(pp.platform)
                if not preview and pp.caption:
                    preview = pp.caption[:80]
            items.append(
                CalendarPostOut(
                    id=str(post.id),
                    title=post.title,
                    status=post.status,
                    scheduledAt=_iso(post.scheduled_at),
                    publishedAt=_iso(post.published_at),
                    platforms=platforms,
                    captionPreview=preview or post.title or "",
                    imageUrl=post.image_url,
                )
            )
        return CalendarResponse(month=month, items=items)

    def duplicate_post(
        self,
        post: SocialPost,
        user: User,
        workspace: Workspace,
    ) -> SocialPostOut:
        from app.social.audit import write_social_audit
        from app.social.limits import enforce_posts_limit

        enforce_posts_limit(self.db, workspace)
        self.db.refresh(post, attribute_names=["platforms"])
        clone = SocialPost(
            id=uuid.uuid4(),
            workspace_id=post.workspace_id,
            created_by=user.id,
            title=f"{post.title} (copy)" if post.title else "Untitled draft",
            status=SocialPostStatus.DRAFT,
            scheduled_at=None,
            published_at=None,
            ai_prompt=post.ai_prompt,
            image_url=post.image_url,
            image_source=post.image_source,
        )
        self.db.add(clone)
        self.db.flush()
        for pp in post.platforms or []:
            clone.platforms.append(
                SocialPostPlatform(
                    id=uuid.uuid4(),
                    platform=pp.platform,
                    social_account_id=pp.social_account_id,
                    caption=pp.caption,
                    hashtags=list(pp.hashtags or []),
                    first_comment=pp.first_comment,
                    character_count=pp.character_count,
                )
            )
        write_social_audit(
            self.db,
            workspace_id=workspace.id,
            user_id=user.id,
            action="post.created",
            entity_id=clone.id,
            metadata={"title": clone.title, "duplicated_from": str(post.id)},
        )
        self.db.commit()
        self.db.refresh(clone)
        return self._serialize_post(clone)

    # ── Brand voice & AI ──────────────────────────────────────────────────────

    def get_brand_voice(self, workspace: Workspace) -> BrandVoiceOut:
        row = self._get_brand_voice_row(workspace.id)
        if not row:
            return BrandVoiceOut(workspaceId=str(workspace.id))
        return self._serialize_brand_voice(row)

    def upsert_brand_voice(
        self,
        workspace: Workspace,
        payload: BrandVoiceUpdateRequest,
    ) -> BrandVoiceOut:
        row = self._get_brand_voice_row(workspace.id)
        if not row:
            row = SocialBrandVoice(id=uuid.uuid4(), workspace_id=workspace.id)
            self.db.add(row)
        row.brand_name = payload.brandName
        row.industry = payload.industry
        row.tagline = payload.tagline
        row.target_audience = payload.targetAudience
        row.tones = list(payload.tones or [])
        row.words_to_use = list(payload.wordsToUse or [])
        row.words_to_avoid = list(payload.wordsToAvoid or [])
        row.cta_phrases = list(payload.ctaPhrases or [])
        row.sentence_length = payload.sentenceLength
        row.emoji_usage = payload.emojiUsage
        row.primary_language = payload.primaryLanguage
        row.system_prompt_override = payload.systemPromptOverride
        if payload.logoUrl is not None:
            row.logo_url = payload.logoUrl.strip() or None
        self.db.commit()
        self.db.refresh(row)
        return self._serialize_brand_voice(row)

    def test_brand_voice(
        self,
        workspace: Workspace,
        payload: Optional[BrandVoiceUpdateRequest] = None,
    ) -> BrandVoiceTestResponse:
        if payload is not None:
            voice = payload.model_dump()
            brand_voice = {
                "brand_name": voice.get("brandName") or "",
                "industry": voice.get("industry") or "",
                "tagline": voice.get("tagline") or "",
                "target_audience": voice.get("targetAudience") or "",
                "tones": voice.get("tones") or [],
                "words_to_use": voice.get("wordsToUse") or [],
                "words_to_avoid": voice.get("wordsToAvoid") or [],
                "cta_phrases": voice.get("ctaPhrases") or [],
                "sentence_length": (
                    voice.get("sentenceLength").value
                    if hasattr(voice.get("sentenceLength"), "value")
                    else voice.get("sentenceLength")
                ),
                "emoji_usage": (
                    voice.get("emojiUsage").value
                    if hasattr(voice.get("emojiUsage"), "value")
                    else voice.get("emojiUsage")
                ),
                "primary_language": voice.get("primaryLanguage") or "en",
                "system_prompt_override": voice.get("systemPromptOverride"),
            }
        else:
            brand_voice = self._brand_voice_dict(workspace.id)

        from app.social.limits import enforce_ai_text_limit
        from app.plans.service import record_ai_usage

        enforce_ai_text_limit(self.db, workspace)
        record_ai_usage(self.db, workspace.id, "text")
        self.db.commit()

        sample = generate_brand_voice_sample(brand_voice)
        return BrandVoiceTestResponse(**sample)

    def upload_logo(
        self,
        workspace: Workspace,
        *,
        data: bytes,
        content_type: str,
        filename: Optional[str] = None,
    ) -> str:
        from app.social.media import upload_workspace_logo_bytes

        upload = upload_workspace_logo_bytes(
            workspace.id,
            data,
            content_type=content_type,
            filename_hint=filename,
        )
        row = self._get_brand_voice_row(workspace.id)
        if not row:
            row = SocialBrandVoice(id=uuid.uuid4(), workspace_id=workspace.id)
            self.db.add(row)
        row.logo_url = upload.url
        self.db.commit()
        return upload.url

    def generate_post(
        self,
        workspace: Workspace,
        payload: GeneratePostRequest,
        user: Optional[User] = None,
    ) -> GeneratePostResponse:
        from app.social.ai.generator import (
            generate_carousel_content,
            generate_platform_content,
            generate_poll_content,
            generate_thread_content,
        )
        from app.social.limits import enforce_ai_text_limit
        from app.plans.service import record_ai_usage

        enforce_ai_text_limit(self.db, workspace)

        brand_voice = self._brand_voice_dict(workspace.id)
        fmt = (payload.format or "single").lower()

        # Record usage before the LLM call — the AI provider cost is incurred
        # at generation time regardless of whether the caller keeps the output.
        record_ai_usage(self.db, workspace.id, "text", user_id=user.id if user else None)

        # Release DB connection before long LLM work to prevent idle-kill.
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
        try:
            self.db.close()
        except Exception:
            pass

        # ── Carousel ──────────────────────────────────────────────────────────
        if fmt == "carousel":
            result = generate_carousel_content(
                topic=payload.topic,
                tone=payload.tone,
                audience=payload.audience,
                cta=payload.cta,
                brand_voice=brand_voice,
            )
            slides = [
                GeneratedSlide(
                    headline=s["headline"],
                    body=s["body"],
                    imagePrompt=s.get("imagePrompt") or payload.topic,
                )
                for s in result["slides"]
            ]
            return GeneratePostResponse(
                format="carousel",
                prompt=payload.topic,
                slides=slides,
                caption=result.get("caption", ""),
                hashtags=result.get("hashtags", []),
            )

        # ── Thread ────────────────────────────────────────────────────────────
        if fmt == "thread":
            result = generate_thread_content(
                topic=payload.topic,
                tone=payload.tone,
                audience=payload.audience,
                cta=payload.cta,
                brand_voice=brand_voice,
            )
            tweets = [
                GeneratedTweet(text=t["text"], characterCount=len(t["text"]))
                for t in result["tweets"]
            ]
            return GeneratePostResponse(
                format="thread",
                prompt=payload.topic,
                tweets=tweets,
                hashtags=result.get("hashtags", []),
            )

        # ── Poll ──────────────────────────────────────────────────────────────
        if fmt == "poll":
            result = generate_poll_content(
                topic=payload.topic,
                tone=payload.tone,
                audience=payload.audience,
                brand_voice=brand_voice,
            )
            return GeneratePostResponse(
                format="poll",
                prompt=payload.topic,
                pollQuestion=result["question"],
                pollOptions=result["options"],
                caption=result.get("caption", ""),
                hashtags=result.get("hashtags", []),
            )

        # ── Single / Reel / Story (default) ──────────────────────────────────
        platforms = [p.value for p in payload.platforms]
        result = generate_platform_content(
            topic=payload.topic,
            tone=payload.tone,
            platforms=platforms,
            audience=payload.audience,
            cta=payload.cta,
            include_hashtags=payload.includeHashtags,
            include_comment=payload.includeComment,
            brand_voice=brand_voice,
        )
        return GeneratePostResponse(
            format=fmt,
            platforms={
                key: GeneratedPlatformContent(**value) for key, value in result.items()
            },
            prompt=payload.topic,
        )

    # ── Media assets library ──────────────────────────────────────────────────

    def _serialize_media_asset(self, asset: SocialMediaAsset, *, url: Optional[str] = None) -> MediaAssetOut:
        from app.social.media import refresh_blob_url

        resolved_url = url or refresh_blob_url(asset.blob_key)
        return MediaAssetOut(
            id=str(asset.id),
            mediaType=asset.media_type.value,
            source=asset.source.value,
            url=resolved_url,
            mimeType=asset.mime_type,
            fileSizeBytes=asset.file_size_bytes,
            prompt=asset.prompt,
            soraVideoId=asset.sora_video_id,
            durationSeconds=asset.duration_seconds,
            postId=str(asset.post_id) if asset.post_id else None,
            createdAt=asset.created_at.isoformat(),
        )

    def _record_media_asset(
        self,
        workspace: Workspace,
        user: User,
        *,
        media_type: SocialMediaAssetType,
        source: SocialImageSource,
        blob_key: str,
        blob_url: str,
        mime_type: str,
        file_size_bytes: int,
        prompt: Optional[str] = None,
        sora_video_id: Optional[str] = None,
        duration_seconds: Optional[int] = None,
    ) -> SocialMediaAsset:
        asset = SocialMediaAsset(
            workspace_id=workspace.id,
            created_by=user.id,
            media_type=media_type,
            source=source,
            blob_key=blob_key,
            blob_url=blob_url,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            prompt=prompt,
            sora_video_id=sora_video_id,
            duration_seconds=duration_seconds,
        )
        self.db.add(asset)
        self.db.commit()
        self.db.refresh(asset)
        return asset

    def list_media_assets(
        self,
        workspace: Workspace,
        params: MediaAssetListParams,
    ) -> MediaAssetListResponse:
        filters = [
            SocialMediaAsset.workspace_id == workspace.id,
            SocialMediaAsset.is_deleted.is_(False),
        ]
        if params.mediaType in ("image", "video"):
            filters.append(SocialMediaAsset.media_type == SocialMediaAssetType(params.mediaType))
        if params.source in ("uploaded", "ai_generated"):
            filters.append(SocialMediaAsset.source == SocialImageSource(params.source))
        if params.search:
            term = f"%{params.search.strip()}%"
            filters.append(SocialMediaAsset.prompt.ilike(term))

        total = self.db.scalar(
            select(func.count()).select_from(SocialMediaAsset).where(*filters)
        ) or 0
        page_size = params.pageSize
        page = params.page
        total_pages = max(1, math.ceil(total / page_size)) if total else 0

        rows = self.db.scalars(
            select(SocialMediaAsset)
            .where(*filters)
            .order_by(SocialMediaAsset.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()

        return MediaAssetListResponse(
            items=[self._serialize_media_asset(row) for row in rows],
            total=total,
            page=page,
            pageSize=page_size,
            totalPages=total_pages,
        )

    def get_media_asset(self, workspace: Workspace, asset_id: uuid.UUID) -> MediaAssetOut:
        asset = self.db.scalars(
            select(SocialMediaAsset).where(
                SocialMediaAsset.id == asset_id,
                SocialMediaAsset.workspace_id == workspace.id,
                SocialMediaAsset.is_deleted.is_(False),
            )
        ).first()
        if not asset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found")
        return self._serialize_media_asset(asset)

    def delete_media_asset(self, workspace: Workspace, asset_id: uuid.UUID) -> None:
        asset = self.db.scalars(
            select(SocialMediaAsset).where(
                SocialMediaAsset.id == asset_id,
                SocialMediaAsset.workspace_id == workspace.id,
                SocialMediaAsset.is_deleted.is_(False),
            )
        ).first()
        if not asset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found")
        asset.is_deleted = True
        self.db.commit()

    def generate_image(
        self,
        workspace: Workspace,
        user: User,
        topic: str,
        style: Optional[str] = None,
        size: Optional[str] = "1024x1024",
        *,
        mode: str = "create",
        source_image_url: Optional[str] = None,
    ) -> GenerateImageResponse:
        from app.social.media import (
            SocialBlobUpload,
            blob_key_from_url,
            ensure_public_image_url,
            upload_social_image_bytes,
        )
        from app.social.models import SocialSettings
        from app.social.limits import enforce_ai_image_limit
        from app.plans.service import record_ai_usage

        enforce_ai_image_limit(self.db, workspace)
        record_ai_usage(self.db, workspace.id, "image", user_id=user.id if user else None)
        self.db.commit()

        if not style:
            org_settings = self.db.scalars(
                select(SocialSettings).where(SocialSettings.workspace_id == workspace.id)
            ).first()
            if org_settings and org_settings.image_generation_style:
                style = org_settings.image_generation_style

        data = generate_post_image_from_url(
            topic=topic,
            style=style,
            size=size or "1024x1024",
            mode=mode,  # type: ignore[arg-type]
            source_image_url=source_image_url,
        )

        upload: Optional[SocialBlobUpload] = None
        if "imageB64" in data and data["imageB64"]:
            upload = upload_social_image_bytes(
                workspace.id,
                data["imageB64"],  # type: ignore[arg-type]
                content_type="image/png",
            )
        else:
            public_url = ensure_public_image_url(workspace.id, data.get("imageUrl"))  # type: ignore[arg-type]
            if public_url:
                key = blob_key_from_url(public_url) or f"social/{workspace.id}/external"
                upload = SocialBlobUpload(
                    url=public_url,
                    blob_key=key,
                    content_type="image/png",
                    file_size=0,
                )

        if not upload:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Image generation failed to produce uploadable bytes",
            )

        asset = self._record_media_asset(
            workspace,
            user,
            media_type=SocialMediaAssetType.IMAGE,
            source=SocialImageSource.AI_GENERATED,
            blob_key=upload.blob_key,
            blob_url=upload.url,
            mime_type=upload.content_type,
            file_size_bytes=upload.file_size,
            prompt=topic,
        )

        return GenerateImageResponse(
            imageUrl=upload.url,
            source=str(data["source"]),
            assetId=str(asset.id),
        )

    def upload_image(
        self,
        workspace: Workspace,
        user: User,
        *,
        data: bytes,
        content_type: str,
        filename: Optional[str] = None,
    ) -> GenerateImageResponse:
        from app.social.media import upload_social_image_bytes

        upload = upload_social_image_bytes(
            workspace.id,
            data,
            content_type=content_type,
            filename_hint=filename,
        )
        asset = self._record_media_asset(
            workspace,
            user,
            media_type=SocialMediaAssetType.IMAGE,
            source=SocialImageSource.UPLOADED,
            blob_key=upload.blob_key,
            blob_url=upload.url,
            mime_type=upload.content_type,
            file_size_bytes=upload.file_size,
        )
        return GenerateImageResponse(imageUrl=upload.url, source="uploaded", assetId=str(asset.id))

    def generate_video(
        self,
        workspace: Workspace,
        user: User,
        *,
        prompt: str,
        size: str = "1280x720",
        seconds: str = "4",
        reference_image_bytes: Optional[bytes] = None,
        reference_content_type: Optional[str] = None,
        mode: str = "create",
        remix_video_id: Optional[str] = None,
    ) -> "GenerateVideoResponse":
        from app.social.ai.video_generator import generate_post_video, remix_post_video
        from app.social.media import upload_social_video_bytes
        from app.social.schemas import GenerateVideoResponse
        from app.social.limits import enforce_ai_video_limit
        from app.plans.service import record_ai_usage

        enforce_ai_video_limit(self.db, workspace)
        record_ai_usage(self.db, workspace.id, "video", user_id=user.id if user else None)
        self.db.commit()

        if mode == "remix":
            if not remix_video_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="remix_video_id is required to refine a video",
                )
            result = remix_post_video(remix_video_id=remix_video_id, prompt=prompt)
        else:
            result = generate_post_video(
                prompt=prompt,
                size=size,  # type: ignore[arg-type]
                seconds=seconds,  # type: ignore[arg-type]
                reference_image_bytes=reference_image_bytes,
                reference_content_type=reference_content_type,
            )
        upload = upload_social_video_bytes(
            workspace.id,
            result["videoBytes"],
            content_type=result["contentType"],
        )
        duration = int(seconds) if seconds.isdigit() else None
        asset = self._record_media_asset(
            workspace,
            user,
            media_type=SocialMediaAssetType.VIDEO,
            source=SocialImageSource.AI_GENERATED,
            blob_key=upload.blob_key,
            blob_url=upload.url,
            mime_type=upload.content_type,
            file_size_bytes=upload.file_size,
            prompt=prompt,
            sora_video_id=result.get("soraVideoId"),
            duration_seconds=duration if mode != "remix" else None,
        )
        return GenerateVideoResponse(
            videoUrl=upload.url,
            source=result["source"],
            soraVideoId=result.get("soraVideoId"),
            assetId=str(asset.id),
        )

    def upload_video(
        self,
        workspace: Workspace,
        user: User,
        *,
        data: bytes,
        content_type: str,
        filename: Optional[str] = None,
    ) -> "UploadVideoResponse":
        from app.social.media import upload_social_video_bytes
        from app.social.schemas import UploadVideoResponse

        upload = upload_social_video_bytes(
            workspace.id,
            data,
            content_type=content_type,
            filename_hint=filename,
        )
        asset = self._record_media_asset(
            workspace,
            user,
            media_type=SocialMediaAssetType.VIDEO,
            source=SocialImageSource.UPLOADED,
            blob_key=upload.blob_key,
            blob_url=upload.url,
            mime_type=upload.content_type,
            file_size_bytes=upload.file_size,
        )
        return UploadVideoResponse(videoUrl=upload.url, source="uploaded", assetId=str(asset.id))

    # ── Internals ─────────────────────────────────────────────────────────────

    def _get_brand_voice_row(self, workspace_id: uuid.UUID) -> Optional[SocialBrandVoice]:
        return self.db.scalars(
            select(SocialBrandVoice).where(SocialBrandVoice.workspace_id == workspace_id)
        ).first()

    def _brand_voice_dict(self, workspace_id: uuid.UUID) -> dict:
        row = self._get_brand_voice_row(workspace_id)
        if not row:
            return {}
        return {
            "brand_name": row.brand_name,
            "industry": row.industry,
            "tagline": row.tagline,
            "target_audience": row.target_audience,
            "tones": list(row.tones or []),
            "words_to_use": list(row.words_to_use or []),
            "words_to_avoid": list(row.words_to_avoid or []),
            "cta_phrases": list(row.cta_phrases or []),
            "sentence_length": row.sentence_length.value if row.sentence_length else "medium",
            "emoji_usage": row.emoji_usage.value if row.emoji_usage else "sometimes",
            "primary_language": row.primary_language,
            "system_prompt_override": row.system_prompt_override,
            "logo_url": row.logo_url,
        }

    def _serialize_brand_voice(self, row: SocialBrandVoice) -> BrandVoiceOut:
        return BrandVoiceOut(
            id=str(row.id),
            workspaceId=str(row.workspace_id),
            brandName=row.brand_name,
            industry=row.industry,
            tagline=row.tagline,
            targetAudience=row.target_audience,
            tones=list(row.tones or []),
            wordsToUse=list(row.words_to_use or []),
            wordsToAvoid=list(row.words_to_avoid or []),
            ctaPhrases=list(row.cta_phrases or []),
            sentenceLength=row.sentence_length,
            emojiUsage=row.emoji_usage,
            primaryLanguage=row.primary_language,
            systemPromptOverride=row.system_prompt_override,
            logoUrl=row.logo_url,
            updatedAt=_iso(row.updated_at),
        )

    def _default_account_for_platform(
        self,
        workspace_id: uuid.UUID,
        platform: SocialPlatform,
    ) -> Optional[SocialAccount]:
        return self.db.scalars(
            select(SocialAccount)
            .where(
                SocialAccount.workspace_id == workspace_id,
                SocialAccount.platform == platform,
                SocialAccount.is_active.is_(True),
            )
            .order_by(SocialAccount.is_default.desc(), SocialAccount.created_at.asc())
        ).first()

    def _validate_ready_to_publish(self, post: SocialPost) -> None:
        platforms = list(post.platforms or [])
        if not platforms:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Post has no platform content",
            )
        publishable = [pp for pp in platforms if pp.platform in PUBLISHABLE_PLATFORMS]
        if not publishable:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Connect at least one publishable account (Facebook, Instagram, LinkedIn, or X)",
            )
        for pp in publishable:
            if not (pp.caption or "").strip() and not post.image_url:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{pp.platform.value} content is empty",
                )
            if not pp.social_account_id:
                # Try to attach default account
                account = self._default_account_for_platform(post.workspace_id, pp.platform)
                if not account:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Connect a {pp.platform.value} account before publishing",
                    )
                pp.social_account_id = account.id
            if pp.platform == SocialPlatform.INSTAGRAM and not post.image_url:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Instagram posts require an image URL",
                )
            if pp.platform == SocialPlatform.X and post.image_url:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="X free tier is text-only — remove the image before publishing to X",
                )
        from app.social.media import is_video_media_url

        if post.image_url and is_video_media_url(post.image_url):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Video publishing is not supported yet — save as draft only",
            )
        self.db.flush()

    def _enqueue_publish(self, post_id: uuid.UUID, eta: Optional[datetime]) -> None:
        from app.social.tasks.publish import publish_post

        import logging

        logger = logging.getLogger(__name__)
        try:
            if eta is not None:
                publish_post.apply_async(args=[str(post_id)], eta=eta, queue="social_publish")
            else:
                publish_post.apply_async(args=[str(post_id)], queue="social_publish")
        except Exception as exc:
            # Fall back: beat tick picks up scheduled posts; publish-now runs inline
            logger.warning("Failed to enqueue publish_post for %s: %s", post_id, exc)
            if eta is None:
                try:
                    publish_post.delay(str(post_id))
                except Exception:
                    publish_post.run(str(post_id))

    def _upsert_account(
        self,
        *,
        workspace_id: uuid.UUID,
        platform: SocialPlatform,
        profile: OAuthAccountProfile,
        is_default: bool = False,
    ) -> SocialAccount:
        # Match legacy LinkedIn person ids (bare id) when migrating to person:{id}.
        id_candidates = [profile.platform_account_id]
        if profile.platform_account_id.startswith("person:"):
            id_candidates.append(profile.platform_account_id.split(":", 1)[1])

        existing = self.db.scalars(
            select(SocialAccount).where(
                SocialAccount.workspace_id == workspace_id,
                SocialAccount.platform == platform,
                SocialAccount.platform_account_id.in_(id_candidates),
            )
        ).first()
        if existing:
            existing.platform_account_id = profile.platform_account_id
            account = self._apply_profile(existing, profile, reactivate=True)
        else:
            account = SocialAccount(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                platform=platform,
                platform_account_id=profile.platform_account_id,
            )
            self.db.add(account)
            account = self._apply_profile(account, profile, reactivate=True)

        if is_default:
            self._clear_default(workspace_id, platform)
            account.is_default = True
        return account

    def _apply_profile(
        self,
        account: SocialAccount,
        profile: OAuthAccountProfile,
        *,
        reactivate: bool,
    ) -> SocialAccount:
        account.account_type = profile.account_type
        account.account_name = profile.account_name
        account.account_picture_url = profile.account_picture_url
        account.follower_count = profile.follower_count
        account.access_token_enc = encrypt(profile.access_token) if profile.access_token else None
        account.refresh_token_enc = (
            encrypt(profile.refresh_token) if profile.refresh_token else None
        )
        account.token_expires_at = profile.token_expires_at
        account.last_synced_at = datetime.now(timezone.utc)
        if reactivate:
            account.is_active = True
        return account

    def _clear_default(self, workspace_id: uuid.UUID, platform: SocialPlatform) -> None:
        rows = self.db.scalars(
            select(SocialAccount).where(
                SocialAccount.workspace_id == workspace_id,
                SocialAccount.platform == platform,
                SocialAccount.is_default.is_(True),
            )
        ).all()
        for row in rows:
            row.is_default = False

    def _replace_platforms(
        self,
        post: SocialPost,
        workspace_id: uuid.UUID,
        platforms: list,
    ) -> None:
        post.platforms.clear()
        self.db.flush()
        for item in platforms:
            account_id = None
            if item.socialAccountId:
                account = self.db.get(SocialAccount, uuid.UUID(item.socialAccountId))
                if not account or account.workspace_id != workspace_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Social account {item.socialAccountId} not found in this workspace",
                    )
                if account.platform != item.platform:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Platform does not match the selected social account",
                    )
                account_id = account.id
            else:
                default = self._default_account_for_platform(workspace_id, item.platform)
                account_id = default.id if default else None
            caption = item.caption or ""
            hashtags = list(item.hashtags or [])
            post.platforms.append(
                SocialPostPlatform(
                    id=uuid.uuid4(),
                    platform=item.platform,
                    social_account_id=account_id,
                    caption=caption,
                    hashtags=hashtags,
                    first_comment=item.firstComment,
                    character_count=len(caption),
                )
            )

    def _serialize_account(self, account: SocialAccount) -> SocialAccountOut:
        return SocialAccountOut(
            id=str(account.id),
            workspaceId=str(account.workspace_id),
            platform=account.platform,
            accountType=account.account_type,
            platformAccountId=account.platform_account_id,
            accountName=account.account_name,
            accountPictureUrl=account.account_picture_url,
            followerCount=account.follower_count,
            tokenExpiresAt=_iso(account.token_expires_at),
            tokenStatus=_token_status(account),  # type: ignore[arg-type]
            isDefault=account.is_default,
            isActive=account.is_active,
            lastSyncedAt=_iso(account.last_synced_at),
            createdAt=_iso(account.created_at) or "",
            updatedAt=_iso(account.updated_at) or "",
        )

    def _serialize_post(self, post: SocialPost) -> SocialPostOut:
        platforms = [
            SocialPostPlatformOut(
                id=str(pp.id),
                platform=pp.platform,
                socialAccountId=str(pp.social_account_id) if pp.social_account_id else None,
                caption=pp.caption,
                hashtags=list(pp.hashtags or []),
                firstComment=pp.first_comment,
                characterCount=pp.character_count,
                status=pp.status,
                platformPostId=pp.platform_post_id,
                publishedAt=_iso(pp.published_at),
                errorCode=pp.error_code,
                errorMessage=pp.error_message,
                retryCount=pp.retry_count,
                nextRetryAt=_iso(pp.next_retry_at),
                reach=pp.reach,
                impressions=pp.impressions,
                likes=pp.likes,
                comments=pp.comments,
                shares=pp.shares,
                clicks=pp.clicks,
                engagementRate=pp.engagement_rate,
            )
            for pp in (post.platforms or [])
        ]
        return SocialPostOut(
            id=str(post.id),
            workspaceId=str(post.workspace_id),
            createdBy=str(post.created_by),
            title=post.title,
            status=post.status,
            scheduledAt=_iso(post.scheduled_at),
            publishedAt=_iso(post.published_at),
            approvalStatus=post.approval_status,
            approvedBy=str(post.approved_by) if post.approved_by else None,
            templateId=str(post.template_id) if post.template_id else None,
            aiPrompt=post.ai_prompt,
            imageUrl=post.image_url,
            imageSource=post.image_source,
            platforms=platforms,
            createdAt=_iso(post.created_at) or "",
            updatedAt=_iso(post.updated_at) or "",
        )
