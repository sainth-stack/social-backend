"""Org-scoped business logic for Social Media accounts and posts."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import HTTPException, status
from pymongo.database import Database

from app.core.encryption import decrypt, encrypt
from app.core.mongo_utils import new_id, utcnow
from app.social.ai.generator import generate_brand_voice_sample, generate_platform_content
from app.social.ai.image_generator import generate_post_image_from_url
from app.social.models import (
    SocialImageSource,
    SocialMediaAssetType,
    SocialPlatform,
    SocialPlatformPostStatus,
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


def _token_status(account: dict) -> str:
    if not account.get("is_active") or not account.get("access_token_enc"):
        return "disconnected"
    token_expires_at = account.get("token_expires_at")
    if not token_expires_at:
        return "active"
    expires = token_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if expires <= now:
        return "expired"
    if expires <= now + timedelta(days=TOKEN_EXPIRES_SOON_DAYS):
        return "expires_soon"
    return "active"


def _get_post_platforms(db: Database, post_id: str) -> list[dict]:
    return list(db["social_post_platforms"].find({"post_id": str(post_id)}))


class SocialMediaService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ── Accounts ──────────────────────────────────────────────────────────────

    def list_accounts(
        self,
        workspace: dict,
        platform: Optional[SocialPlatform] = None,
    ) -> list[SocialAccountOut]:
        query: dict = {"workspace_id": workspace["id"]}
        if platform is not None:
            query["platform"] = platform.value
        rows = list(
            self.db["social_accounts"]
            .find(query)
            .sort([("platform", 1), ("account_name", 1)])
        )
        return [self._serialize_account(a) for a in rows]

    def create_account(
        self,
        workspace: dict,
        payload: CreateSocialAccountRequest,
    ) -> SocialAccountOut:
        account = self._upsert_account(
            workspace_id=workspace["id"],
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
        return self._serialize_account(account)

    def update_account(
        self,
        account: dict,
        payload: UpdateSocialAccountRequest,
    ) -> SocialAccountOut:
        updates: dict = {}
        if payload.accountName is not None:
            updates["account_name"] = payload.accountName
        if payload.isActive is not None:
            updates["is_active"] = payload.isActive
            if not payload.isActive:
                updates["access_token_enc"] = None
                updates["refresh_token_enc"] = None
        if payload.isDefault is True:
            self._clear_default(account["workspace_id"], SocialPlatform(account["platform"]))
            updates["is_default"] = True
        elif payload.isDefault is False:
            updates["is_default"] = False
        if updates:
            updates["updated_at"] = utcnow()
            self.db["social_accounts"].update_one({"id": account["id"]}, {"$set": updates})
        account = self.db["social_accounts"].find_one({"id": account["id"]})
        return self._serialize_account(account)

    def delete_account(self, account: dict) -> None:
        self.db["social_accounts"].update_one(
            {"id": account["id"]},
            {
                "$set": {
                    "is_active": False,
                    "access_token_enc": None,
                    "refresh_token_enc": None,
                    "is_default": False,
                    "updated_at": utcnow(),
                }
            },
        )

    def sync_account(self, account: dict) -> SocialAccountOut:
        try:
            get_oauth_handler(SocialPlatform(account["platform"]))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if not account.get("access_token_enc"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Account is disconnected — reconnect before syncing",
            )
        handler = get_oauth_handler(SocialPlatform(account["platform"]))
        token = decrypt(account["access_token_enc"])
        token = self._ensure_fresh_token(account, handler, token)

        try:
            stats = handler.sync_account_stats(account["platform_account_id"], token)
        except Exception as first_exc:
            refreshed = self._force_refresh_token(account, handler)
            if not refreshed:
                raise
            token = refreshed
            try:
                stats = handler.sync_account_stats(account["platform_account_id"], token)
            except Exception:
                raise first_exc from None

        updates: dict = {"last_synced_at": datetime.now(timezone.utc), "updated_at": utcnow()}
        if stats.get("account_name"):
            updates["account_name"] = stats["account_name"]
        if stats.get("account_picture_url") is not None:
            updates["account_picture_url"] = stats["account_picture_url"]
        if stats.get("follower_count") is not None:
            updates["follower_count"] = int(stats["follower_count"])
        self.db["social_accounts"].update_one({"id": account["id"]}, {"$set": updates})
        account = self.db["social_accounts"].find_one({"id": account["id"]})

        try:
            from app.social.analytics.sync import sync_account_daily
            sync_account_daily(self.db, account)
        except Exception as exc:
            logger.warning("Analytics daily sync after account sync failed: %s", exc)

        return self._serialize_account(account)

    def _ensure_fresh_token(self, account: dict, handler, token: str) -> str:
        expires = account.get("token_expires_at")
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        needs_refresh = bool(expires and expires <= datetime.now(timezone.utc) + timedelta(minutes=5))
        if not needs_refresh:
            return token
        refreshed = self._force_refresh_token(account, handler)
        return refreshed or token

    def _force_refresh_token(self, account: dict, handler) -> Optional[str]:
        if not account.get("refresh_token_enc"):
            return None
        try:
            refresh = decrypt(account["refresh_token_enc"])
            payload = handler.refresh_access_token(refresh)
            if not payload or not payload.get("access_token"):
                return None
            updates: dict = {
                "access_token_enc": encrypt(payload["access_token"]),
                "updated_at": utcnow(),
            }
            if payload.get("refresh_token"):
                updates["refresh_token_enc"] = encrypt(str(payload["refresh_token"]))
            expires_in = int(payload.get("expires_in") or 0)
            if expires_in > 0:
                updates["token_expires_at"] = datetime.now(timezone.utc) + timedelta(
                    seconds=expires_in
                )
            self.db["social_accounts"].update_one({"id": account["id"]}, {"$set": updates})
            account = self.db["social_accounts"].find_one({"id": account["id"]})
            logger.info(
                "Refreshed OAuth token for %s account %s",
                account.get("platform"),
                account.get("id"),
            )
            return payload["access_token"]
        except Exception as exc:
            logger.warning(
                "Token refresh failed for %s %s: %s",
                account.get("platform"),
                account.get("id"),
                exc,
            )
            return None

    def get_oauth_url(
        self,
        workspace: dict,
        platform: SocialPlatform,
        *,
        reconnect_account_id: Optional[str] = None,
    ) -> str:
        from app.social.limits import enforce_account_limit

        enforce_account_limit(self.db, workspace)
        try:
            handler = get_oauth_handler(platform)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        state = generate_state_token()
        payload = {
            "workspace_id": str(workspace["id"]),
            "platform": platform.value,
            "reconnect_account_id": reconnect_account_id,
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
                status_code=status.HTTP_400_BAD_REQUEST, detail="OAuth platform mismatch"
            )

        workspace_id = data["workspace_id"]
        reconnect_id = data.get("reconnect_account_id")
        handler = get_oauth_handler(platform)
        result = handler.exchange_code(code, state=state)

        accounts: list[dict] = []
        if reconnect_id and result.accounts:
            existing = self.db["social_accounts"].find_one({"id": reconnect_id})
            if not existing or existing.get("workspace_id") != workspace_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Account to reconnect was not found",
                )
            profile = next(
                (
                    p
                    for p in result.accounts
                    if p.platform_account_id == existing.get("platform_account_id")
                ),
                result.accounts[0],
            )
            accounts.append(self._apply_profile(existing, profile, reactivate=True))
        else:
            for profile in result.accounts:
                accounts.append(
                    self._upsert_account(
                        workspace_id=workspace_id, platform=platform, profile=profile
                    )
                )

        # Ensure one default per platform when none exists.
        for account in accounts:
            has_default = self.db["social_accounts"].find_one(
                {
                    "workspace_id": workspace_id,
                    "platform": platform.value,
                    "is_default": True,
                    "is_active": True,
                }
            )
            if not has_default:
                self.db["social_accounts"].update_one(
                    {"id": account["id"]}, {"$set": {"is_default": True}}
                )
        # Re-fetch accounts
        account_ids = [a["id"] for a in accounts]
        accounts = [self.db["social_accounts"].find_one({"id": aid}) for aid in account_ids]
        return [self._serialize_account(a) for a in accounts if a]

    # ── Posts ─────────────────────────────────────────────────────────────────

    def list_posts(
        self,
        workspace: dict,
        params: SocialPostListParams,
    ) -> SocialPostListResponse:
        query: dict = {"workspace_id": workspace["id"]}
        if params.status is not None:
            query["status"] = params.status.value if hasattr(params.status, "value") else params.status
        if params.search:
            term = params.search.strip()
            # Search in title and ai_prompt; caption search requires checking platforms
            query["$or"] = [
                {"title": {"$regex": term, "$options": "i"}},
                {"ai_prompt": {"$regex": term, "$options": "i"}},
            ]

        total = self.db["social_posts"].count_documents(query)
        page_size = params.pageSize
        page = params.page
        total_pages = max(1, math.ceil(total / page_size)) if total else 0

        rows = list(
            self.db["social_posts"]
            .find(query)
            .sort("updated_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )

        return SocialPostListResponse(
            items=[self._serialize_post(p) for p in rows],
            total=total,
            page=page,
            pageSize=page_size,
            totalPages=total_pages,
        )

    def get_post(self, post: dict) -> SocialPostOut:
        return self._serialize_post(post)

    def create_post(
        self,
        workspace: dict,
        user: dict,
        payload: CreateSocialPostRequest,
    ) -> SocialPostOut:
        from app.social.audit import write_social_audit
        from app.social.limits import enforce_posts_limit
        from app.social.permissions import require_permission
        from app.social.models import SocialPermission

        require_permission(self.db, workspace, user, SocialPermission.EDITOR)
        enforce_posts_limit(self.db, workspace)
        from app.social.media import ensure_public_image_url

        image_url = ensure_public_image_url(workspace["id"], payload.imageUrl)
        template_id = None
        if payload.templateId:
            template_id = payload.templateId

        now = utcnow()
        post_id = new_id()
        status_val = (
            payload.status.value if hasattr(payload.status, "value") else payload.status
        )
        image_source_val = (
            payload.imageSource.value
            if hasattr(payload.imageSource, "value")
            else payload.imageSource
        )
        post = {
            "id": post_id,
            "workspace_id": workspace["id"],
            "created_by": user["id"],
            "title": payload.title,
            "status": status_val,
            "scheduled_at": _parse_dt(payload.scheduledAt),
            "published_at": None,
            "approval_status": "not_required",
            "approved_by": None,
            "template_id": template_id,
            "ai_prompt": payload.aiPrompt,
            "image_url": image_url,
            "image_source": image_source_val,
            "created_at": now,
            "updated_at": now,
        }
        self.db["social_posts"].insert_one(post)
        self._replace_platforms(post_id, workspace["id"], payload.platforms)
        write_social_audit(
            self.db,
            workspace_id=workspace["id"],
            user_id=user["id"],
            action="post.created",
            entity_id=post_id,
            metadata={"title": payload.title},
        )
        post = self.db["social_posts"].find_one({"id": post_id})
        return self._serialize_post(post)

    def update_post(
        self,
        post: dict,
        workspace: dict,
        payload: UpdateSocialPostRequest,
    ) -> SocialPostOut:
        previous_status = post.get("status")
        previous_scheduled = post.get("scheduled_at")
        updates: dict = {"updated_at": utcnow()}

        if payload.title is not None:
            updates["title"] = payload.title
        if payload.status is not None:
            new_status = payload.status.value if hasattr(payload.status, "value") else payload.status
            if (
                new_status == SocialPostStatus.DRAFT.value
                and post.get("status") == SocialPostStatus.SCHEDULED.value
            ):
                updates["scheduled_at"] = None
            if new_status == SocialPostStatus.SCHEDULED.value:
                platforms = _get_post_platforms(self.db, post["id"])
                self._validate_ready_to_publish_dict(post, platforms)
            updates["status"] = new_status
        if payload.scheduledAt is not None:
            new_scheduled = _parse_dt(payload.scheduledAt)
            updates["scheduled_at"] = new_scheduled
            current_status = updates.get("status") or post.get("status")
            becoming_scheduled = current_status == SocialPostStatus.SCHEDULED.value
            if becoming_scheduled and new_scheduled:
                if new_scheduled <= datetime.now(timezone.utc):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="scheduledAt must be in the future",
                    )
                platforms = _get_post_platforms(self.db, post["id"])
                self._validate_ready_to_publish_dict(post, platforms)
        if payload.imageUrl is not None:
            from app.social.media import ensure_public_image_url
            updates["image_url"] = ensure_public_image_url(workspace["id"], payload.imageUrl)
        if payload.imageSource is not None:
            updates["image_source"] = (
                payload.imageSource.value
                if hasattr(payload.imageSource, "value")
                else payload.imageSource
            )
        if payload.aiPrompt is not None:
            updates["ai_prompt"] = payload.aiPrompt
        if payload.templateId is not None:
            updates["template_id"] = payload.templateId or None
        if payload.platforms is not None:
            self._replace_platforms(post["id"], workspace["id"], payload.platforms)

        self.db["social_posts"].update_one({"id": post["id"]}, {"$set": updates})
        post = self.db["social_posts"].find_one({"id": post["id"]})

        new_status = post.get("status")
        new_scheduled = post.get("scheduled_at")
        if (
            new_status == SocialPostStatus.SCHEDULED.value
            and new_scheduled
            and new_scheduled != previous_scheduled
        ):
            self._enqueue_publish(post["id"], eta=new_scheduled)
        return self._serialize_post(post)

    def regenerate_post_content(
        self,
        post: dict,
        workspace: dict,
        user: dict,
        *,
        prompt: Optional[str] = None,
        regenerate_image: bool = True,
        regenerate_caption: bool = True,
        tone: Optional[str] = None,
        cta: Optional[str] = None,
    ) -> SocialPostOut:
        from app.plans.service import record_ai_usage
        from app.social.ai.image_generator import generate_post_image
        from app.social.limits import enforce_ai_image_limit, enforce_ai_text_limit
        from app.social.media import upload_social_image_bytes
        from app.social.polish import SocialPolishService
        from app.social.schemas import SocialPostPlatformIn, UpdateSocialPostRequest

        platforms = _get_post_platforms(self.db, post["id"])
        if not platforms:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Post has no platforms to regenerate",
            )

        platform_row = platforms[0]
        platform = SocialPlatform(platform_row["platform"])
        topic = (prompt or post.get("ai_prompt") or post.get("title") or "").strip()
        if len(topic) < 5:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Add a prompt or save a brief on this post before regenerating",
            )

        settings = SocialPolishService(self.db).get_settings(workspace)
        brand_voice = self._brand_voice_dict(workspace["id"])
        tone = (
            tone
            or settings.get("defaultTone")
            or ((brand_voice.get("tones") or ["Professional"])[0])
        )
        cta = cta or settings.get("defaultCta") or None
        audience = brand_voice.get("target_audience")

        if not regenerate_caption and not regenerate_image:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Choose caption and/or image to regenerate",
            )

        caption = (platform_row.get("caption") or "").strip()
        hashtags = list(platform_row.get("hashtags") or [])

        if regenerate_caption:
            enforce_ai_text_limit(self.db, workspace)
            record_ai_usage(self.db, workspace["id"], "text", user_id=user["id"])

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
                    socialAccountId=str(platform_row.get("social_account_id")) if platform_row.get("social_account_id") else None,
                    caption=caption,
                    hashtags=hashtags,
                )
            ],
        }
        if prompt:
            update_kwargs["aiPrompt"] = prompt.strip()

        image_caption_hint = caption[:180] if caption else topic[:180]

        if regenerate_image:
            try:
                enforce_ai_image_limit(self.db, workspace)
                record_ai_usage(self.db, workspace["id"], "image", user_id=user["id"])
                image_style = settings.get("imageGenerationStyle")
                img_data = generate_post_image(
                    topic=f"Social media image for: {image_caption_hint}",
                    style=image_style,
                    size="1024x1024",
                    mode="create",
                )
                upload = None
                if img_data.get("imageB64"):
                    upload = upload_social_image_bytes(
                        workspace["id"],
                        img_data["imageB64"],
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
                logger.warning("Regenerate image failed for post %s: %s", post["id"], exc)

        return self.update_post(post, workspace, UpdateSocialPostRequest(**update_kwargs))

    def delete_post(self, post: dict) -> None:
        post_id = str(post["id"])
        fresh = self.db["social_posts"].find_one({"id": post_id})
        if not fresh:
            return
        status = fresh.get("status")

        if status == SocialPostStatus.PUBLISHING.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete while the post is publishing — try again in a moment",
            )

        if status == SocialPostStatus.SCHEDULED.value:
            self.db["social_posts"].update_one(
                {"id": post_id},
                {
                    "$set": {
                        "status": SocialPostStatus.DRAFT.value,
                        "scheduled_at": None,
                        "updated_at": utcnow(),
                    }
                },
            )
            status = SocialPostStatus.DRAFT.value

        deletable = {
            SocialPostStatus.DRAFT.value,
            SocialPostStatus.ARCHIVED.value,
            SocialPostStatus.FAILED.value,
            SocialPostStatus.PUBLISHED.value,
        }
        if status not in deletable:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete a post in status {status}",
            )

        self.db["social_post_platforms"].delete_many({"post_id": post_id})
        self.db["social_posts"].delete_one({"id": post_id})

    def schedule_post(self, post: dict, payload: SchedulePostRequest) -> SocialPostOut:
        platforms = _get_post_platforms(self.db, post["id"])
        self._validate_ready_to_publish_dict(post, platforms)
        scheduled_at = _parse_dt(payload.scheduledAt)
        if not scheduled_at or scheduled_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scheduledAt must be a future datetime",
            )
        if post.get("status") not in (
            SocialPostStatus.DRAFT.value,
            SocialPostStatus.SCHEDULED.value,
            SocialPostStatus.FAILED.value,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot schedule a post in status {post.get('status')}",
            )
        self.db["social_posts"].update_one(
            {"id": post["id"]},
            {"$set": {"scheduled_at": scheduled_at, "status": SocialPostStatus.SCHEDULED.value, "updated_at": utcnow()}},
        )
        # Update platform statuses
        for pp in platforms:
            if pp.get("status") != SocialPlatformPostStatus.PUBLISHED.value:
                self.db["social_post_platforms"].update_one(
                    {"id": pp["id"]},
                    {"$set": {"status": SocialPlatformPostStatus.PENDING.value, "error_code": None, "error_message": None}},
                )
        post = self.db["social_posts"].find_one({"id": post["id"]})
        self._enqueue_publish(post["id"], eta=scheduled_at)
        return self._serialize_post(post)

    def publish_now(self, post: dict) -> SocialPostOut:
        platforms = _get_post_platforms(self.db, post["id"])
        self._validate_ready_to_publish_dict(post, platforms)
        if post.get("status") in (SocialPostStatus.PUBLISHING.value, SocialPostStatus.PUBLISHED.value):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Post is already {post.get('status')}",
            )
        now = utcnow()
        self.db["social_posts"].update_one(
            {"id": post["id"]},
            {
                "$set": {
                    "status": SocialPostStatus.PUBLISHING.value,
                    "scheduled_at": post.get("scheduled_at") or now,
                    "updated_at": now,
                }
            },
        )
        for pp in platforms:
            if pp.get("status") != SocialPlatformPostStatus.PUBLISHED.value:
                self.db["social_post_platforms"].update_one(
                    {"id": pp["id"]},
                    {"$set": {"status": SocialPlatformPostStatus.PENDING.value, "error_code": None, "error_message": None}},
                )
        post = self.db["social_posts"].find_one({"id": post["id"]})
        self._enqueue_publish(post["id"], eta=None)
        return self._serialize_post(post)

    def archive_post(self, post: dict) -> SocialPostOut:
        if post.get("status") not in (
            SocialPostStatus.PUBLISHED.value,
            SocialPostStatus.FAILED.value,
            SocialPostStatus.DRAFT.value,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only published, failed, or draft posts can be archived",
            )
        self.db["social_posts"].update_one(
            {"id": post["id"]},
            {"$set": {"status": SocialPostStatus.ARCHIVED.value, "updated_at": utcnow()}},
        )
        post = self.db["social_posts"].find_one({"id": post["id"]})
        return self._serialize_post(post)

    def retry_post(self, post: dict) -> "RetryResponse":
        from app.social.publishers.base import MAX_RETRIES, is_retryable_error
        from app.social.schemas import RetryResponse
        from app.social.tasks.retry import retry_failed_post

        platforms = _get_post_platforms(self.db, post["id"])
        if post.get("status") not in (SocialPostStatus.FAILED.value, SocialPostStatus.PUBLISHED.value):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only failed (or partially failed) posts can be retried",
            )

        retried = 0
        skipped = 0
        for pp in platforms:
            if pp.get("status") != SocialPlatformPostStatus.FAILED.value:
                continue
            if not is_retryable_error(pp.get("error_code"), True):
                skipped += 1
                continue
            if (pp.get("retry_count") or 0) >= MAX_RETRIES:
                skipped += 1
                continue
            self.db["social_post_platforms"].update_one(
                {"id": pp["id"]},
                {"$set": {"status": SocialPlatformPostStatus.PENDING.value, "next_retry_at": None}},
            )
            retried += 1
            try:
                retry_failed_post.apply_async(args=[str(pp["id"])], queue="social_publish")
            except Exception:
                retry_failed_post.run(str(pp["id"]))

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
                status_code=status.HTTP_400_BAD_REQUEST, detail="No failed platforms to retry"
            )

        self.db["social_posts"].update_one(
            {"id": post["id"]},
            {"$set": {"status": SocialPostStatus.PUBLISHING.value, "updated_at": utcnow()}},
        )
        post = self.db["social_posts"].find_one({"id": post["id"]})
        return RetryResponse(
            post=self._serialize_post(post),
            retriedPlatforms=retried,
            skippedPlatforms=skipped,
        )

    def bulk_retry(self, workspace: dict, post_ids: list[str]) -> "BulkRetryResponse":
        from app.social.schemas import BulkRetryResponse, RetryResponse

        items: list[RetryResponse] = []
        total = 0
        for pid in post_ids:
            post = self.db["social_posts"].find_one({"id": str(pid)})
            if not post or post.get("workspace_id") != workspace["id"]:
                continue
            try:
                result = self.retry_post(post)
                items.append(result)
                total += result.retriedPlatforms
            except HTTPException:
                continue
        return BulkRetryResponse(items=items, totalRetried=total)

    def cancel_schedule(self, post: dict) -> SocialPostOut:
        if post.get("status") != SocialPostStatus.SCHEDULED.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only scheduled posts can be cancelled",
            )
        self.db["social_posts"].update_one(
            {"id": post["id"]},
            {"$set": {"status": SocialPostStatus.DRAFT.value, "scheduled_at": None, "updated_at": utcnow()}},
        )
        post = self.db["social_posts"].find_one({"id": post["id"]})
        return self._serialize_post(post)

    def analytics_overview(self, workspace: dict, from_date: Optional[str], to_date: Optional[str]):
        from app.social.analytics.aggregator import AnalyticsAggregator
        from app.social.schemas import AnalyticsOverviewOut

        data = AnalyticsAggregator(self.db).overview(workspace["id"], from_date, to_date)
        return AnalyticsOverviewOut(**data)

    def analytics_platform(
        self, workspace: dict, platform: SocialPlatform, from_date: Optional[str], to_date: Optional[str]
    ):
        from app.social.analytics.aggregator import AnalyticsAggregator
        from app.social.schemas import PlatformAnalyticsOut

        data = AnalyticsAggregator(self.db).platform(workspace["id"], platform, from_date, to_date)
        return PlatformAnalyticsOut(**data)

    def analytics_posts(
        self, workspace: dict, from_date: Optional[str], to_date: Optional[str], sort: str = "engagementRate", order: str = "desc"
    ):
        from app.social.analytics.aggregator import AnalyticsAggregator
        from app.social.schemas import PostPerformanceOut

        data = AnalyticsAggregator(self.db).posts(workspace["id"], from_date, to_date, sort=sort, order=order)
        return PostPerformanceOut(**data)

    def analytics_audience(self, workspace: dict, from_date: Optional[str], to_date: Optional[str]):
        from app.social.analytics.aggregator import AnalyticsAggregator
        from app.social.schemas import AudienceGrowthOut

        data = AnalyticsAggregator(self.db).audience(workspace["id"], from_date, to_date)
        return AudienceGrowthOut(**data)

    def calendar(self, workspace: dict, month: str) -> CalendarResponse:
        try:
            year_s, month_s = month.split("-")
            year, mon = int(year_s), int(month_s)
            start = datetime(year, mon, 1, tzinfo=timezone.utc)
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if mon == 12 else datetime(year, mon + 1, 1, tzinfo=timezone.utc)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="month must be YYYY-MM") from exc

        valid_statuses = [
            SocialPostStatus.DRAFT.value,
            SocialPostStatus.SCHEDULED.value,
            SocialPostStatus.PUBLISHING.value,
            SocialPostStatus.PUBLISHED.value,
            SocialPostStatus.FAILED.value,
        ]
        rows = list(
            self.db["social_posts"]
            .find(
                {
                    "workspace_id": workspace["id"],
                    "scheduled_at": {"$ne": None, "$gte": start, "$lt": end},
                    "status": {"$in": valid_statuses},
                }
            )
            .sort("scheduled_at", 1)
        )

        items: list[CalendarPostOut] = []
        from app.social.media import resolve_stored_image_url

        for post in rows:
            platforms = _get_post_platforms(self.db, post["id"])
            preview = ""
            platform_list: list[SocialPlatform] = []
            for pp in platforms:
                try:
                    platform_list.append(SocialPlatform(pp.get("platform", "")))
                except ValueError:
                    pass
                if not preview and pp.get("caption"):
                    preview = pp["caption"][:80]
            items.append(
                CalendarPostOut(
                    id=str(post["id"]),
                    title=post.get("title", ""),
                    status=SocialPostStatus(post.get("status", "draft")),
                    scheduledAt=_iso(post.get("scheduled_at")),
                    publishedAt=_iso(post.get("published_at")),
                    platforms=platform_list,
                    captionPreview=preview or post.get("title") or "",
                    imageUrl=resolve_stored_image_url(post.get("image_url")),
                )
            )
        return CalendarResponse(month=month, items=items)

    def duplicate_post(self, post: dict, user: dict, workspace: dict) -> SocialPostOut:
        from app.social.audit import write_social_audit
        from app.social.limits import enforce_posts_limit

        enforce_posts_limit(self.db, workspace)
        platforms = _get_post_platforms(self.db, post["id"])
        now = utcnow()
        clone_id = new_id()
        clone = {
            "id": clone_id,
            "workspace_id": post["workspace_id"],
            "created_by": user["id"],
            "title": f"{post.get('title', '')} (copy)" if post.get("title") else "Untitled draft",
            "status": SocialPostStatus.DRAFT.value,
            "scheduled_at": None,
            "published_at": None,
            "approval_status": "not_required",
            "approved_by": None,
            "template_id": None,
            "ai_prompt": post.get("ai_prompt"),
            "image_url": post.get("image_url"),
            "image_source": post.get("image_source", SocialImageSource.NONE.value),
            "created_at": now,
            "updated_at": now,
        }
        self.db["social_posts"].insert_one(clone)
        for pp in platforms:
            self.db["social_post_platforms"].insert_one(
                {
                    "id": new_id(),
                    "post_id": clone_id,
                    "platform": pp.get("platform"),
                    "social_account_id": pp.get("social_account_id"),
                    "caption": pp.get("caption", ""),
                    "hashtags": list(pp.get("hashtags") or []),
                    "first_comment": pp.get("first_comment"),
                    "character_count": pp.get("character_count", 0),
                    "status": SocialPlatformPostStatus.PENDING.value,
                    "platform_post_id": None,
                    "published_at": None,
                    "error_code": None,
                    "error_message": None,
                    "retry_count": 0,
                    "next_retry_at": None,
                    "reach": 0,
                    "impressions": 0,
                    "likes": 0,
                    "comments": 0,
                    "shares": 0,
                    "clicks": 0,
                    "engagement_rate": 0.0,
                }
            )
        write_social_audit(
            self.db,
            workspace_id=workspace["id"],
            user_id=user["id"],
            action="post.created",
            entity_id=clone_id,
            metadata={"title": clone.get("title"), "duplicated_from": str(post["id"])},
        )
        clone = self.db["social_posts"].find_one({"id": clone_id})
        return self._serialize_post(clone)

    # ── Brand voice & AI ──────────────────────────────────────────────────────

    def get_brand_voice(self, workspace: dict) -> BrandVoiceOut:
        row = self._get_brand_voice_row(workspace["id"])
        if not row:
            return BrandVoiceOut(workspaceId=str(workspace["id"]))
        return self._serialize_brand_voice(row)

    def upsert_brand_voice(self, workspace: dict, payload: BrandVoiceUpdateRequest) -> BrandVoiceOut:
        row = self._get_brand_voice_row(workspace["id"])
        now = utcnow()
        updates = {
            "brand_name": payload.brandName,
            "industry": payload.industry,
            "tagline": payload.tagline,
            "target_audience": payload.targetAudience,
            "tones": list(payload.tones or []),
            "words_to_use": list(payload.wordsToUse or []),
            "words_to_avoid": list(payload.wordsToAvoid or []),
            "cta_phrases": list(payload.ctaPhrases or []),
            "sentence_length": payload.sentenceLength.value if hasattr(payload.sentenceLength, "value") else payload.sentenceLength,
            "emoji_usage": payload.emojiUsage.value if hasattr(payload.emojiUsage, "value") else payload.emojiUsage,
            "primary_language": payload.primaryLanguage,
            "system_prompt_override": payload.systemPromptOverride,
            "updated_at": now,
        }
        if payload.logoUrl is not None:
            updates["logo_url"] = payload.logoUrl.strip() or None
        if not row:
            doc = {"id": new_id(), "workspace_id": workspace["id"]}
            doc.update(updates)
            self.db["social_brand_voices"].insert_one(doc)
        else:
            self.db["social_brand_voices"].update_one(
                {"workspace_id": workspace["id"]}, {"$set": updates}
            )
        row = self.db["social_brand_voices"].find_one({"workspace_id": workspace["id"]})
        return self._serialize_brand_voice(row)

    def test_brand_voice(
        self, workspace: dict, payload: Optional[BrandVoiceUpdateRequest] = None
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
            brand_voice = self._brand_voice_dict(workspace["id"])

        from app.social.limits import enforce_ai_text_limit
        from app.plans.service import record_ai_usage

        enforce_ai_text_limit(self.db, workspace)
        record_ai_usage(self.db, workspace["id"], "text")

        sample = generate_brand_voice_sample(brand_voice)
        return BrandVoiceTestResponse(**sample)

    def upload_logo(self, workspace: dict, *, data: bytes, content_type: str, filename: Optional[str] = None) -> str:
        from app.social.media import upload_workspace_logo_bytes

        upload = upload_workspace_logo_bytes(
            workspace["id"], data, content_type=content_type, filename_hint=filename
        )
        row = self._get_brand_voice_row(workspace["id"])
        if not row:
            self.db["social_brand_voices"].insert_one(
                {"id": new_id(), "workspace_id": workspace["id"], "logo_url": upload.url, "updated_at": utcnow()}
            )
        else:
            self.db["social_brand_voices"].update_one(
                {"workspace_id": workspace["id"]}, {"$set": {"logo_url": upload.url, "updated_at": utcnow()}}
            )
        return upload.url

    def generate_post(self, workspace: dict, payload: GeneratePostRequest, user: Optional[dict] = None) -> GeneratePostResponse:
        from app.social.ai.generator import (
            generate_carousel_content,
            generate_platform_content,
            generate_poll_content,
            generate_thread_content,
        )
        from app.social.limits import enforce_ai_text_limit
        from app.plans.service import record_ai_usage

        enforce_ai_text_limit(self.db, workspace)

        brand_voice = self._brand_voice_dict(workspace["id"])
        fmt = (payload.format or "single").lower()

        record_ai_usage(self.db, workspace["id"], "text", user_id=user["id"] if user else None)

        if fmt == "carousel":
            result = generate_carousel_content(
                topic=payload.topic, tone=payload.tone, audience=payload.audience,
                cta=payload.cta, brand_voice=brand_voice,
            )
            slides = [
                GeneratedSlide(
                    headline=s["headline"], body=s["body"],
                    imagePrompt=s.get("imagePrompt") or payload.topic,
                )
                for s in result["slides"]
            ]
            return GeneratePostResponse(
                format="carousel", prompt=payload.topic, slides=slides,
                caption=result.get("caption", ""), hashtags=result.get("hashtags", []),
            )

        if fmt == "thread":
            result = generate_thread_content(
                topic=payload.topic, tone=payload.tone, audience=payload.audience,
                cta=payload.cta, brand_voice=brand_voice,
            )
            tweets = [
                GeneratedTweet(text=t["text"], characterCount=len(t["text"]))
                for t in result["tweets"]
            ]
            return GeneratePostResponse(
                format="thread", prompt=payload.topic, tweets=tweets,
                hashtags=result.get("hashtags", []),
            )

        if fmt == "poll":
            result = generate_poll_content(
                topic=payload.topic, tone=payload.tone, audience=payload.audience,
                brand_voice=brand_voice,
            )
            return GeneratePostResponse(
                format="poll", prompt=payload.topic, pollQuestion=result["question"],
                pollOptions=result["options"], caption=result.get("caption", ""),
                hashtags=result.get("hashtags", []),
            )

        platforms = [p.value for p in payload.platforms]
        result = generate_platform_content(
            topic=payload.topic, tone=payload.tone, platforms=platforms,
            audience=payload.audience, cta=payload.cta,
            include_hashtags=payload.includeHashtags, include_comment=payload.includeComment,
            brand_voice=brand_voice,
        )
        return GeneratePostResponse(
            format=fmt,
            platforms={key: GeneratedPlatformContent(**value) for key, value in result.items()},
            prompt=payload.topic,
        )

    # ── Media assets library ──────────────────────────────────────────────────

    def _serialize_media_asset(self, asset: dict, *, url: Optional[str] = None) -> MediaAssetOut:
        from app.social.media import refresh_blob_url

        resolved_url = url or refresh_blob_url(asset.get("blob_key", ""))
        return MediaAssetOut(
            id=str(asset["id"]),
            mediaType=asset.get("media_type", "image"),
            source=asset.get("source", "uploaded"),
            url=resolved_url,
            mimeType=asset.get("mime_type", ""),
            fileSizeBytes=asset.get("file_size_bytes", 0),
            prompt=asset.get("prompt"),
            soraVideoId=asset.get("sora_video_id"),
            durationSeconds=asset.get("duration_seconds"),
            postId=str(asset["post_id"]) if asset.get("post_id") else None,
            createdAt=asset["created_at"].isoformat() if asset.get("created_at") else "",
        )

    def _record_media_asset(
        self,
        workspace: dict,
        user: dict,
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
    ) -> dict:
        now = utcnow()
        asset = {
            "id": new_id(),
            "workspace_id": workspace["id"],
            "created_by": user["id"],
            "media_type": media_type.value if hasattr(media_type, "value") else media_type,
            "source": source.value if hasattr(source, "value") else source,
            "blob_key": blob_key,
            "blob_url": blob_url,
            "mime_type": mime_type,
            "file_size_bytes": file_size_bytes,
            "prompt": prompt,
            "sora_video_id": sora_video_id,
            "duration_seconds": duration_seconds,
            "post_id": None,
            "is_deleted": False,
            "created_at": now,
            "updated_at": now,
        }
        self.db["social_media_assets"].insert_one(asset)
        return asset

    def list_media_assets(self, workspace: dict, params: MediaAssetListParams) -> MediaAssetListResponse:
        query: dict = {"workspace_id": workspace["id"], "is_deleted": False}
        if params.mediaType in ("image", "video"):
            query["media_type"] = params.mediaType
        if params.source in ("uploaded", "ai_generated"):
            query["source"] = params.source
        if params.search:
            query["prompt"] = {"$regex": params.search.strip(), "$options": "i"}

        total = self.db["social_media_assets"].count_documents(query)
        page_size = params.pageSize
        page = params.page
        total_pages = max(1, math.ceil(total / page_size)) if total else 0

        rows = list(
            self.db["social_media_assets"]
            .find(query)
            .sort("created_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )

        return MediaAssetListResponse(
            items=[self._serialize_media_asset(row) for row in rows],
            total=total,
            page=page,
            pageSize=page_size,
            totalPages=total_pages,
        )

    def get_media_asset(self, workspace: dict, asset_id: str) -> MediaAssetOut:
        asset = self.db["social_media_assets"].find_one(
            {"id": str(asset_id), "workspace_id": workspace["id"], "is_deleted": False}
        )
        if not asset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found")
        return self._serialize_media_asset(asset)

    def delete_media_asset(self, workspace: dict, asset_id: str) -> None:
        asset = self.db["social_media_assets"].find_one(
            {"id": str(asset_id), "workspace_id": workspace["id"], "is_deleted": False}
        )
        if not asset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found")
        self.db["social_media_assets"].update_one(
            {"id": str(asset_id)}, {"$set": {"is_deleted": True, "updated_at": utcnow()}}
        )

    def generate_image(
        self,
        workspace: dict,
        user: dict,
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
        from app.social.limits import enforce_ai_image_limit
        from app.plans.service import record_ai_usage

        enforce_ai_image_limit(self.db, workspace)
        record_ai_usage(self.db, workspace["id"], "image", user_id=user["id"] if user else None)

        if not style:
            org_settings = self.db["social_settings"].find_one({"workspace_id": workspace["id"]})
            if org_settings and org_settings.get("image_generation_style"):
                style = org_settings["image_generation_style"]

        data = generate_post_image_from_url(
            topic=topic,
            style=style,
            size=size or "1024x1024",
            mode=mode,
            source_image_url=source_image_url,
        )

        upload: Optional[SocialBlobUpload] = None
        if "imageB64" in data and data["imageB64"]:
            upload = upload_social_image_bytes(
                workspace["id"], data["imageB64"], content_type="image/png"
            )
        else:
            public_url = ensure_public_image_url(workspace["id"], data.get("imageUrl"))
            if public_url:
                key = blob_key_from_url(public_url) or f"social/{workspace['id']}/external"
                upload = SocialBlobUpload(
                    url=public_url, blob_key=key, content_type="image/png", file_size=0
                )

        if not upload:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Image generation failed to produce uploadable bytes",
            )

        asset = self._record_media_asset(
            workspace, user,
            media_type=SocialMediaAssetType.IMAGE,
            source=SocialImageSource.AI_GENERATED,
            blob_key=upload.blob_key,
            blob_url=upload.url,
            mime_type=upload.content_type,
            file_size_bytes=upload.file_size,
            prompt=topic,
        )

        return GenerateImageResponse(
            imageUrl=upload.url, source=str(data["source"]), assetId=str(asset["id"])
        )

    def upload_image(
        self, workspace: dict, user: dict, *, data: bytes, content_type: str, filename: Optional[str] = None
    ) -> GenerateImageResponse:
        from app.social.media import upload_social_image_bytes

        upload = upload_social_image_bytes(
            workspace["id"], data, content_type=content_type, filename_hint=filename
        )
        asset = self._record_media_asset(
            workspace, user,
            media_type=SocialMediaAssetType.IMAGE,
            source=SocialImageSource.UPLOADED,
            blob_key=upload.blob_key,
            blob_url=upload.url,
            mime_type=upload.content_type,
            file_size_bytes=upload.file_size,
        )
        return GenerateImageResponse(imageUrl=upload.url, source="uploaded", assetId=str(asset["id"]))

    def generate_video(
        self,
        workspace: dict,
        user: dict,
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
        record_ai_usage(self.db, workspace["id"], "video", user_id=user["id"] if user else None)

        if mode == "remix":
            if not remix_video_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="remix_video_id is required to refine a video",
                )
            result = remix_post_video(remix_video_id=remix_video_id, prompt=prompt)
        else:
            result = generate_post_video(
                prompt=prompt, size=size, seconds=seconds,
                reference_image_bytes=reference_image_bytes,
                reference_content_type=reference_content_type,
            )

        upload = upload_social_video_bytes(
            workspace["id"], result["videoBytes"], content_type=result["contentType"]
        )
        duration = int(seconds) if seconds.isdigit() else None
        asset = self._record_media_asset(
            workspace, user,
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
            assetId=str(asset["id"]),
        )

    def upload_video(
        self, workspace: dict, user: dict, *, data: bytes, content_type: str, filename: Optional[str] = None
    ) -> "UploadVideoResponse":
        from app.social.media import upload_social_video_bytes
        from app.social.schemas import UploadVideoResponse

        upload = upload_social_video_bytes(
            workspace["id"], data, content_type=content_type, filename_hint=filename
        )
        asset = self._record_media_asset(
            workspace, user,
            media_type=SocialMediaAssetType.VIDEO,
            source=SocialImageSource.UPLOADED,
            blob_key=upload.blob_key,
            blob_url=upload.url,
            mime_type=upload.content_type,
            file_size_bytes=upload.file_size,
        )
        return UploadVideoResponse(videoUrl=upload.url, source="uploaded", assetId=str(asset["id"]))

    # ── Internals ─────────────────────────────────────────────────────────────

    def _get_brand_voice_row(self, workspace_id: str) -> Optional[dict]:
        return self.db["social_brand_voices"].find_one({"workspace_id": str(workspace_id)})

    def _brand_voice_dict(self, workspace_id: str) -> dict:
        row = self._get_brand_voice_row(workspace_id)
        if not row:
            return {}
        return {
            "brand_name": row.get("brand_name", ""),
            "industry": row.get("industry", ""),
            "tagline": row.get("tagline", ""),
            "target_audience": row.get("target_audience", ""),
            "tones": list(row.get("tones") or []),
            "words_to_use": list(row.get("words_to_use") or []),
            "words_to_avoid": list(row.get("words_to_avoid") or []),
            "cta_phrases": list(row.get("cta_phrases") or []),
            "sentence_length": row.get("sentence_length") or "medium",
            "emoji_usage": row.get("emoji_usage") or "sometimes",
            "primary_language": row.get("primary_language", "en"),
            "system_prompt_override": row.get("system_prompt_override"),
            "logo_url": row.get("logo_url"),
        }

    def _serialize_brand_voice(self, row: dict) -> BrandVoiceOut:
        return BrandVoiceOut(
            id=str(row["id"]),
            workspaceId=str(row.get("workspace_id")),
            brandName=row.get("brand_name", ""),
            industry=row.get("industry", ""),
            tagline=row.get("tagline", ""),
            targetAudience=row.get("target_audience", ""),
            tones=list(row.get("tones") or []),
            wordsToUse=list(row.get("words_to_use") or []),
            wordsToAvoid=list(row.get("words_to_avoid") or []),
            ctaPhrases=list(row.get("cta_phrases") or []),
            sentenceLength=row.get("sentence_length"),
            emojiUsage=row.get("emoji_usage"),
            primaryLanguage=row.get("primary_language", "en"),
            systemPromptOverride=row.get("system_prompt_override"),
            logoUrl=row.get("logo_url"),
            updatedAt=_iso(row.get("updated_at")),
        )

    def _default_account_for_platform(self, workspace_id: str, platform: SocialPlatform) -> Optional[dict]:
        return self.db["social_accounts"].find_one(
            {"workspace_id": str(workspace_id), "platform": platform.value, "is_active": True},
            sort=[("is_default", -1), ("created_at", 1)],
        )

    def _validate_ready_to_publish_dict(self, post: dict, platforms: list[dict]) -> None:
        if not platforms:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Post has no platform content"
            )
        publishable = [pp for pp in platforms if pp.get("platform") in {p.value for p in PUBLISHABLE_PLATFORMS}]
        if not publishable:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Connect at least one publishable account (Facebook, Instagram, LinkedIn, or X)",
            )
        for pp in publishable:
            if not (pp.get("caption") or "").strip() and not post.get("image_url"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{pp.get('platform')} content is empty",
                )
            if not pp.get("social_account_id"):
                account = self._default_account_for_platform(
                    post["workspace_id"], SocialPlatform(pp["platform"])
                )
                if not account:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Connect a {pp.get('platform')} account before publishing",
                    )
                self.db["social_post_platforms"].update_one(
                    {"id": pp["id"]}, {"$set": {"social_account_id": account["id"]}}
                )
                pp["social_account_id"] = account["id"]
            if pp.get("platform") == SocialPlatform.INSTAGRAM.value and not post.get("image_url"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Instagram posts require an image URL",
                )
            if pp.get("platform") == SocialPlatform.X.value and post.get("image_url"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="X free tier is text-only — remove the image before publishing to X",
                )
        from app.social.media import is_video_media_url
        if post.get("image_url") and is_video_media_url(post["image_url"]):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Video publishing is not supported yet — save as draft only",
            )

    def _enqueue_publish(self, post_id: str, eta: Optional[datetime]) -> None:
        from app.social.tasks.publish import publish_post
        import logging
        _logger = logging.getLogger(__name__)
        try:
            if eta is not None:
                publish_post.apply_async(args=[str(post_id)], eta=eta, queue="social_publish")
            else:
                publish_post.apply_async(args=[str(post_id)], queue="social_publish")
        except Exception as exc:
            _logger.warning("Failed to enqueue publish_post for %s: %s", post_id, exc)
            if eta is None:
                try:
                    publish_post.delay(str(post_id))
                except Exception:
                    publish_post.run(str(post_id))

    def _upsert_account(
        self, *, workspace_id: str, platform: SocialPlatform, profile: OAuthAccountProfile, is_default: bool = False
    ) -> dict:
        id_candidates = [profile.platform_account_id]
        if profile.platform_account_id.startswith("person:"):
            id_candidates.append(profile.platform_account_id.split(":", 1)[1])

        existing = self.db["social_accounts"].find_one(
            {
                "workspace_id": str(workspace_id),
                "platform": platform.value,
                "platform_account_id": {"$in": id_candidates},
            }
        )
        if existing:
            self.db["social_accounts"].update_one(
                {"id": existing["id"]}, {"$set": {"platform_account_id": profile.platform_account_id}}
            )
            existing["platform_account_id"] = profile.platform_account_id
            account = self._apply_profile(existing, profile, reactivate=True)
        else:
            now = utcnow()
            doc = {
                "id": new_id(),
                "workspace_id": str(workspace_id),
                "platform": platform.value,
                "platform_account_id": profile.platform_account_id,
                "account_type": profile.account_type.value if hasattr(profile.account_type, "value") else profile.account_type,
                "account_name": profile.account_name,
                "account_picture_url": profile.account_picture_url,
                "follower_count": profile.follower_count,
                "access_token_enc": encrypt(profile.access_token) if profile.access_token else None,
                "refresh_token_enc": encrypt(profile.refresh_token) if profile.refresh_token else None,
                "token_expires_at": profile.token_expires_at,
                "is_default": False,
                "is_active": True,
                "last_synced_at": utcnow(),
                "created_at": now,
                "updated_at": now,
            }
            self.db["social_accounts"].insert_one(doc)
            account = self.db["social_accounts"].find_one({"id": doc["id"]})

        if is_default:
            self._clear_default(str(workspace_id), platform)
            self.db["social_accounts"].update_one({"id": account["id"]}, {"$set": {"is_default": True}})
            account = self.db["social_accounts"].find_one({"id": account["id"]})
        return account

    def _apply_profile(self, account: dict, profile: OAuthAccountProfile, *, reactivate: bool) -> dict:
        updates = {
            "account_type": profile.account_type.value if hasattr(profile.account_type, "value") else profile.account_type,
            "account_name": profile.account_name,
            "account_picture_url": profile.account_picture_url,
            "follower_count": profile.follower_count,
            "access_token_enc": encrypt(profile.access_token) if profile.access_token else None,
            "refresh_token_enc": encrypt(profile.refresh_token) if profile.refresh_token else None,
            "token_expires_at": profile.token_expires_at,
            "last_synced_at": utcnow(),
            "updated_at": utcnow(),
        }
        if reactivate:
            updates["is_active"] = True
        self.db["social_accounts"].update_one({"id": account["id"]}, {"$set": updates})
        return self.db["social_accounts"].find_one({"id": account["id"]})

    def _clear_default(self, workspace_id: str, platform: SocialPlatform) -> None:
        self.db["social_accounts"].update_many(
            {"workspace_id": str(workspace_id), "platform": platform.value, "is_default": True},
            {"$set": {"is_default": False}},
        )

    def _replace_platforms(self, post_id: str, workspace_id: str, platforms: list) -> None:
        self.db["social_post_platforms"].delete_many({"post_id": post_id})
        for item in platforms:
            account_id = None
            if item.socialAccountId:
                account = self.db["social_accounts"].find_one({"id": str(item.socialAccountId)})
                if not account or account.get("workspace_id") != workspace_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Social account {item.socialAccountId} not found in this workspace",
                    )
                platform_val = item.platform.value if hasattr(item.platform, "value") else item.platform
                if account.get("platform") != platform_val:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Platform does not match the selected social account",
                    )
                account_id = account["id"]
            else:
                platform_enum = item.platform if isinstance(item.platform, SocialPlatform) else SocialPlatform(item.platform)
                default = self._default_account_for_platform(workspace_id, platform_enum)
                account_id = default["id"] if default else None

            caption = item.caption or ""
            hashtags = list(item.hashtags or [])
            platform_val = item.platform.value if hasattr(item.platform, "value") else item.platform
            self.db["social_post_platforms"].insert_one(
                {
                    "id": new_id(),
                    "post_id": post_id,
                    "platform": platform_val,
                    "social_account_id": account_id,
                    "caption": caption,
                    "hashtags": hashtags,
                    "first_comment": item.firstComment if hasattr(item, "firstComment") else None,
                    "character_count": len(caption),
                    "status": SocialPlatformPostStatus.PENDING.value,
                    "platform_post_id": None,
                    "published_at": None,
                    "error_code": None,
                    "error_message": None,
                    "retry_count": 0,
                    "next_retry_at": None,
                    "reach": 0,
                    "impressions": 0,
                    "likes": 0,
                    "comments": 0,
                    "shares": 0,
                    "clicks": 0,
                    "engagement_rate": 0.0,
                }
            )

    def _serialize_account(self, account: dict) -> SocialAccountOut:
        return SocialAccountOut(
            id=str(account["id"]),
            workspaceId=str(account.get("workspace_id")),
            platform=SocialPlatform(account.get("platform")),
            accountType=account.get("account_type", "page"),
            platformAccountId=account.get("platform_account_id", ""),
            accountName=account.get("account_name", ""),
            accountPictureUrl=account.get("account_picture_url"),
            followerCount=account.get("follower_count", 0),
            tokenExpiresAt=_iso(account.get("token_expires_at")),
            tokenStatus=_token_status(account),
            isDefault=bool(account.get("is_default")),
            isActive=bool(account.get("is_active", True)),
            lastSyncedAt=_iso(account.get("last_synced_at")),
            createdAt=_iso(account.get("created_at")) or "",
            updatedAt=_iso(account.get("updated_at")) or "",
        )

    def _serialize_post(self, post: dict) -> SocialPostOut:
        from app.social.media import resolve_stored_image_url

        platforms = _get_post_platforms(self.db, post["id"])
        platform_outs = [
            SocialPostPlatformOut(
                id=str(pp["id"]),
                platform=SocialPlatform(pp.get("platform")),
                socialAccountId=str(pp["social_account_id"]) if pp.get("social_account_id") else None,
                caption=pp.get("caption", ""),
                hashtags=list(pp.get("hashtags") or []),
                firstComment=pp.get("first_comment"),
                characterCount=pp.get("character_count", 0),
                status=SocialPlatformPostStatus(pp.get("status", "pending")),
                platformPostId=pp.get("platform_post_id"),
                publishedAt=_iso(pp.get("published_at")),
                errorCode=pp.get("error_code"),
                errorMessage=pp.get("error_message"),
                retryCount=pp.get("retry_count", 0),
                nextRetryAt=_iso(pp.get("next_retry_at")),
                reach=pp.get("reach", 0),
                impressions=pp.get("impressions", 0),
                likes=pp.get("likes", 0),
                comments=pp.get("comments", 0),
                shares=pp.get("shares", 0),
                clicks=pp.get("clicks", 0),
                engagementRate=pp.get("engagement_rate", 0.0),
            )
            for pp in platforms
        ]
        return SocialPostOut(
            id=str(post["id"]),
            workspaceId=str(post.get("workspace_id")),
            createdBy=str(post.get("created_by")),
            title=post.get("title", ""),
            status=SocialPostStatus(post.get("status", "draft")),
            scheduledAt=_iso(post.get("scheduled_at")),
            publishedAt=_iso(post.get("published_at")),
            approvalStatus=post.get("approval_status", "not_required"),
            approvedBy=str(post["approved_by"]) if post.get("approved_by") else None,
            templateId=str(post["template_id"]) if post.get("template_id") else None,
            aiPrompt=post.get("ai_prompt"),
            imageUrl=resolve_stored_image_url(post.get("image_url")),
            imageSource=post.get("image_source", SocialImageSource.NONE.value),
            platforms=platform_outs,
            createdAt=_iso(post.get("created_at")) or "",
            updatedAt=_iso(post.get("updated_at")) or "",
        )
