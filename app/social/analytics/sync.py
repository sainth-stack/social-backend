"""Pull metrics from platform APIs and upsert daily analytics rows."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from pymongo.database import Database

from app.core.config import settings
from app.core.encryption import decrypt
from app.core.mongo_utils import new_id, utcnow
from app.social.models import SocialPlatform, SocialPlatformPostStatus
from app.social.oauth.base import get_oauth_handler

logger = logging.getLogger(__name__)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _engagement(pp: dict) -> int:
    return int(pp.get("likes") or 0) + int(pp.get("comments") or 0) + int(pp.get("shares") or 0)


def sync_account_daily(
    db: Database, account: dict, day: Optional[date] = None
) -> dict:
    """Upsert today's analytics row for one account from posts + follower sync."""
    day = day or _utc_today()
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    follower_count = int(account.get("follower_count") or 0)
    if account.get("access_token_enc"):
        try:
            token = decrypt(account["access_token_enc"])
            handler = get_oauth_handler(SocialPlatform(account["platform"]))
            stats = handler.sync_account_stats(account["platform_account_id"], token)
            if stats.get("follower_count") is not None:
                follower_count = int(stats["follower_count"])
                db["social_accounts"].update_one(
                    {"id": account["id"]}, {"$set": {"follower_count": follower_count}}
                )
            updates: dict = {}
            if stats.get("account_name"):
                updates["account_name"] = stats["account_name"]
            if stats.get("account_picture_url") is not None:
                updates["account_picture_url"] = stats["account_picture_url"]
            updates["last_synced_at"] = utcnow()
            db["social_accounts"].update_one({"id": account["id"]}, {"$set": updates})
            account = db["social_accounts"].find_one({"id": account["id"]})
        except Exception as exc:
            logger.warning("Follower sync failed for account %s: %s", account["id"], exc)

    # Previous day follower count for new_followers
    prev_day = (datetime.combine(day, datetime.min.time()) - timedelta(days=1)).date()
    prev = db["social_analytics_daily"].find_one(
        {"social_account_id": account["id"], "date": prev_day}
    )
    prev_followers = int(prev.get("follower_count") or follower_count) if prev else follower_count
    new_followers = max(0, follower_count - prev_followers)

    # Get published platforms in this day window
    post_ids = [
        p["id"]
        for p in db["social_posts"].find({"workspace_id": account["workspace_id"]})
    ]
    platforms = list(
        db["social_post_platforms"].find(
            {
                "social_account_id": account["id"],
                "status": SocialPlatformPostStatus.PUBLISHED.value,
                "published_at": {"$gte": start, "$lt": end},
            }
        )
    )

    posts_count = len(platforms)
    total_reach = sum(int(p.get("reach") or 0) for p in platforms)
    total_impressions = sum(int(p.get("impressions") or 0) for p in platforms)
    total_engagements = sum(_engagement(p) for p in platforms)
    total_clicks = sum(int(p.get("clicks") or 0) for p in platforms)

    row = db["social_analytics_daily"].find_one(
        {"social_account_id": account["id"], "date": day}
    )

    update_doc = {
        "workspace_id": account["workspace_id"],
        "social_account_id": account["id"],
        "platform": account["platform"],
        "date": day,
        "follower_count": follower_count,
        "new_followers": new_followers,
        "posts_count": posts_count,
        "total_reach": total_reach,
        "total_impressions": total_impressions,
        "total_engagements": total_engagements,
        "total_clicks": total_clicks,
    }

    if not row:
        update_doc["id"] = new_id()
        db["social_analytics_daily"].insert_one(update_doc)
        return update_doc
    else:
        db["social_analytics_daily"].update_one(
            {"social_account_id": account["id"], "date": day}, {"$set": update_doc}
        )
        return db["social_analytics_daily"].find_one(
            {"social_account_id": account["id"], "date": day}
        )


def sync_org_platform_analytics(
    db: Database, workspace_id: str, day: Optional[date] = None
) -> int:
    accounts = list(
        db["social_accounts"].find({"workspace_id": str(workspace_id), "is_active": True})
    )
    count = 0
    for account in accounts:
        sync_account_daily(db, account, day=day)
        count += 1
    return count


def sync_all_orgs_platform_analytics(db: Database) -> int:
    pipeline = [{"$group": {"_id": "$workspace_id"}}]
    workspace_ids = [row["_id"] for row in db["social_accounts"].aggregate(pipeline)]
    total = 0
    for workspace_id in workspace_ids:
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


def sync_recent_post_metrics(db: Database, days: int = 7) -> int:
    """Refresh metrics for posts published in the last N days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = list(
        db["social_post_platforms"].find(
            {
                "status": SocialPlatformPostStatus.PUBLISHED.value,
                "platform_post_id": {"$ne": None},
                "published_at": {"$gte": since},
            }
        )
    )

    updated = 0
    for pp in rows:
        account = db["social_accounts"].find_one({"id": pp.get("social_account_id")})
        if not account or not account.get("access_token_enc") or not pp.get("platform_post_id"):
            continue
        token = decrypt(account["access_token_enc"])
        metrics: dict = {}
        try:
            platform = pp.get("platform", "")
            if platform == SocialPlatform.FACEBOOK.value:
                metrics = _fetch_facebook_post_metrics(pp["platform_post_id"], token)
            elif platform == SocialPlatform.INSTAGRAM.value:
                metrics = _fetch_instagram_post_metrics(pp["platform_post_id"], token)
        except Exception as exc:
            logger.warning("Post metrics sync failed for %s: %s", pp.get("id"), exc)
            continue

        if not metrics:
            eng = _engagement(pp)
            impressions = pp.get("impressions") or pp.get("reach") or 0
            if impressions and not pp.get("engagement_rate"):
                db["social_post_platforms"].update_one(
                    {"id": pp["id"]},
                    {"$set": {"engagement_rate": round(eng / impressions, 4)}},
                )
            continue

        db["social_post_platforms"].update_one(
            {"id": pp["id"]},
            {
                "$set": {
                    "likes": metrics.get("likes", pp.get("likes")),
                    "comments": metrics.get("comments", pp.get("comments")),
                    "shares": metrics.get("shares", pp.get("shares")),
                    "reach": metrics.get("reach", pp.get("reach")),
                    "impressions": metrics.get("impressions", pp.get("impressions")),
                    "clicks": metrics.get("clicks", pp.get("clicks")),
                    "engagement_rate": metrics.get("engagement_rate", pp.get("engagement_rate")),
                }
            },
        )
        updated += 1

    return updated
