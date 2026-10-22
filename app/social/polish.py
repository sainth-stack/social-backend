"""Settings, templates, dashboard, approval, and team permissions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException, status
from pymongo.database import Database

from app.core.config import settings
from app.core.mongo_utils import new_id, utcnow
from app.social.audit import write_social_audit
from app.social.limits import (
    enforce_approval_available,
    enforce_posts_limit,
    enforce_templates_limit,
    usage_snapshot,
)
from app.social.models import (
    SocialApprovalStatus,
    SocialPermission,
    SocialPostStatus,
)
from app.social.template_utils import (
    apply_template_fields,
    load_social_template_seed_rows,
    merge_placeholder_values,
)
from app.social.permissions import _LEVEL_TO_PERMISSION, can_manage_team, require_permission
from app.workspaces.models import SocialLevel

_DASHBOARD_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="dashboard")

DEFAULT_NOTIFICATION_EVENTS = {
    "post_published": True,
    "post_failed": True,
    "token_expired": True,
    "approval_requested": True,
    "approval_resolved": True,
    "analytics_weekly": False,
}

DEFAULT_ENABLED_PLATFORMS = {
    "facebook": True,
    "instagram": True,
    "linkedin": True,
    "x": True,
}

DEFAULT_POSTING_TIMES = {
    "mon": ["09:00", "18:00"],
    "tue": ["09:00", "18:00"],
    "wed": ["09:00", "18:00"],
    "thu": ["09:00", "18:00"],
    "fri": ["09:00", "18:00"],
    "sat": ["10:00"],
    "sun": ["10:00"],
}


class SocialPolishService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ── Settings ──────────────────────────────────────────────────────────────

    def get_settings(self, workspace: dict) -> dict:
        row = self._settings_row(workspace["id"], create=False)
        return self._serialize_settings(row, workspace)

    def update_settings(
        self,
        workspace: dict,
        user: dict,
        payload: dict,
    ) -> dict:
        require_permission(self.db, workspace, user, SocialPermission.ADMIN)
        if payload.get("approvalRequired"):
            enforce_approval_available(self.db, workspace)
        row = self._settings_row(workspace["id"], create=True)

        mapping = {
            "timezone": "timezone",
            "defaultLanguage": "default_language",
            "approvalRequired": "approval_required",
            "approverUserIds": "approver_user_ids",
            "approvalSlaHours": "approval_sla_hours",
            "approvalSlaAction": "approval_sla_action",
            "defaultPostingTimes": "default_posting_times",
            "queueGapMinutes": "queue_gap_minutes",
            "blackoutDates": "blackout_dates",
            "defaultTone": "default_tone",
            "defaultCta": "default_cta",
            "hashtagCount": "hashtag_count",
            "autoFirstComment": "auto_first_comment",
            "imageGenerationStyle": "image_generation_style",
            "openaiModel": "openai_model",
            "systemPromptOverride": "system_prompt_override",
            "enabledPlatforms": "enabled_platforms",
            "notificationEvents": "notification_events",
            "notificationDelivery": "notification_delivery",
        }
        updates: dict = {}
        for api_key, attr in mapping.items():
            if api_key in payload:
                updates[attr] = payload[api_key]

        if updates:
            self.db["social_settings"].update_one(
                {"workspace_id": workspace["id"]}, {"$set": updates}
            )

        row = self._settings_row(workspace["id"], create=False)
        write_social_audit(
            self.db,
            workspace_id=workspace["id"],
            user_id=user["id"],
            action="setting.updated",
            entity_type="setting",
            entity_id=row.get("id") if row else None,
        )
        return self._serialize_settings(row, workspace)

    # ── Team permissions ──────────────────────────────────────────────────────

    def list_team_permissions(self, workspace: dict) -> list[dict]:
        memberships = list(
            self.db["workspace_members"].find({"workspace_id": workspace["id"]})
        )
        if not memberships:
            return []
        users_by_id = {
            user["id"]: user
            for user in self.db["users"].find(
                {"id": {"$in": [member["user_id"] for member in memberships]}}
            )
        }
        first_membership_by_user = {}
        for member in memberships:
            first_membership_by_user.setdefault(member["user_id"], member)
        items = []
        for member in memberships:
            user = users_by_id.get(member["user_id"])
            if not user:
                continue
            if user.get("is_platform_admin") or workspace.get("owner_user_id") == user["id"]:
                permission = SocialPermission.ADMIN
            else:
                level = first_membership_by_user[user["id"]].get(
                    "social_level", SocialLevel.VIEWER.value
                )
                permission = _LEVEL_TO_PERMISSION.get(level, SocialPermission.VIEWER)
            items.append(
                {
                    "userId": str(user["id"]),
                    "name": user.get("full_name") or user["email"],
                    "email": user["email"],
                    "permission": permission.value,
                }
            )
        return items

    def update_team_permission(
        self,
        workspace: dict,
        actor: dict,
        user_id: str,
        permission: SocialPermission,
    ) -> dict:
        if not can_manage_team(actor, workspace, self.db):
            require_permission(self.db, workspace, actor, SocialPermission.ADMIN)

        member = self.db["workspace_members"].find_one(
            {"workspace_id": workspace["id"], "user_id": str(user_id)}
        )
        if not member:
            raise HTTPException(status_code=404, detail="Team member not found")

        user = self.db["users"].find_one({"id": str(user_id)})
        self.db["workspace_members"].update_one(
            {"workspace_id": workspace["id"], "user_id": str(user_id)},
            {"$set": {"social_level": SocialLevel(permission.value).value}},
        )
        write_social_audit(
            self.db,
            workspace_id=workspace["id"],
            user_id=actor["id"],
            action="permission.updated",
            entity_type="user",
            entity_id=str(user_id),
            metadata={"permission": permission.value},
        )
        return {
            "userId": str(user["id"]) if user else str(user_id),
            "name": (user.get("full_name") or user["email"]) if user else str(user_id),
            "email": user["email"] if user else "",
            "permission": permission.value,
        }

    # ── Templates ─────────────────────────────────────────────────────────────

    def list_templates(self, workspace: dict, user: dict) -> list[dict]:
        self.ensure_system_templates(workspace, user)
        rows = list(
            self.db["social_templates"]
            .find({"workspace_id": workspace["id"]})
            .sort([("is_system", -1), ("sort_order", 1), ("created_at", -1)])
        )
        return [self._serialize_template(r) for r in rows]

    def ensure_system_templates(self, workspace: dict, user: dict) -> None:
        existing_keys = {
            row.get("system_key")
            for row in self.db["social_templates"].find(
                {"workspace_id": workspace["id"], "is_system": True}
            )
            if row.get("system_key")
        }

        created = False
        for row in load_social_template_seed_rows():
            system_key = str(row.get("id", "")).strip()
            if not system_key or system_key in existing_keys:
                continue
            now = utcnow()
            self.db["social_templates"].insert_one(
                {
                    "id": new_id(),
                    "workspace_id": workspace["id"],
                    "name": str(row.get("name", "Untitled")).strip(),
                    "category": str(row.get("category", "general")).strip(),
                    "platforms": list(row.get("platforms") or []),
                    "caption_template": str(row.get("captionTemplate", "")).strip(),
                    "hashtags": list(row.get("hashtags") or []),
                    "is_system": True,
                    "system_key": system_key,
                    "description": str(row.get("description", "")).strip(),
                    "goal": str(row.get("goal", "general")).strip(),
                    "placeholders": list(row.get("placeholders") or []),
                    "image_prompt": str(row.get("imagePrompt", "")).strip(),
                    "generate_image": bool(row.get("generateImage", False)),
                    "suggested_tone": str(row.get("suggestedTone", "Professional")).strip(),
                    "suggested_cta": str(row.get("suggestedCta", "")).strip(),
                    "first_comment_template": str(row.get("firstCommentTemplate", "")).strip(),
                    "sort_order": int(row.get("sortOrder", 0)),
                    "created_by": user["id"],
                    "created_at": now,
                }
            )
            created = True

    def apply_template(
        self,
        workspace: dict,
        user: dict,
        template_id: str,
        payload: dict,
    ) -> dict:
        require_permission(self.db, workspace, user, SocialPermission.EDITOR)
        self.ensure_system_templates(workspace, user)
        row = self._get_template(workspace["id"], template_id)

        voice = self.db["social_brand_voices"].find_one(
            {"workspace_id": workspace["id"]}
        )
        brand_name = voice.get("brand_name") if voice else workspace.get("name", "")
        industry = voice.get("industry") if voice else ""

        seed_row = None
        if row.get("system_key"):
            seed_row = next(
                (r for r in load_social_template_seed_rows() if r.get("id") == row["system_key"]),
                None,
            )
        template_source = seed_row or {
            "captionTemplate": row.get("caption_template", ""),
            "imagePrompt": row.get("image_prompt", ""),
            "firstCommentTemplate": row.get("first_comment_template", ""),
            "suggestedCta": row.get("suggested_cta", ""),
            "hashtags": row.get("hashtags", []),
            "name": row.get("name", ""),
            "placeholders": row.get("placeholders", []),
        }

        user_values = payload.get("values") or {}
        values = merge_placeholder_values(
            template_source,
            user_values,
            brand_name=brand_name,
            industry=industry,
            organization_name=workspace.get("name", ""),
        )
        resolved = apply_template_fields(template_source, values)

        return {
            "templateId": str(row["id"]),
            "name": row.get("name"),
            "category": row.get("category"),
            "goal": row.get("goal"),
            "platforms": list(row.get("platforms") or []),
            "topic": resolved["topic"],
            "captionTemplate": resolved["captionTemplate"],
            "hashtags": resolved["hashtags"],
            "firstComment": resolved["firstComment"],
            "suggestedTone": row.get("suggested_tone") or "Professional",
            "suggestedCta": resolved["suggestedCta"] or row.get("suggested_cta", ""),
            "generateImage": bool(row.get("generate_image")),
            "imagePrompt": resolved["imagePrompt"] or row.get("image_prompt", ""),
            "placeholderValues": values,
        }

    def create_template(self, workspace: dict, user: dict, payload: dict) -> dict:
        require_permission(self.db, workspace, user, SocialPermission.EDITOR)
        enforce_templates_limit(self.db, workspace)
        now = utcnow()
        doc = {
            "id": new_id(),
            "workspace_id": workspace["id"],
            "name": payload.get("name") or "Untitled",
            "category": payload.get("category") or "general",
            "platforms": list(payload.get("platforms") or []),
            "caption_template": payload.get("captionTemplate") or "",
            "hashtags": list(payload.get("hashtags") or []),
            "description": payload.get("description") or "",
            "goal": payload.get("goal") or "general",
            "placeholders": list(payload.get("placeholders") or []),
            "image_prompt": payload.get("imagePrompt") or "",
            "generate_image": bool(payload.get("generateImage", False)),
            "suggested_tone": payload.get("suggestedTone") or "Professional",
            "suggested_cta": payload.get("suggestedCta") or "",
            "first_comment_template": payload.get("firstCommentTemplate") or "",
            "is_system": False,
            "system_key": None,
            "sort_order": 0,
            "created_by": user["id"],
            "created_at": now,
        }
        self.db["social_templates"].insert_one(doc)
        write_social_audit(
            self.db,
            workspace_id=workspace["id"],
            user_id=user["id"],
            action="template.created",
            entity_type="template",
            entity_id=doc["id"],
        )
        return self._serialize_template(doc)

    def update_template(
        self, workspace: dict, user: dict, template_id: str, payload: dict
    ) -> dict:
        require_permission(self.db, workspace, user, SocialPermission.EDITOR)
        row = self._get_template(workspace["id"], template_id)
        if row.get("is_system"):
            raise HTTPException(status_code=400, detail="System templates cannot be edited")
        updates: dict = {}
        for key, attr in [
            ("name", "name"),
            ("category", "category"),
            ("platforms", "platforms"),
            ("captionTemplate", "caption_template"),
            ("hashtags", "hashtags"),
            ("description", "description"),
            ("goal", "goal"),
            ("placeholders", "placeholders"),
            ("imagePrompt", "image_prompt"),
            ("generateImage", "generate_image"),
            ("suggestedTone", "suggested_tone"),
            ("suggestedCta", "suggested_cta"),
            ("firstCommentTemplate", "first_comment_template"),
        ]:
            if key in payload and payload[key] is not None:
                updates[attr] = payload[key]
        if updates:
            self.db["social_templates"].update_one({"id": template_id}, {"$set": updates})
        row = self.db["social_templates"].find_one({"id": template_id})
        return self._serialize_template(row)

    def delete_template(self, workspace: dict, user: dict, template_id: str) -> None:
        require_permission(self.db, workspace, user, SocialPermission.EDITOR)
        row = self._get_template(workspace["id"], template_id)
        if row.get("is_system"):
            raise HTTPException(status_code=400, detail="System templates cannot be deleted")
        self.db["social_templates"].delete_one({"id": template_id})

    # ── Approval ──────────────────────────────────────────────────────────────

    def submit_approval(self, workspace: dict, user: dict, post: dict) -> dict:
        require_permission(self.db, workspace, user, SocialPermission.EDITOR)
        settings_row = self._settings_row(workspace["id"])
        if not settings_row or not settings_row.get("approval_required"):
            raise HTTPException(status_code=400, detail="Approval workflow is disabled")
        enforce_approval_available(self.db, workspace)
        if post.get("status") not in (SocialPostStatus.DRAFT.value, SocialPostStatus.FAILED.value):
            raise HTTPException(status_code=400, detail="Only drafts can be submitted")
        self.db["social_posts"].update_one(
            {"id": post["id"]},
            {
                "$set": {
                    "status": SocialPostStatus.PENDING_APPROVAL.value,
                    "approval_status": SocialApprovalStatus.PENDING.value,
                }
            },
        )
        post = self.db["social_posts"].find_one({"id": post["id"]})
        write_social_audit(
            self.db,
            workspace_id=workspace["id"],
            user_id=user["id"],
            action="post.submitted_approval",
            entity_id=post["id"],
        )
        return post

    def approve_post(self, workspace: dict, user: dict, post: dict) -> dict:
        require_permission(self.db, workspace, user, SocialPermission.ADMIN)
        if post.get("status") != SocialPostStatus.PENDING_APPROVAL.value:
            raise HTTPException(status_code=400, detail="Post is not pending approval")
        self.db["social_posts"].update_one(
            {"id": post["id"]},
            {
                "$set": {
                    "approval_status": SocialApprovalStatus.APPROVED.value,
                    "approved_by": user["id"],
                    "status": SocialPostStatus.DRAFT.value,
                }
            },
        )
        post = self.db["social_posts"].find_one({"id": post["id"]})
        write_social_audit(
            self.db,
            workspace_id=workspace["id"],
            user_id=user["id"],
            action="post.approved",
            entity_id=post["id"],
        )
        return post

    def reject_post(
        self, workspace: dict, user: dict, post: dict, reason: Optional[str] = None
    ) -> dict:
        require_permission(self.db, workspace, user, SocialPermission.ADMIN)
        if post.get("status") != SocialPostStatus.PENDING_APPROVAL.value:
            raise HTTPException(status_code=400, detail="Post is not pending approval")
        self.db["social_posts"].update_one(
            {"id": post["id"]},
            {
                "$set": {
                    "approval_status": SocialApprovalStatus.REJECTED.value,
                    "approved_by": user["id"],
                    "status": SocialPostStatus.DRAFT.value,
                }
            },
        )
        post = self.db["social_posts"].find_one({"id": post["id"]})
        write_social_audit(
            self.db,
            workspace_id=workspace["id"],
            user_id=user["id"],
            action="post.rejected",
            entity_id=post["id"],
            metadata={"reason": reason},
        )
        return post

    def request_changes(
        self, workspace: dict, user: dict, post: dict, reason: Optional[str] = None
    ) -> dict:
        require_permission(self.db, workspace, user, SocialPermission.ADMIN)
        if post.get("status") != SocialPostStatus.PENDING_APPROVAL.value:
            raise HTTPException(status_code=400, detail="Post is not pending approval")
        self.db["social_posts"].update_one(
            {"id": post["id"]},
            {
                "$set": {
                    "approval_status": SocialApprovalStatus.CHANGES_REQUESTED.value,
                    "approved_by": user["id"],
                    "status": SocialPostStatus.DRAFT.value,
                }
            },
        )
        post = self.db["social_posts"].find_one({"id": post["id"]})
        write_social_audit(
            self.db,
            workspace_id=workspace["id"],
            user_id=user["id"],
            action="post.changes_requested",
            entity_id=post["id"],
            metadata={"reason": reason},
        )
        return post

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def dashboard_home(self, workspace: dict) -> dict:
        """Single payload for the workspace home dashboard (one round-trip)."""
        from app.core.database import get_database, mongo_retry_once
        from app.social.dashboard_cache import cached_dashboard

        def _refresh_db() -> None:
            self.db = get_database()

        return cached_dashboard(
            str(workspace["id"]),
            "home",
            lambda: mongo_retry_once(self._dashboard_home_impl, workspace, refresh=_refresh_db),
        )

    def _dashboard_home_impl(self, workspace: dict) -> dict:
        import contextvars

        from app.social.schemas import SocialPostListParams, SocialPostStatus
        from app.social.service import SocialMediaService

        def _scheduled() -> list[dict]:
            page = SocialMediaService(self.db).list_posts(
                workspace,
                SocialPostListParams(status=SocialPostStatus.SCHEDULED, page=1, pageSize=4),
            )
            return [p.model_dump(mode="json", by_alias=True) for p in page.items]

        sections = {
            "stats": lambda: self._dashboard_stats_impl(workspace),
            "activity": lambda: self._activity_impl(workspace, 10),
            "recommendations": lambda: self.recommendations(workspace),
            "scheduledPosts": _scheduled,
        }
        # Sections are independent; overlapping their Mongo round-trips cuts latency to the slowest one.
        futures = {
            name: _DASHBOARD_POOL.submit(contextvars.copy_context().run, fn)
            for name, fn in sections.items()
        }
        return {name: future.result() for name, future in futures.items()}

    def dashboard_stats(self, workspace: dict) -> dict:
        from app.core.database import get_database, mongo_retry_once
        from app.social.dashboard_cache import cached_dashboard

        def _refresh_db() -> None:
            self.db = get_database()

        return cached_dashboard(
            str(workspace["id"]),
            "stats",
            lambda: mongo_retry_once(self._dashboard_stats_impl, workspace, refresh=_refresh_db),
        )

    def usage(self, workspace: dict) -> dict:
        from app.social.dashboard_cache import cached_dashboard

        return cached_dashboard(
            str(workspace["id"]), "usage", lambda: usage_snapshot(self.db, workspace)
        )

    def _dashboard_stats_impl(self, workspace: dict) -> dict:
        accounts = list(
            self.db["social_accounts"].find(
                {"workspace_id": workspace["id"], "is_active": True},
                {
                    "_id": 0,
                    "id": 1,
                    "platform": 1,
                    "account_name": 1,
                    "follower_count": 1,
                    "token_expires_at": 1,
                    "is_default": 1,
                    "is_active": 1,
                    "access_token_enc": 1,
                },
            )
        )
        now = datetime.now(timezone.utc)
        posts_week = self.db["social_posts"].count_documents(
            {"workspace_id": workspace["id"], "created_at": {"$gte": now - timedelta(days=7)}}
        )
        from app.social.analytics.aggregator import AnalyticsAggregator

        metrics = AnalyticsAggregator(self.db).summary_metrics(
            workspace["id"], (now - timedelta(days=29)).date(), now.date()
        )
        statuses = [self._token_status(a) for a in accounts]
        expired = sum(
            1
            for a in accounts
            if a.get("token_expires_at")
            and (
                a["token_expires_at"].replace(tzinfo=timezone.utc)
                if a["token_expires_at"].tzinfo is None
                else a["token_expires_at"]
            )
            <= now
        )
        return {
            "connectedAccounts": len(accounts),
            "expiredAccounts": expired,
            "postsThisWeek": posts_week,
            "totalReach": metrics["totalReach"],
            "avgEngagementRate": metrics["avgEngagementRate"],
            "usage": usage_snapshot(self.db, workspace, active_accounts=len(accounts)),
            "accounts": [
                {
                    "id": str(a["id"]),
                    "platform": a.get("platform"),
                    "accountName": a.get("account_name"),
                    "followerCount": a.get("follower_count"),
                    "tokenStatus": token_status,
                    "isDefault": a.get("is_default"),
                }
                for a, token_status in zip(accounts, statuses)
            ],
        }

    def activity(self, workspace: dict, limit: int = 10) -> list[dict]:
        from app.core.database import get_database, mongo_retry_once

        def _refresh_db() -> None:
            self.db = get_database()

        return mongo_retry_once(
            self._activity_impl,
            workspace,
            limit,
            refresh=_refresh_db,
        )

    def _activity_impl(self, workspace: dict, limit: int = 10) -> list[dict]:
        fetch_limit = min(max(limit * 4, limit), 40)
        rows = list(
            self.db["social_audit_logs"]
            .find({"workspace_id": workspace["id"]})
            .sort("created_at", -1)
            .limit(fetch_limit)
        )
        if rows:
            post_ids = [
                r["entity_id"]
                for r in rows
                if r.get("entity_type") == "post" and r.get("entity_id")
            ]
            title_by_post_id: dict[str, str] = {}
            if post_ids:
                for post in self.db["social_posts"].find(
                    {"id": {"$in": post_ids}}, {"_id": 0, "id": 1, "title": 1}
                ):
                    title_by_post_id[post["id"]] = post.get("title", "")

            items = [
                self._serialize_activity_item(
                    id=str(r["id"]),
                    action=r.get("action", ""),
                    entity_type=r.get("entity_type", "post"),
                    entity_id=r.get("entity_id"),
                    metadata=r.get("metadata_json") or {},
                    created_at=r.get("created_at"),
                    title_lookup=title_by_post_id,
                )
                for r in rows
            ]
            items = self._enrich_activity_items(items)
            items = self._collapse_activity_duplicates(items)
            return items[:limit]

        # Fallback: recent posts status changes
        posts = list(
            self.db["social_posts"]
            .find({"workspace_id": workspace["id"]})
            .sort("updated_at", -1)
            .limit(limit)
        )
        items = [
            self._serialize_activity_item(
                id=str(p["id"]),
                action=f"post.{p.get('status', 'draft')}",
                entity_type="post",
                entity_id=str(p["id"]),
                metadata={"title": p.get("title", "")},
                created_at=p.get("updated_at"),
            )
            for p in posts
        ]
        return self._enrich_activity_items(items)

    def _enrich_activity_items(self, items: list[dict]) -> list[dict]:
        post_ids = list(
            {
                str(i["entityId"])
                for i in items
                if i.get("entityType") == "post" and i.get("entityId")
            }
        )
        platforms_by_post: dict[str, list[str]] = {}
        if post_ids:
            for pp in self.db["social_post_platforms"].find(
                {"post_id": {"$in": post_ids}}, {"_id": 0, "post_id": 1, "platform": 1}
            ):
                pid = str(pp.get("post_id") or "")
                platform = (pp.get("platform") or "").strip()
                if not pid or not platform:
                    continue
                bucket = platforms_by_post.setdefault(pid, [])
                if platform not in bucket:
                    bucket.append(platform)

        for item in items:
            meta = item.get("metadata") or {}
            platforms: list[str] = []
            meta_platform = meta.get("platform")
            if isinstance(meta_platform, str) and meta_platform.strip():
                platforms.append(meta_platform.strip())
            entity_id = item.get("entityId")
            if entity_id:
                for p in platforms_by_post.get(str(entity_id), []):
                    if p not in platforms:
                        platforms.append(p)
            item["platforms"] = platforms
            item["groupCount"] = int(item.get("groupCount") or 1)
        return items

    @staticmethod
    def _collapse_activity_duplicates(items: list[dict]) -> list[dict]:
        if not items:
            return []
        collapsed: list[dict] = []
        for item in items:
            if collapsed and SocialPolishService._activities_mergeable(collapsed[-1], item):
                SocialPolishService._merge_activity_item(collapsed[-1], item)
                continue
            collapsed.append(dict(item))
        return collapsed

    @staticmethod
    def _activities_mergeable(a: dict, b: dict) -> bool:
        if a.get("entityId") != b.get("entityId"):
            return False
        if a.get("action") != b.get("action"):
            return False
        if a.get("entityType") != b.get("entityType"):
            return False
        ta = a.get("createdAt")
        tb = b.get("createdAt")
        if not ta or not tb:
            return (a.get("text") or "") == (b.get("text") or "")
        try:
            da = datetime.fromisoformat(str(ta).replace("Z", "+00:00"))
            db = datetime.fromisoformat(str(tb).replace("Z", "+00:00"))
            if da.tzinfo is None:
                da = da.replace(tzinfo=timezone.utc)
            if db.tzinfo is None:
                db = db.replace(tzinfo=timezone.utc)
            return abs((da - db).total_seconds()) <= 300
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _merge_activity_item(into: dict, other: dict) -> None:
        into["groupCount"] = int(into.get("groupCount") or 1) + 1
        merged = list(into.get("platforms") or [])
        for p in other.get("platforms") or []:
            if p not in merged:
                merged.append(p)
        into["platforms"] = merged

    @staticmethod
    def _activity_type(action: str) -> str:
        normalized = action.lower()
        if "failed" in normalized:
            return "failed"
        if "publish" in normalized:
            return "published"
        if "schedule" in normalized or normalized.endswith(".scheduled"):
            return "scheduled"
        if any(k in normalized for k in ("connect", "account", "token")):
            return "connected"
        if any(
            k in normalized
            for k in ("generat", "template", "created", "draft", "approval", "approved", "rejected")
        ):
            return "generated"
        return "generated"

    @classmethod
    def _activity_text(
        cls,
        action: str,
        entity_type: str,
        metadata: dict[str, Any],
        title_lookup: Optional[dict[str, str]] = None,
        entity_id: Optional[str] = None,
    ) -> str:
        title = (metadata.get("title") or metadata.get("name") or "").strip()
        if not title and entity_type == "post" and entity_id and title_lookup:
            title = (title_lookup.get(entity_id) or "").strip()
        quoted = f" '{title}'" if title else ""

        labels = {
            "post.created": f"Created post{quoted}",
            "post.scheduled": f"Scheduled{quoted}",
            "post.published": f"Published{quoted}",
            "post.failed": f"Failed to publish{quoted}",
            "post.draft": f"Saved draft{quoted}",
            "post.submitted_approval": f"Submitted{quoted} for approval",
            "post.approved": f"Approved{quoted}",
            "post.rejected": f"Rejected{quoted}",
            "post.changes_requested": f"Requested changes on{quoted}",
            "post.auto_approved": f"Auto-approved{quoted}",
            "post.auto_rejected": f"Auto-rejected{quoted}",
            "post.approval_reminder": f"Approval reminder sent for{quoted}",
            "template.created": f"Created template{quoted or ' ' + str(metadata.get('name', 'template')).strip()}",
            "setting.updated": "Updated workspace settings",
            "permission.updated": "Updated team permissions",
            "account.token_expired": "Social account token expired",
            "account.token_expiring": "Social account token expiring soon",
        }
        if action in labels:
            return labels[action]
        if action.startswith("post."):
            status_label = action.split(".", 1)[1].replace("_", " ")
            return f"Post {status_label}{quoted}"
        return action.replace(".", " ").replace("_", " ").capitalize()

    @classmethod
    def _serialize_activity_item(
        cls,
        *,
        id: str,
        action: str,
        entity_type: str,
        entity_id: Optional[str],
        metadata: dict[str, Any],
        created_at: Optional[datetime],
        title_lookup: Optional[dict[str, str]] = None,
    ) -> dict:
        title = (metadata.get("title") or metadata.get("name") or "").strip()
        if not title and entity_type == "post" and entity_id and title_lookup:
            title = (title_lookup.get(entity_id) or "").strip()
        return {
            "id": id,
            "action": action,
            "type": cls._activity_type(action),
            "text": cls._activity_text(
                action,
                entity_type,
                metadata,
                title_lookup=title_lookup,
                entity_id=entity_id,
            ),
            "title": title or None,
            "entityType": entity_type,
            "entityId": entity_id,
            "metadata": metadata,
            "platforms": [],
            "groupCount": 1,
            "createdAt": created_at.isoformat() if created_at else None,
        }

    def recommendations(self, workspace: dict) -> list[dict]:
        voice = self.db["social_brand_voices"].find_one(
            {"workspace_id": workspace["id"]}, {"_id": 0, "brand_name": 1, "industry": 1}
        )
        brand = voice.get("brand_name") if voice else workspace.get("name", "your brand")
        industry = voice.get("industry") if voice else "your industry"
        day = datetime.now(timezone.utc).strftime("%A")
        return [
            {
                "topic": f"Share a {day} insight about {industry}",
                "reason": f"Matches {brand} brand voice and today's weekday hook",
            },
            {
                "topic": f"Customer win story for {brand}",
                "reason": "Social proof posts typically drive higher engagement",
            },
            {
                "topic": f"Behind-the-scenes look at how {brand} works",
                "reason": "Humanizes the brand and builds trust",
            },
        ]

    # ── Internals ─────────────────────────────────────────────────────────────

    def _settings_row(self, workspace_id: str, create: bool = True) -> dict | None:
        row = self.db["social_settings"].find_one({"workspace_id": str(workspace_id)})
        if row:
            return row
        if not create:
            # Return ephemeral defaults dict
            return {
                "id": new_id(),
                "workspace_id": str(workspace_id),
                "timezone": "Asia/Kolkata",
                "default_language": "en",
                "approval_required": False,
                "approver_user_ids": [],
                "approval_sla_hours": 24,
                "approval_sla_action": "none",
                "default_posting_times": DEFAULT_POSTING_TIMES,
                "queue_gap_minutes": 30,
                "blackout_dates": [],
                "default_tone": "Professional",
                "default_cta": "",
                "hashtag_count": 5,
                "auto_first_comment": False,
                "image_generation_style": "Photographic",
                "openai_model": settings.bedrock_model_id or "amazon.nova-micro-v1:0",
                "system_prompt_override": None,
                "enabled_platforms": DEFAULT_ENABLED_PLATFORMS,
                "notification_events": DEFAULT_NOTIFICATION_EVENTS,
                "notification_delivery": "in_app",
            }
        now = utcnow()
        doc = {
            "id": new_id(),
            "workspace_id": str(workspace_id),
            "timezone": "Asia/Kolkata",
            "default_language": "en",
            "approval_required": False,
            "approver_user_ids": [],
            "approval_sla_hours": 24,
            "approval_sla_action": "none",
            "default_posting_times": dict(DEFAULT_POSTING_TIMES),
            "queue_gap_minutes": 30,
            "blackout_dates": [],
            "default_tone": "Professional",
            "default_cta": "",
            "hashtag_count": 5,
            "auto_first_comment": False,
            "image_generation_style": "Photographic",
            "openai_model": settings.bedrock_model_id or "amazon.nova-micro-v1:0",
            "system_prompt_override": None,
            "enabled_platforms": dict(DEFAULT_ENABLED_PLATFORMS),
            "notification_events": dict(DEFAULT_NOTIFICATION_EVENTS),
            "notification_delivery": "in_app",
            "updated_at": now,
        }
        self.db["social_settings"].insert_one(doc)
        return doc

    def _serialize_settings(self, row: dict | None, workspace: dict) -> dict:
        r = row or {}
        return {
            "id": str(r.get("id")) if r.get("id") else None,
            "workspaceId": str(workspace["id"]),
            "timezone": r.get("timezone") or "Asia/Kolkata",
            "defaultLanguage": r.get("default_language") or "en",
            "approvalRequired": bool(r.get("approval_required")),
            "approverUserIds": list(r.get("approver_user_ids") or []),
            "approvalSlaHours": r.get("approval_sla_hours") or 24,
            "approvalSlaAction": r.get("approval_sla_action") or "none",
            "defaultPostingTimes": r.get("default_posting_times") or DEFAULT_POSTING_TIMES,
            "queueGapMinutes": r.get("queue_gap_minutes") or 30,
            "blackoutDates": list(r.get("blackout_dates") or []),
            "defaultTone": r.get("default_tone") or "Professional",
            "defaultCta": r.get("default_cta") or "",
            "hashtagCount": r.get("hashtag_count") if r.get("hashtag_count") is not None else 5,
            "autoFirstComment": bool(r.get("auto_first_comment")),
            "imageGenerationStyle": r.get("image_generation_style") or "Photographic",
            "openaiModel": r.get("openai_model") or settings.bedrock_model_id or "amazon.nova-micro-v1:0",
            "systemPromptOverride": r.get("system_prompt_override"),
            "enabledPlatforms": r.get("enabled_platforms") or DEFAULT_ENABLED_PLATFORMS,
            "notificationEvents": r.get("notification_events") or DEFAULT_NOTIFICATION_EVENTS,
            "notificationDelivery": r.get("notification_delivery") or "in_app",
            "usage": usage_snapshot(self.db, workspace),
        }

    def _get_template(self, workspace_id: str, template_id: str) -> dict:
        row = self.db["social_templates"].find_one({"id": str(template_id)})
        if not row or row.get("workspace_id") != workspace_id:
            raise HTTPException(status_code=404, detail="Template not found")
        return row

    def _serialize_template(self, row: dict) -> dict:
        return {
            "id": str(row["id"]),
            "workspaceId": str(row.get("workspace_id")),
            "name": row.get("name"),
            "category": row.get("category"),
            "platforms": list(row.get("platforms") or []),
            "captionTemplate": row.get("caption_template"),
            "hashtags": list(row.get("hashtags") or []),
            "isSystem": bool(row.get("is_system")),
            "systemKey": row.get("system_key"),
            "description": row.get("description") or "",
            "goal": row.get("goal") or "general",
            "placeholders": list(row.get("placeholders") or []),
            "imagePrompt": row.get("image_prompt") or "",
            "generateImage": bool(row.get("generate_image")),
            "suggestedTone": row.get("suggested_tone") or "Professional",
            "suggestedCta": row.get("suggested_cta") or "",
            "firstCommentTemplate": row.get("first_comment_template") or "",
            "sortOrder": row.get("sort_order") or 0,
            "createdBy": str(row["created_by"]) if row.get("created_by") else None,
            "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
        }

    def _token_status(self, account: dict) -> str:
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
        if expires <= now + timedelta(days=7):
            return "expires_soon"
        return "active"
