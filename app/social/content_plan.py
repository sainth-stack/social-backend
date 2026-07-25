"""Day-wise content plan: generate captions (+ images) and schedule auto-publish.

Flow:
  1. Build posting slots from workspace timezone + defaultPostingTimes / blackouts
  2. For each day: AI caption (and image for Instagram) → create post → schedule
  3. Celery publish_post runs at scheduled_at (existing pipeline)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.social.models import (
    SocialAccount,
    SocialImageSource,
    SocialMediaAssetType,
    SocialPlatform,
    SocialPost,
    SocialPostStatus,
)
from app.social.polish import DEFAULT_POSTING_TIMES, SocialPolishService
from app.social.schemas import (
    CalendarPostOut,
    ContentPlanDayOut,
    ContentPlanGenerateRequest,
    ContentPlanGenerateResponse,
    CreateSocialPostRequest,
    SchedulePostRequest,
    SocialPostPlatformIn,
)
from app.users.models import User
from app.workspaces.models import Workspace, WorkspacePlan

logger = logging.getLogger(__name__)

PUBLISHABLE = (SocialPlatform.FACEBOOK, SocialPlatform.INSTAGRAM)

PLAN_DAY_CAP: dict[str, int] = {
    WorkspacePlan.STARTER.value: 7,
    WorkspacePlan.GROWTH.value: 15,
    WorkspacePlan.ENTERPRISE.value: 30,
}

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

TOPIC_ANGLES = [
    "pain → outcome tip that makes the audience want your product",
    "credibility / authority insight that positions the brand as the expert",
    "customer-style win story with a clear before/after benefit",
    "myth-bust that leads into why your approach works",
    "short checklist that saves time/money and soft-sells the brand",
    "trend take with a practical next step (demo, trial, book a call)",
    "feature highlight framed as revenue or productivity ROI",
    "engagement question that reveals buying intent",
    "educational tip that ends in a clear CTA",
    "founder/operator insight that builds trust and leads",
]


def _plan_cap(workspace: Workspace) -> int:
    plan = getattr(workspace.plan, "value", None) or str(workspace.plan or "starter")
    return PLAN_DAY_CAP.get(str(plan).lower(), 7)


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def build_posting_slots(
    *,
    days: int,
    timezone_name: str,
    posting_times: dict[str, Any] | None,
    blackout_dates: list[Any] | None,
    queue_gap_minutes: int = 30,
    start: Optional[datetime] = None,
) -> list[datetime]:
    """Return `days` future UTC datetimes, one primary slot per calendar day."""
    tz = _tz(timezone_name)
    now_local = (start or datetime.now(timezone.utc)).astimezone(tz)
    times_map = posting_times or DEFAULT_POSTING_TIMES
    blackouts = {str(d)[:10] for d in (blackout_dates or []) if d}
    gap = max(15, int(queue_gap_minutes or 30))

    slots: list[datetime] = []
    day_cursor = now_local.date()
    safety = 0
    while len(slots) < days and safety < days * 14:
        safety += 1
        key = WEEKDAY_KEYS[day_cursor.weekday()]
        day_str = day_cursor.isoformat()
        if day_str in blackouts:
            day_cursor += timedelta(days=1)
            continue

        raw_times = times_map.get(key) or ["10:00"]
        if isinstance(raw_times, str):
            raw_times = [raw_times]

        chosen: Optional[datetime] = None
        for t in raw_times:
            try:
                hh, mm = str(t).split(":")[:2]
                local_dt = datetime(
                    day_cursor.year,
                    day_cursor.month,
                    day_cursor.day,
                    int(hh),
                    int(mm),
                    tzinfo=tz,
                )
            except (ValueError, TypeError):
                continue
            if local_dt <= now_local + timedelta(minutes=5):
                continue
            if slots:
                prev = slots[-1].astimezone(tz)
                if local_dt < prev + timedelta(minutes=gap):
                    continue
            chosen = local_dt
            break

        if chosen is None and day_cursor == now_local.date():
            day_cursor += timedelta(days=1)
            continue

        if chosen is None:
            fallback = datetime(
                day_cursor.year, day_cursor.month, day_cursor.day, 10, 0, tzinfo=tz
            )
            if fallback > now_local + timedelta(minutes=5):
                if not slots or fallback >= slots[-1].astimezone(tz) + timedelta(
                    minutes=gap
                ):
                    chosen = fallback

        if chosen is not None:
            slots.append(chosen.astimezone(timezone.utc))
        day_cursor += timedelta(days=1)

    if len(slots) < days:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Could only find {len(slots)} posting slots. "
                "Check default posting times and blackout dates in Settings."
            ),
        )
    return slots


def _connected_publishable(db: Session, workspace_id: uuid.UUID) -> list[SocialAccount]:
    return list(
        db.scalars(
            select(SocialAccount)
            .where(
                SocialAccount.workspace_id == workspace_id,
                SocialAccount.is_active.is_(True),
                SocialAccount.platform.in_(list(PUBLISHABLE)),
            )
            .order_by(SocialAccount.is_default.desc(), SocialAccount.created_at.asc())
        ).all()
    )


class ContentPlanService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def generate(
        self,
        workspace: Workspace,
        user: User,
        payload: ContentPlanGenerateRequest,
    ) -> ContentPlanGenerateResponse:
        from app.plans.service import record_ai_usage
        from app.social.ai.generator import generate_platform_content
        from app.social.ai.image_generator import generate_post_image
        from app.social.limits import enforce_ai_image_limit, enforce_ai_text_limit, enforce_posts_limit
        from app.social.media import SocialBlobUpload, upload_social_image_bytes
        from app.social.models import SocialPermission
        from app.social.permissions import require_permission
        from app.social.service import SocialMediaService

        require_permission(self.db, workspace, user, SocialPermission.EDITOR)

        cap = _plan_cap(workspace)
        days = min(int(payload.days), cap)
        if days < 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="days must be at least 1",
            )

        accounts = _connected_publishable(self.db, workspace.id)
        if not accounts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Connect a Facebook or Instagram account first. "
                    "Auto-scheduling and posting require a publishable platform."
                ),
            )

        settings = SocialPolishService(self.db).get_settings(workspace)
        tz_name = settings.get("timezone") or "UTC"
        slots = build_posting_slots(
            days=days,
            timezone_name=tz_name,
            posting_times=settings.get("defaultPostingTimes"),
            blackout_dates=settings.get("blackoutDates"),
            queue_gap_minutes=int(settings.get("queueGapMinutes") or 30),
        )

        social = SocialMediaService(self.db)
        brand = social._brand_voice_dict(workspace.id)
        brand_name = (brand.get("brandName") or workspace.name or "our brand").strip()
        tone = (
            payload.tone
            or settings.get("defaultTone")
            or ((brand.get("tones") or ["Professional"])[0])
        )
        cta = payload.cta or settings.get("defaultCta") or None
        theme = (payload.theme or "").strip()
        image_style = settings.get("imageGenerationStyle")

        day_outs: list[ContentPlanDayOut] = []
        calendar_items: list[CalendarPostOut] = []
        scheduled_count = 0
        draft_count = 0
        errors: list[str] = []

        for i, slot in enumerate(slots):
            account = accounts[i % len(accounts)]
            platform = account.platform
            angle = TOPIC_ANGLES[i % len(TOPIC_ANGLES)]
            topic = (
                f"Content plan day {i + 1} for {brand_name}: {angle}."
                + (f" Campaign theme: {theme}." if theme else "")
                + " Write one ready-to-publish, professional, conversion-focused social post "
                "that can generate leads or revenue. Elevate any thin brief into impressive copy."
            )

            try:
                enforce_posts_limit(self.db, workspace)
                enforce_ai_text_limit(self.db, workspace)
            except HTTPException as exc:
                errors.append(f"Day {i + 1}: {exc.detail}")
                break

            # ── Caption ──────────────────────────────────────────────────────
            try:
                record_ai_usage(self.db, workspace.id, "text", user_id=user.id)
                self.db.commit()
                result = generate_platform_content(
                    topic=topic,
                    tone=tone,
                    platforms=[platform.value],
                    audience=brand.get("targetAudience"),
                    cta=cta,
                    include_hashtags=True,
                    include_comment=False,
                    brand_voice=brand,
                )
            except Exception as exc:
                logger.warning("Plan day %s caption failed: %s", i + 1, exc)
                errors.append(f"Day {i + 1}: caption generation failed")
                continue

            pc = result.get(platform.value) or {}
            caption = (pc.get("caption") or "").strip()
            hashtags = list(pc.get("hashtags") or [])
            if not caption:
                errors.append(f"Day {i + 1}: empty caption")
                continue

            # ── Image (required for Instagram; optional otherwise) ────────────
            image_url: Optional[str] = None
            image_source = SocialImageSource.NONE
            need_image = platform == SocialPlatform.INSTAGRAM or payload.generateImages
            if need_image:
                try:
                    enforce_ai_image_limit(self.db, workspace)
                    record_ai_usage(self.db, workspace.id, "image", user_id=user.id)
                    self.db.commit()
                    img_data = generate_post_image(
                        topic=f"Social media image for: {caption[:180]}",
                        style=image_style,
                        size="1024x1024",
                        mode="create",
                    )
                    upload: Optional[SocialBlobUpload] = None
                    if img_data.get("imageB64"):
                        upload = upload_social_image_bytes(
                            workspace.id,
                            img_data["imageB64"],  # type: ignore[arg-type]
                            content_type="image/png",
                        )
                    if upload:
                        social._record_media_asset(
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
                        image_url = upload.url
                        image_source = SocialImageSource.AI_GENERATED
                except Exception as exc:
                    logger.warning("Plan day %s image failed: %s", i + 1, exc)
                    if platform == SocialPlatform.INSTAGRAM:
                        errors.append(
                            f"Day {i + 1}: Instagram needs an image — skipped"
                        )
                        continue

            title = f"Day {i + 1}: {caption[:60]}"
            local_slot = slot.astimezone(_tz(tz_name))

            try:
                created = social.create_post(
                    workspace,
                    user,
                    CreateSocialPostRequest(
                        title=title,
                        status=SocialPostStatus.DRAFT,
                        imageUrl=image_url,
                        imageSource=image_source,
                        aiPrompt=topic,
                        platforms=[
                            SocialPostPlatformIn(
                                platform=platform,
                                socialAccountId=str(account.id),
                                caption=caption,
                                hashtags=hashtags,
                            )
                        ],
                    ),
                )
                post_id = created.id
                final_status = SocialPostStatus.DRAFT
                scheduled_at_iso: Optional[str] = None

                if payload.autoSchedule:
                    try:
                        post = self._load_post(uuid.UUID(post_id))
                        scheduled = social.schedule_post(
                            post,
                            SchedulePostRequest(scheduledAt=slot.isoformat()),
                        )
                        final_status = scheduled.status
                        scheduled_at_iso = scheduled.scheduledAt
                        scheduled_count += 1
                    except Exception as exc:
                        logger.warning("Plan day %s schedule failed: %s", i + 1, exc)
                        errors.append(
                            f"Day {i + 1}: saved as draft (schedule failed)"
                        )
                        draft_count += 1
                else:
                    draft_count += 1

                day_outs.append(
                    ContentPlanDayOut(
                        dayIndex=i + 1,
                        date=local_slot.date().isoformat(),
                        weekday=local_slot.strftime("%a"),
                        scheduledAt=scheduled_at_iso or slot.isoformat(),
                        platform=platform,
                        topic=angle,
                        title=title,
                        caption=caption,
                        hashtags=hashtags,
                        imageUrl=image_url,
                        postId=post_id,
                        status=final_status,
                    )
                )
                calendar_items.append(
                    CalendarPostOut(
                        id=post_id,
                        title=title,
                        status=final_status,
                        scheduledAt=scheduled_at_iso or slot.isoformat(),
                        publishedAt=None,
                        platforms=[platform],
                        captionPreview=caption[:80],
                        imageUrl=image_url,
                    )
                )
            except Exception as exc:
                logger.exception("Plan day %s create failed", i + 1)
                errors.append(f"Day {i + 1}: could not create post")

        return ContentPlanGenerateResponse(
            days=len(day_outs),
            timezone=tz_name,
            autoScheduled=payload.autoSchedule,
            scheduledCount=scheduled_count,
            draftCount=draft_count,
            items=day_outs,
            calendarItems=calendar_items,
            errors=errors,
            message=(
                f"Planned {len(day_outs)} day(s)"
                + (
                    f", {scheduled_count} set to auto-post"
                    if scheduled_count
                    else ""
                )
                + ("." if not errors else f" · {len(errors)} warning(s).")
            ),
        )

    def _load_post(self, post_id: uuid.UUID) -> SocialPost:
        post = self.db.scalars(
            select(SocialPost)
            .options(selectinload(SocialPost.platforms))
            .where(SocialPost.id == post_id)
        ).first()
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")
        return post
