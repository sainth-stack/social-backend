"""Pull metrics from platform APIs and upsert daily analytics rows."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.encryption import decrypt
from app.social.models import (
    SocialAccount,
    SocialAnalyticsDaily,
    SocialPlatform,
    SocialPlatformPostStatus,
    SocialPost,
    SocialPostPlatform,
    SocialPostStatus,
)
from app.social.oauth.base import get_oauth_handler

logger = logging.getLogger(__name__)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _engagement(pp: SocialPostPlatform) -> int:
    return int(pp.likes or 0) + int(pp.comments or 0) + int(pp.shares or 0)


def sync_account_daily(db: Session, account: SocialAccount, day: Optional[date] = None) -> SocialAnalyticsDaily:
    """Upsert today's analytics row for one account from posts + follower sync."""
    day = day or _utc_today()
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    follower_count = account.follower_count or 0
    if account.access_token_enc:
        try:
            token = decrypt(account.access_token_enc)
            handler = get_oauth_handler(account.platform)
            stats = handler.sync_account_stats(account.platform_account_id, token)
            if stats.get("follower_count") is not None:
                follower_count = int(stats["follower_count"])
                account.follower_count = follower_count
            if stats.get("account_name"):
                account.account_name = stats["account_name"]
            if stats.get("account_picture_url") is not None:
                account.account_picture_url = stats["account_picture_url"]
            account.last_synced_at = datetime.now(timezone.utc)
        except Exception as exc:
            logger.warning("Follower sync failed for account %s: %s", account.id, exc)

    # Previous day follower count for new_followers
    prev = db.scalars(
        select(SocialAnalyticsDaily).where(
            SocialAnalyticsDaily.social_account_id == account.id,
            SocialAnalyticsDaily.date == day - timedelta(days=1),
        )
    ).first()
    prev_followers = prev.follower_count if prev else follower_count
    new_followers = max(0, follower_count - prev_followers)

    platforms = db.scalars(
        select(SocialPostPlatform)
        .join(SocialPost, SocialPost.id == SocialPostPlatform.post_id)
        .where(
            SocialPostPlatform.social_account_id == account.id,
            SocialPostPlatform.status == SocialPlatformPostStatus.PUBLISHED,
            SocialPostPlatform.published_at.is_not(None),
            SocialPostPlatform.published_at >= start,
            SocialPostPlatform.published_at < end,
        )
    ).all()

    posts_count = len(platforms)
    total_reach = sum(int(p.reach or 0) for p in platforms)
    total_impressions = sum(int(p.impressions or 0) for p in platforms)
    total_engagements = sum(_engagement(p) for p in platforms)
    total_clicks = sum(int(p.clicks or 0) for p in platforms)

    row = db.scalars(
        select(SocialAnalyticsDaily).where(
            SocialAnalyticsDaily.social_account_id == account.id,
            SocialAnalyticsDaily.date == day,
        )
    ).first()
    if not row:
        row = SocialAnalyticsDaily(
            id=uuid.uuid4(),
            workspace_id=account.workspace_id,
            social_account_id=account.id,
            platform=account.platform,
            date=day,
        )
        db.add(row)

    row.follower_count = follower_count
    row.new_followers = new_followers
    row.posts_count = posts_count
    row.total_reach = total_reach
    row.total_impressions = total_impressions
    row.total_engagements = total_engagements
    row.total_clicks = total_clicks
    db.flush()
    return row


def sync_org_platform_analytics(db: Session, workspace_id: uuid.UUID, day: Optional[date] = None) -> int:
    accounts = db.scalars(
        select(SocialAccount).where(
            SocialAccount.workspace_id == workspace_id,
            SocialAccount.is_active.is_(True),
        )
    ).all()
    count = 0
    for account in accounts:
        sync_account_daily(db, account, day=day)
        count += 1
    db.commit()
    return count


def sync_all_orgs_platform_analytics(db: Session) -> int:
    org_ids = db.scalars(select(SocialAccount.workspace_id).distinct()).all()
    total = 0
    for workspace_id in org_ids:
        total += sync_org_platform_analytics(db, workspace_id)
    return total


