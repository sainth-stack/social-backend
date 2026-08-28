"""Cross-platform analytics aggregates for API responses."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.social.models import (
    SocialAnalyticsDaily,
    SocialPlatform,
    SocialPlatformPostStatus,
    SocialPost,
    SocialPostPlatform,
)


def _parse_range(from_date: Optional[str], to_date: Optional[str]) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    if to_date:
        end = date.fromisoformat(to_date[:10])
    else:
        end = today
    if from_date:
        start = date.fromisoformat(from_date[:10])
    else:
        start = end - timedelta(days=29)
    if start > end:
        start, end = end, start
    return start, end


def _engagement(pp: SocialPostPlatform) -> int:
    return int(pp.likes or 0) + int(pp.comments or 0) + int(pp.shares or 0)


class AnalyticsAggregator:
    def __init__(self, db: Session) -> None:
        self.db = db

    def overview(self, workspace_id: uuid.UUID, from_date: Optional[str], to_date: Optional[str]) -> dict:
        start, end = _parse_range(from_date, to_date)
        rows = self._daily_rows(workspace_id, start, end)
        if not rows:
            rows = self._synthetic_daily_from_posts(workspace_id, start, end)

        total_posts = sum(int(r.posts_count or 0) for r in rows)
        total_reach = sum(int(r.total_reach or 0) for r in rows)
        total_impressions = sum(int(r.total_impressions or 0) for r in rows)
        total_engagements = sum(int(r.total_engagements or 0) for r in rows)
        total_clicks = sum(int(r.total_clicks or 0) for r in rows)
        follower_growth = sum(int(r.new_followers or 0) for r in rows)
        avg_engagement_rate = (
            round(total_engagements / total_impressions, 4) if total_impressions else 0.0
        )

        # Engagement over time by platform
        by_day_platform: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in rows:
            key = r.date.isoformat()
            by_day_platform[key][r.platform.value] += int(r.total_engagements or 0)

        engagement_series = []
        day = start
        while day <= end:
            key = day.isoformat()
            point = {"date": key}
            for platform in SocialPlatform:
                point[platform.value] = by_day_platform[key].get(platform.value, 0)
            engagement_series.append(point)
            day += timedelta(days=1)

        # Reach by platform (weekly buckets)
        reach_by_platform: dict[str, int] = defaultdict(int)
        for r in rows:
            reach_by_platform[r.platform.value] += int(r.total_reach or 0)
        reach_chart = [
            {"platform": p, "reach": reach_by_platform.get(p, 0)}
            for p in [e.value for e in SocialPlatform]
        ]

        comparison = self._platform_comparison(workspace_id, start, end, rows)
        return {
            "fromDate": start.isoformat(),
            "toDate": end.isoformat(),
            "metrics": {
                "totalPosts": total_posts,
                "totalReach": total_reach,
                "totalImpressions": total_impressions,
                "totalEngagements": total_engagements,
                "avgEngagementRate": avg_engagement_rate,
                "followerGrowth": follower_growth,
                "totalClicks": total_clicks,
            },
            "engagementSeries": engagement_series,
            "reachByPlatform": reach_chart,
            "platformComparison": comparison,
        }

    def platform(
        self,
        workspace_id: uuid.UUID,
        platform: SocialPlatform,
        from_date: Optional[str],
        to_date: Optional[str],
    ) -> dict:
        start, end = _parse_range(from_date, to_date)
        all_rows = self._daily_rows(workspace_id, start, end)
        if not all_rows:
            all_rows = self._synthetic_daily_from_posts(workspace_id, start, end)
        rows = [r for r in all_rows if r.platform == platform]

        series = []
        day = start
        by_day = {r.date: r for r in rows}
        while day <= end:
            r = by_day.get(day)
            series.append(
                {
                    "date": day.isoformat(),
                    "impressions": int(r.total_impressions or 0) if r else 0,
                    "reach": int(r.total_reach or 0) if r else 0,
                    "engagement": int(r.total_engagements or 0) if r else 0,
                    "clicks": int(r.total_clicks or 0) if r else 0,
                    "followers": int(r.follower_count or 0) if r else 0,
                    "newFollowers": int(r.new_followers or 0) if r else 0,
                }
            )
            day += timedelta(days=1)

        totals = {
            "posts": sum(int(r.posts_count or 0) for r in rows),
            "reach": sum(int(r.total_reach or 0) for r in rows),
            "impressions": sum(int(r.total_impressions or 0) for r in rows),
            "engagements": sum(int(r.total_engagements or 0) for r in rows),
            "clicks": sum(int(r.total_clicks or 0) for r in rows),
            "followerGrowth": sum(int(r.new_followers or 0) for r in rows),
            "latestFollowers": int(rows[-1].follower_count or 0) if rows else 0,
        }

        # Post type breakdown (image vs text) from published posts in range
        start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        posts = self.db.scalars(
            select(SocialPostPlatform)
            .join(SocialPost, SocialPost.id == SocialPostPlatform.post_id)
            .options(selectinload(SocialPostPlatform.post))
            .where(
                SocialPost.workspace_id == workspace_id,
                SocialPostPlatform.platform == platform,
                SocialPostPlatform.status == SocialPlatformPostStatus.PUBLISHED,
                SocialPostPlatform.published_at.is_not(None),
                SocialPostPlatform.published_at >= start_dt,
                SocialPostPlatform.published_at < end_dt,
            )
        ).all()
        image_posts = sum(1 for p in posts if p.post and p.post.image_url)
        text_posts = len(posts) - image_posts
        post_types = [
            {"type": "image", "count": image_posts},
            {"type": "text", "count": text_posts},
        ]

        return {
            "platform": platform.value,
            "fromDate": start.isoformat(),
            "toDate": end.isoformat(),
            "metrics": totals,
            "series": series,
            "postTypes": post_types,
        }

    def posts(
        self,
        workspace_id: uuid.UUID,
        from_date: Optional[str],
        to_date: Optional[str],
        sort: str = "engagementRate",
        order: str = "desc",
    ) -> dict:
        start, end = _parse_range(from_date, to_date)
        start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)

        rows = self.db.scalars(
            select(SocialPostPlatform)
            .join(SocialPost, SocialPost.id == SocialPostPlatform.post_id)
            .options(selectinload(SocialPostPlatform.post))
            .where(
                SocialPost.workspace_id == workspace_id,
                SocialPostPlatform.status == SocialPlatformPostStatus.PUBLISHED,
                SocialPostPlatform.published_at.is_not(None),
                SocialPostPlatform.published_at >= start_dt,
                SocialPostPlatform.published_at < end_dt,
            )
        ).all()

        items = []
        for pp in rows:
            post = pp.post
            eng = _engagement(pp)
            rate = pp.engagement_rate or (
                round(eng / pp.impressions, 4) if pp.impressions else 0.0
            )
            items.append(
                {
                    "postId": str(post.id) if post else str(pp.post_id),
                    "platformRowId": str(pp.id),
                    "caption": (pp.caption or "")[:120],
                    "platform": pp.platform.value,
                    "publishedAt": pp.published_at.isoformat() if pp.published_at else None,
                    "reach": pp.reach,
                    "impressions": pp.impressions,
                    "likes": pp.likes,
                    "comments": pp.comments,
                    "shares": pp.shares,
                    "clicks": pp.clicks,
                    "engagementRate": rate,
                    "engagements": eng,
                    "imageUrl": post.image_url if post else None,
                }
            )

        sort_key_map = {
            "reach": "reach",
            "impressions": "impressions",
            "engagement": "engagements",
            "engagements": "engagements",
            "engagement_rate": "engagementRate",
            "engagementRate": "engagementRate",
            "clicks": "clicks",
            "publishedAt": "publishedAt",
        }
        key = sort_key_map.get(sort, "engagementRate")
        reverse = order.lower() != "asc"
        items.sort(key=lambda x: (x.get(key) is None, x.get(key) or 0), reverse=reverse)

        return {
            "fromDate": start.isoformat(),
            "toDate": end.isoformat(),
            "items": items,
        }

    def audience(
        self,
        workspace_id: uuid.UUID,
        from_date: Optional[str],
        to_date: Optional[str],
    ) -> dict:
        start, end = _parse_range(from_date, to_date)
        rows = self._daily_rows(workspace_id, start, end)
        if not rows:
            rows = self._synthetic_daily_from_posts(workspace_id, start, end)

        by_day_platform: dict[str, dict[str, dict]] = defaultdict(dict)
        for r in rows:
            by_day_platform[r.date.isoformat()][r.platform.value] = {
                "followers": int(r.follower_count or 0),
                "newFollowers": int(r.new_followers or 0),
            }

        series = []
        day = start
        while day <= end:
            key = day.isoformat()
            point: dict = {"date": key}
            for platform in SocialPlatform:
                data = by_day_platform[key].get(platform.value) or {}
                point[platform.value] = data.get("followers", 0)
                point[f"{platform.value}New"] = data.get("newFollowers", 0)
            series.append(point)
            day += timedelta(days=1)

        # Latest follower cards per platform
        cards = []
        for platform in SocialPlatform:
            platform_rows = [r for r in rows if r.platform == platform]
            latest = int(platform_rows[-1].follower_count or 0) if platform_rows else 0
            growth = sum(int(r.new_followers or 0) for r in platform_rows)
            cards.append(
                {
                    "platform": platform.value,
                    "followers": latest,
                    "growth": growth,
                }
            )

        net_new = []
        day = start
        while day <= end:
            key = day.isoformat()
            total_new = sum(
                (by_day_platform[key].get(p.value) or {}).get("newFollowers", 0)
                for p in SocialPlatform
            )
            net_new.append({"date": key, "newFollowers": total_new})
            day += timedelta(days=1)

        return {
            "fromDate": start.isoformat(),
            "toDate": end.isoformat(),
            "series": series,
            "netNewFollowers": net_new,
            "platformCards": cards,
        }

    def _daily_rows(
        self, workspace_id: uuid.UUID, start: date, end: date
    ) -> list[SocialAnalyticsDaily]:
        return list(
            self.db.scalars(
                select(SocialAnalyticsDaily)
                .where(
                    SocialAnalyticsDaily.workspace_id == workspace_id,
                    SocialAnalyticsDaily.date >= start,
                    SocialAnalyticsDaily.date <= end,
                )
                .order_by(SocialAnalyticsDaily.date.asc())
            ).all()
        )

    def _synthetic_daily_from_posts(
        self, workspace_id: uuid.UUID, start: date, end: date
    ) -> list[SocialAnalyticsDaily]:
        """Build in-memory daily rows from published posts when sync has not run yet."""
        start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        published = self.db.scalars(
            select(SocialPostPlatform)
            .join(SocialPost, SocialPost.id == SocialPostPlatform.post_id)
            .options(selectinload(SocialPostPlatform.social_account))
            .where(
                SocialPost.workspace_id == workspace_id,
                SocialPostPlatform.status == SocialPlatformPostStatus.PUBLISHED,
                SocialPostPlatform.published_at.is_not(None),
                SocialPostPlatform.published_at >= start_dt,
                SocialPostPlatform.published_at < end_dt,
            )
        ).all()
        buckets: dict[tuple[date, str], SocialAnalyticsDaily] = {}
        for pp in published:
            if not pp.published_at:
                continue
            day = pp.published_at.astimezone(timezone.utc).date()
            key = (day, pp.platform.value)
            row = buckets.get(key)
            if not row:
                account = pp.social_account
                # SQLAlchemy column defaults apply on INSERT, not in-memory objects.
                row = SocialAnalyticsDaily(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    social_account_id=pp.social_account_id or uuid.uuid4(),
                    platform=pp.platform,
                    date=day,
                    follower_count=int(account.follower_count or 0) if account else 0,
                    new_followers=0,
                    posts_count=0,
                    total_reach=0,
                    total_impressions=0,
                    total_engagements=0,
                    total_clicks=0,
                )
                buckets[key] = row
            row.posts_count = int(row.posts_count or 0) + 1
            row.total_reach = int(row.total_reach or 0) + int(pp.reach or 0)
            row.total_impressions = int(row.total_impressions or 0) + int(
                pp.impressions or 0
            )
            row.total_engagements = int(row.total_engagements or 0) + _engagement(pp)
            row.total_clicks = int(row.total_clicks or 0) + int(pp.clicks or 0)
        return sorted(buckets.values(), key=lambda r: r.date)

    def _platform_comparison(
        self,
        workspace_id: uuid.UUID,
        start: date,
        end: date,
        rows: list[SocialAnalyticsDaily],
    ) -> list[dict]:
        by_platform: dict[str, dict] = defaultdict(
            lambda: {
                "posts": 0,
                "reach": 0,
                "impressions": 0,
                "engagements": 0,
            }
        )
        for r in rows:
            p = r.platform.value
            by_platform[p]["posts"] += int(r.posts_count or 0)
            by_platform[p]["reach"] += int(r.total_reach or 0)
            by_platform[p]["impressions"] += int(r.total_impressions or 0)
            by_platform[p]["engagements"] += int(r.total_engagements or 0)

        # Top post caption per platform
        start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        published = self.db.scalars(
            select(SocialPostPlatform)
            .join(SocialPost, SocialPost.id == SocialPostPlatform.post_id)
            .where(
                SocialPost.workspace_id == workspace_id,
                SocialPostPlatform.status == SocialPlatformPostStatus.PUBLISHED,
                SocialPostPlatform.published_at.is_not(None),
                SocialPostPlatform.published_at >= start_dt,
                SocialPostPlatform.published_at < end_dt,
            )
        ).all()
        top_by_platform: dict[str, SocialPostPlatform] = {}
        for pp in published:
            key = pp.platform.value
            current = top_by_platform.get(key)
            if not current or _engagement(pp) > _engagement(current):
                top_by_platform[key] = pp

        result = []
        for platform in SocialPlatform:
            p = platform.value
            data = by_platform[p]
            impressions = data["impressions"]
            engagements = data["engagements"]
            top = top_by_platform.get(p)
            result.append(
                {
                    "platform": p,
                    "posts": data["posts"],
                    "reach": data["reach"],
                    "impressions": impressions,
                    "engagementRate": (
                        round(engagements / impressions, 4) if impressions else 0.0
                    ),
                    "topPost": (top.caption[:80] if top and top.caption else None),
                }
            )
        return result
