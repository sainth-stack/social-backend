"""Cross-platform analytics aggregates for API responses."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from pymongo.database import Database

from app.social.models import SocialPlatform, SocialPlatformPostStatus


def _parse_range(from_date: Optional[str], to_date: Optional[str]) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    end = date.fromisoformat(to_date[:10]) if to_date else today
    start = date.fromisoformat(from_date[:10]) if from_date else end - timedelta(days=29)
    if start > end:
        start, end = end, start
    return start, end


def _engagement(pp: dict) -> int:
    return int(pp.get("likes") or 0) + int(pp.get("comments") or 0) + int(pp.get("shares") or 0)


class AnalyticsAggregator:
    def __init__(self, db: Database) -> None:
        self.db = db

    def overview(self, workspace_id: str, from_date: Optional[str], to_date: Optional[str]) -> dict:
        start, end = _parse_range(from_date, to_date)
        rows = self._daily_rows(workspace_id, start, end)
        if not rows:
            rows = self._synthetic_daily_from_posts(workspace_id, start, end)

        total_posts = sum(int(r.get("posts_count") or 0) for r in rows)
        total_reach = sum(int(r.get("total_reach") or 0) for r in rows)
        total_impressions = sum(int(r.get("total_impressions") or 0) for r in rows)
        total_engagements = sum(int(r.get("total_engagements") or 0) for r in rows)
        total_clicks = sum(int(r.get("total_clicks") or 0) for r in rows)
        follower_growth = sum(int(r.get("new_followers") or 0) for r in rows)
        avg_engagement_rate = (
            round(total_engagements / total_impressions, 4) if total_impressions else 0.0
        )

        by_day_platform: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in rows:
            key = r["date"].isoformat() if isinstance(r["date"], date) else str(r["date"])[:10]
            by_day_platform[key][r.get("platform", "")] += int(r.get("total_engagements") or 0)

        engagement_series = []
        day = start
        while day <= end:
            key = day.isoformat()
            point: dict = {"date": key}
            for platform in SocialPlatform:
                point[platform.value] = by_day_platform[key].get(platform.value, 0)
            engagement_series.append(point)
            day += timedelta(days=1)

        reach_by_platform: dict[str, int] = defaultdict(int)
        for r in rows:
            reach_by_platform[r.get("platform", "")] += int(r.get("total_reach") or 0)
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
        workspace_id: str,
        platform: SocialPlatform,
        from_date: Optional[str],
        to_date: Optional[str],
    ) -> dict:
        start, end = _parse_range(from_date, to_date)
        all_rows = self._daily_rows(workspace_id, start, end)
        if not all_rows:
            all_rows = self._synthetic_daily_from_posts(workspace_id, start, end)
        rows = [r for r in all_rows if r.get("platform") == platform.value]

        series = []
        day = start
        by_day = {
            (r["date"] if isinstance(r["date"], date) else r["date"]): r for r in rows
        }
        while day <= end:
            r = by_day.get(day)
            series.append(
                {
                    "date": day.isoformat(),
                    "impressions": int(r.get("total_impressions") or 0) if r else 0,
                    "reach": int(r.get("total_reach") or 0) if r else 0,
                    "engagement": int(r.get("total_engagements") or 0) if r else 0,
                    "clicks": int(r.get("total_clicks") or 0) if r else 0,
                    "followers": int(r.get("follower_count") or 0) if r else 0,
                    "newFollowers": int(r.get("new_followers") or 0) if r else 0,
                }
            )
            day += timedelta(days=1)

        totals = {
            "posts": sum(int(r.get("posts_count") or 0) for r in rows),
            "reach": sum(int(r.get("total_reach") or 0) for r in rows),
            "impressions": sum(int(r.get("total_impressions") or 0) for r in rows),
            "engagements": sum(int(r.get("total_engagements") or 0) for r in rows),
            "clicks": sum(int(r.get("total_clicks") or 0) for r in rows),
            "followerGrowth": sum(int(r.get("new_followers") or 0) for r in rows),
            "latestFollowers": int(rows[-1].get("follower_count") or 0) if rows else 0,
        }

        # Post type breakdown
        start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        posts_pp = list(
            self.db["social_post_platforms"].find(
                {
                    "platform": platform.value,
                    "status": SocialPlatformPostStatus.PUBLISHED.value,
                    "published_at": {"$gte": start_dt, "$lt": end_dt},
                }
            )
        )
        image_posts = 0
        for pp in posts_pp:
            post = self.db["social_posts"].find_one({"id": pp.get("post_id")})
            if post and post.get("image_url"):
                image_posts += 1
        text_posts = len(posts_pp) - image_posts
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
        workspace_id: str,
        from_date: Optional[str],
        to_date: Optional[str],
        sort: str = "engagementRate",
        order: str = "desc",
    ) -> dict:
        start, end = _parse_range(from_date, to_date)
        start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)

        # Get all published post_platforms for this workspace in date range
        # We need to join with social_posts to filter by workspace_id
        post_ids = [
            p["id"]
            for p in self.db["social_posts"].find({"workspace_id": str(workspace_id)})
        ]
        rows = list(
            self.db["social_post_platforms"].find(
                {
                    "post_id": {"$in": post_ids},
                    "status": SocialPlatformPostStatus.PUBLISHED.value,
                    "published_at": {"$gte": start_dt, "$lt": end_dt},
                }
            )
        )

        items = []
        for pp in rows:
            post = self.db["social_posts"].find_one({"id": pp.get("post_id")})
            eng = _engagement(pp)
            impressions = int(pp.get("impressions") or 0)
            rate = pp.get("engagement_rate") or (
                round(eng / impressions, 4) if impressions else 0.0
            )
            items.append(
                {
                    "postId": pp.get("post_id"),
                    "platformRowId": pp.get("id"),
                    "caption": (pp.get("caption") or "")[:120],
                    "platform": pp.get("platform"),
                    "publishedAt": pp["published_at"].isoformat() if pp.get("published_at") else None,
                    "reach": pp.get("reach"),
                    "impressions": impressions,
                    "likes": pp.get("likes"),
                    "comments": pp.get("comments"),
                    "shares": pp.get("shares"),
                    "clicks": pp.get("clicks"),
                    "engagementRate": rate,
                    "engagements": eng,
                    "imageUrl": post.get("image_url") if post else None,
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
        workspace_id: str,
        from_date: Optional[str],
        to_date: Optional[str],
    ) -> dict:
        start, end = _parse_range(from_date, to_date)
        rows = self._daily_rows(workspace_id, start, end)
        if not rows:
            rows = self._synthetic_daily_from_posts(workspace_id, start, end)

        by_day_platform: dict[str, dict[str, dict]] = defaultdict(dict)
        for r in rows:
            day_str = r["date"].isoformat() if isinstance(r["date"], date) else str(r["date"])[:10]
            by_day_platform[day_str][r.get("platform", "")] = {
                "followers": int(r.get("follower_count") or 0),
                "newFollowers": int(r.get("new_followers") or 0),
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

        cards = []
        for platform in SocialPlatform:
            platform_rows = [r for r in rows if r.get("platform") == platform.value]
            latest = int(platform_rows[-1].get("follower_count") or 0) if platform_rows else 0
            growth = sum(int(r.get("new_followers") or 0) for r in platform_rows)
            cards.append({"platform": platform.value, "followers": latest, "growth": growth})

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

    def _daily_rows(self, workspace_id: str, start: date, end: date) -> list[dict]:
        return list(
            self.db["social_analytics_daily"].find(
                {
                    "workspace_id": str(workspace_id),
                    "date": {"$gte": start, "$lte": end},
                }
            ).sort("date", 1)
        )

    def _synthetic_daily_from_posts(
        self, workspace_id: str, start: date, end: date
    ) -> list[dict]:
        """Build in-memory daily rows from published posts when sync has not run yet."""
        start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)

        post_ids = [
            p["id"]
            for p in self.db["social_posts"].find({"workspace_id": str(workspace_id)})
        ]
        published = list(
            self.db["social_post_platforms"].find(
                {
                    "post_id": {"$in": post_ids},
                    "status": SocialPlatformPostStatus.PUBLISHED.value,
                    "published_at": {"$gte": start_dt, "$lt": end_dt},
                }
            )
        )

        buckets: dict[tuple, dict] = {}
        for pp in published:
            pub_at = pp.get("published_at")
            if not pub_at:
                continue
            if pub_at.tzinfo is None:
                pub_at = pub_at.replace(tzinfo=timezone.utc)
            day = pub_at.astimezone(timezone.utc).date()
            key = (day, pp.get("platform", ""))
            row = buckets.get(key)
            if not row:
                account = self.db["social_accounts"].find_one(
                    {"id": pp.get("social_account_id")}
                )
                row = {
                    "id": new_id(),
                    "workspace_id": str(workspace_id),
                    "social_account_id": pp.get("social_account_id") or "",
                    "platform": pp.get("platform", ""),
                    "date": day,
                    "follower_count": int(account.get("follower_count") or 0) if account else 0,
                    "new_followers": 0,
                    "posts_count": 0,
                    "total_reach": 0,
                    "total_impressions": 0,
                    "total_engagements": 0,
                    "total_clicks": 0,
                }
                buckets[key] = row
            row["posts_count"] = int(row.get("posts_count") or 0) + 1
            row["total_reach"] = int(row.get("total_reach") or 0) + int(pp.get("reach") or 0)
            row["total_impressions"] = int(row.get("total_impressions") or 0) + int(pp.get("impressions") or 0)
            row["total_engagements"] = int(row.get("total_engagements") or 0) + _engagement(pp)
            row["total_clicks"] = int(row.get("total_clicks") or 0) + int(pp.get("clicks") or 0)
        return sorted(buckets.values(), key=lambda r: r["date"])

    def _platform_comparison(
        self, workspace_id: str, start: date, end: date, rows: list[dict]
    ) -> list[dict]:
        by_platform: dict[str, dict] = defaultdict(
            lambda: {"posts": 0, "reach": 0, "impressions": 0, "engagements": 0}
        )
        for r in rows:
            p = r.get("platform", "")
            by_platform[p]["posts"] += int(r.get("posts_count") or 0)
            by_platform[p]["reach"] += int(r.get("total_reach") or 0)
            by_platform[p]["impressions"] += int(r.get("total_impressions") or 0)
            by_platform[p]["engagements"] += int(r.get("total_engagements") or 0)

        start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        post_ids = [
            p["id"] for p in self.db["social_posts"].find({"workspace_id": str(workspace_id)})
        ]
        published = list(
            self.db["social_post_platforms"].find(
                {
                    "post_id": {"$in": post_ids},
                    "status": SocialPlatformPostStatus.PUBLISHED.value,
                    "published_at": {"$gte": start_dt, "$lt": end_dt},
                }
            )
        )
        top_by_platform: dict[str, dict] = {}
        for pp in published:
            key = pp.get("platform", "")
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
                    "topPost": (top.get("caption", "")[:80] if top and top.get("caption") else None),
                }
            )
        return result


from app.core.mongo_utils import new_id