def _fetch_facebook_post_metrics(platform_post_id: str, token: str) -> dict:
    api = settings.meta_api_version
    url = f"https://graph.facebook.com/{api}/{platform_post_id}"
    params = {
        "fields": "shares,likes.summary(true),comments.summary(true)",
        "access_token": token,
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.get(url, params=params)
        if response.status_code >= 400:
            return {}
        data = response.json()
    likes = int((data.get("likes") or {}).get("summary", {}).get("total_count") or 0)
    comments = int((data.get("comments") or {}).get("summary", {}).get("total_count") or 0)
    shares = int((data.get("shares") or {}).get("count") or 0)
    # Insights (may require permissions)
    insights_url = f"https://graph.facebook.com/{api}/{platform_post_id}/insights"
    reach = impressions = clicks = 0
    try:
        with httpx.Client(timeout=30.0) as client:
            ir = client.get(
                insights_url,
                params={
                    "metric": "post_impressions,post_engaged_users,post_clicks",
                    "access_token": token,
                },
            )
            if ir.status_code == 200:
                for item in ir.json().get("data") or []:
                    name = item.get("name")
                    values = item.get("values") or []
                    val = int((values[0] or {}).get("value") or 0) if values else 0
                    if name == "post_impressions":
                        impressions = val
                        reach = max(reach, val)
                    elif name == "post_engaged_users":
                        pass
                    elif name == "post_clicks":
                        clicks = val
    except Exception:
        pass
    engagements = likes + comments + shares
    return {
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "reach": reach or impressions,
        "impressions": impressions or reach,
        "clicks": clicks,
        "engagement_rate": round(engagements / impressions, 4) if impressions else 0.0,
    }


def _fetch_instagram_post_metrics(platform_post_id: str, token: str) -> dict:
    api = settings.meta_api_version
    # Instagram Login tokens are valid on graph.instagram.com.
    url = f"https://graph.instagram.com/{api}/{platform_post_id}/insights"
    params = {
        "metric": "impressions,reach,likes,comments,shares,saved",
        "access_token": token,
    }
    metrics: dict[str, int] = {}
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, params=params)
            if response.status_code >= 400:
                return {}
            for item in response.json().get("data") or []:
                name = item.get("name")
                values = item.get("values") or []
                metrics[name] = int((values[0] or {}).get("value") or 0) if values else 0
    except Exception:
        return {}
    likes = metrics.get("likes", 0)
    comments = metrics.get("comments", 0)
    shares = metrics.get("shares", 0)
    impressions = metrics.get("impressions", 0)
    reach = metrics.get("reach", 0)
    engagements = likes + comments + shares + metrics.get("saved", 0)
    return {
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "reach": reach,
        "impressions": impressions,
        "clicks": 0,
        "engagement_rate": round(engagements / impressions, 4) if impressions else 0.0,
    }


def sync_recent_post_metrics(db: Session, days: int = 7) -> int:
    """Refresh metrics for posts published in the last N days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.scalars(
        select(SocialPostPlatform)
        .options(selectinload(SocialPostPlatform.social_account))
        .where(
            SocialPostPlatform.status == SocialPlatformPostStatus.PUBLISHED,
            SocialPostPlatform.platform_post_id.is_not(None),
            SocialPostPlatform.published_at.is_not(None),
            SocialPostPlatform.published_at >= since,
        )
    ).all()

    updated = 0
    for pp in rows:
        account = pp.social_account
        if not account or not account.access_token_enc or not pp.platform_post_id:
            continue
        token = decrypt(account.access_token_enc)
        metrics: dict = {}
        try:
            if pp.platform == SocialPlatform.FACEBOOK:
                metrics = _fetch_facebook_post_metrics(pp.platform_post_id, token)
            elif pp.platform == SocialPlatform.INSTAGRAM:
                metrics = _fetch_instagram_post_metrics(pp.platform_post_id, token)
            # LinkedIn / X: limited free-tier insights — leave stored values
        except Exception as exc:
            logger.warning("Post metrics sync failed for %s: %s", pp.id, exc)
            continue

        if not metrics:
            # Fallback: derive engagement_rate from existing counters
            eng = _engagement(pp)
            impressions = pp.impressions or pp.reach or 0
            if impressions and not pp.engagement_rate:
                pp.engagement_rate = round(eng / impressions, 4)
            continue

        pp.likes = metrics.get("likes", pp.likes)
        pp.comments = metrics.get("comments", pp.comments)
        pp.shares = metrics.get("shares", pp.shares)
        pp.reach = metrics.get("reach", pp.reach)
        pp.impressions = metrics.get("impressions", pp.impressions)
        pp.clicks = metrics.get("clicks", pp.clicks)
        pp.engagement_rate = metrics.get("engagement_rate", pp.engagement_rate)
        updated += 1

    db.commit()
    return updated
