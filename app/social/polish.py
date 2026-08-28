"""Settings, templates, dashboard, approval, and team permissions."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.workspaces.models import SocialLevel, Workspace, WorkspaceMember
from app.users.models import User
from app.social.audit import write_social_audit
from app.social.limits import (
    enforce_approval_available,
    enforce_posts_limit,
    enforce_templates_limit,
    usage_snapshot,
)
from app.social.models import (
    SocialAccount,
    SocialApprovalStatus,
    SocialAuditLog,
    SocialPermission,
    SocialPost,
    SocialPostStatus,
    SocialSettings,
    SocialTemplate,
)
from app.social.template_utils import (
    apply_template_fields,
    load_social_template_seed_rows,
    merge_placeholder_values,
)
from app.social.permissions import can_manage_team, get_user_permission, require_permission

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
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── Settings ──────────────────────────────────────────────────────────────

    def get_settings(self, workspace: Workspace) -> dict:
        row = self._settings_row(workspace.id, create=False)
        return self._serialize_settings(row, workspace)

    def update_settings(
        self,
        workspace: Workspace,
        user: User,
        payload: dict,
    ) -> dict:
        require_permission(self.db, workspace, user, SocialPermission.ADMIN)
        if payload.get("approvalRequired"):
            enforce_approval_available(self.db, workspace)
        row = self._settings_row(workspace.id, create=True)
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
        for api_key, attr in mapping.items():
            if api_key in payload:
                setattr(row, attr, payload[api_key])
        self.db.commit()
        write_social_audit(
            self.db,
            workspace_id=workspace.id,
            user_id=user.id,
            action="setting.updated",
            entity_type="setting",
            entity_id=row.id,
            commit=True,
        )
        return self._serialize_settings(row, workspace)

    # ── Team permissions ──────────────────────────────────────────────────────
    #
    # WorkspaceMember.social_level is the single source of truth for social
    # permissions — there is no separate permissions table. The workspace
    # owner always shows as "admin" even without an explicit membership row.

    def list_team_permissions(self, workspace: Workspace) -> list[dict]:
        rows = self.db.execute(
            select(WorkspaceMember, User)
            .join(User, User.id == WorkspaceMember.user_id)
            .where(WorkspaceMember.workspace_id == workspace.id)
        ).all()
        items = []
        for member, user in rows:
            permission = get_user_permission(self.db, workspace, user).value
            items.append(
                {
                    "userId": str(user.id),
                    "name": user.full_name or user.email,
                    "email": user.email,
                    "permission": permission,
                }
            )
        return items

    def update_team_permission(
        self,
        workspace: Workspace,
        actor: User,
        user_id: uuid.UUID,
        permission: SocialPermission,
    ) -> dict:
        if not can_manage_team(actor, workspace, self.db):
            require_permission(self.db, workspace, actor, SocialPermission.ADMIN)

        member = self.db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == user_id,
            )
        ).scalar_one_or_none()
        if not member:
            raise HTTPException(status_code=404, detail="Team member not found")

        user = self.db.get(User, user_id)
        member.social_level = SocialLevel(permission.value)
        self.db.commit()
        write_social_audit(
            self.db,
            workspace_id=workspace.id,
            user_id=actor.id,
            action="permission.updated",
            entity_type="user",
            entity_id=user_id,
            metadata={"permission": permission.value},
            commit=True,
        )
        return {
            "userId": str(user.id),
            "name": user.full_name or user.email,
            "email": user.email,
            "permission": permission.value,
        }

    # ── Templates ─────────────────────────────────────────────────────────────

    def list_templates(self, workspace: Workspace, user: User) -> list[dict]:
        self.ensure_system_templates(workspace, user)
        rows = self.db.scalars(
            select(SocialTemplate)
            .where(SocialTemplate.workspace_id == workspace.id)
            .order_by(SocialTemplate.is_system.desc(), SocialTemplate.sort_order.asc(), SocialTemplate.created_at.desc())
        ).all()
        return [self._serialize_template(r) for r in rows]

    def ensure_system_templates(self, workspace: Workspace, user: User) -> None:
        """Idempotently provision system templates for an workspace."""
        existing_keys = {
            row.system_key
            for row in self.db.scalars(
                select(SocialTemplate).where(
                    SocialTemplate.workspace_id == workspace.id,
                    SocialTemplate.is_system.is_(True),
                )
            ).all()
            if row.system_key
        }

        created = False
        for row in load_social_template_seed_rows():
            system_key = str(row.get("id", "")).strip()
            if not system_key or system_key in existing_keys:
                continue
            self.db.add(
                SocialTemplate(
                    id=uuid.uuid4(),
                    workspace_id=workspace.id,
                    name=str(row.get("name", "Untitled")).strip(),
                    category=str(row.get("category", "general")).strip(),
                    platforms=list(row.get("platforms") or []),
                    caption_template=str(row.get("captionTemplate", "")).strip(),
                    hashtags=list(row.get("hashtags") or []),
                    is_system=True,
                    system_key=system_key,
                    description=str(row.get("description", "")).strip(),
                    goal=str(row.get("goal", "general")).strip(),
                    placeholders=list(row.get("placeholders") or []),
                    image_prompt=str(row.get("imagePrompt", "")).strip(),
                    generate_image=bool(row.get("generateImage", False)),
                    suggested_tone=str(row.get("suggestedTone", "Professional")).strip(),
                    suggested_cta=str(row.get("suggestedCta", "")).strip(),
                    first_comment_template=str(row.get("firstCommentTemplate", "")).strip(),
                    sort_order=int(row.get("sortOrder", 0)),
                    created_by=user.id,
                )
            )
            created = True

        if created:
            self.db.commit()

    def apply_template(
        self,
        workspace: Workspace,
        user: User,
        template_id: uuid.UUID,
        payload: dict,
    ) -> dict:
        require_permission(self.db, workspace, user, SocialPermission.EDITOR)
        self.ensure_system_templates(workspace, user)
        row = self._get_template(workspace.id, template_id)

        from app.social.models import SocialBrandVoice

        voice = self.db.scalars(
            select(SocialBrandVoice).where(SocialBrandVoice.workspace_id == workspace.id)
        ).first()
        brand_name = voice.brand_name if voice else workspace.name
        industry = voice.industry if voice else ""

        seed_row = None
        if row.system_key:
            seed_row = next(
                (r for r in load_social_template_seed_rows() if r.get("id") == row.system_key),
                None,
            )
        template_source = seed_row or {
            "captionTemplate": row.caption_template,
            "imagePrompt": row.image_prompt,
            "firstCommentTemplate": row.first_comment_template,
            "suggestedCta": row.suggested_cta,
            "hashtags": row.hashtags,
            "name": row.name,
            "placeholders": row.placeholders,
        }

        user_values = payload.get("values") or {}
        values = merge_placeholder_values(
            template_source,
            user_values,
            brand_name=brand_name,
            industry=industry,
            organization_name=workspace.name,
        )
        resolved = apply_template_fields(template_source, values)

        return {
            "templateId": str(row.id),
            "name": row.name,
            "category": row.category,
            "goal": row.goal,
            "platforms": list(row.platforms or []),
            "topic": resolved["topic"],
            "captionTemplate": resolved["captionTemplate"],
            "hashtags": resolved["hashtags"],
            "firstComment": resolved["firstComment"],
            "suggestedTone": row.suggested_tone or "Professional",
            "suggestedCta": resolved["suggestedCta"] or row.suggested_cta,
            "generateImage": bool(row.generate_image),
            "imagePrompt": resolved["imagePrompt"] or row.image_prompt,
            "placeholderValues": values,
        }

    def create_template(
        self,
        workspace: Workspace,
        user: User,
        payload: dict,
    ) -> dict:
        require_permission(self.db, workspace, user, SocialPermission.EDITOR)
        enforce_templates_limit(self.db, workspace)
        row = SocialTemplate(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            name=payload.get("name") or "Untitled",
            category=payload.get("category") or "general",
            platforms=list(payload.get("platforms") or []),
            caption_template=payload.get("captionTemplate") or "",
            hashtags=list(payload.get("hashtags") or []),
            description=payload.get("description") or "",
            goal=payload.get("goal") or "general",
            placeholders=list(payload.get("placeholders") or []),
            image_prompt=payload.get("imagePrompt") or "",
            generate_image=bool(payload.get("generateImage", False)),
            suggested_tone=payload.get("suggestedTone") or "Professional",
            suggested_cta=payload.get("suggestedCta") or "",
            first_comment_template=payload.get("firstCommentTemplate") or "",
            is_system=False,
            created_by=user.id,
        )
        self.db.add(row)
        self.db.commit()
        write_social_audit(
            self.db,
            workspace_id=workspace.id,
            user_id=user.id,
            action="template.created",
            entity_type="template",
            entity_id=row.id,
            commit=True,
        )
        return self._serialize_template(row)

    def update_template(
        self,
        workspace: Workspace,
        user: User,
        template_id: uuid.UUID,
        payload: dict,
    ) -> dict:
        require_permission(self.db, workspace, user, SocialPermission.EDITOR)
        row = self._get_template(workspace.id, template_id)
        if row.is_system:
            raise HTTPException(status_code=400, detail="System templates cannot be edited")
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
                setattr(row, attr, payload[key])
        self.db.commit()
        return self._serialize_template(row)

    def delete_template(
        self,
        workspace: Workspace,
        user: User,
        template_id: uuid.UUID,
    ) -> None:
        require_permission(self.db, workspace, user, SocialPermission.EDITOR)
        row = self._get_template(workspace.id, template_id)
        if row.is_system:
            raise HTTPException(status_code=400, detail="System templates cannot be deleted")
        self.db.delete(row)
        self.db.commit()

    # ── Approval ──────────────────────────────────────────────────────────────

    def submit_approval(self, workspace: Workspace, user: User, post: SocialPost) -> SocialPost:
        require_permission(self.db, workspace, user, SocialPermission.EDITOR)
        settings = self._settings_row(workspace.id)
        if not settings.approval_required:
            raise HTTPException(status_code=400, detail="Approval workflow is disabled")
        enforce_approval_available(self.db, workspace)
        if post.status not in (SocialPostStatus.DRAFT, SocialPostStatus.FAILED):
            raise HTTPException(status_code=400, detail="Only drafts can be submitted")
        post.status = SocialPostStatus.PENDING_APPROVAL
        post.approval_status = SocialApprovalStatus.PENDING
        self.db.commit()
        write_social_audit(
            self.db,
            workspace_id=workspace.id,
            user_id=user.id,
            action="post.submitted_approval",
            entity_id=post.id,
            commit=True,
        )
        self.db.refresh(post)
        return post

    def approve_post(self, workspace: Workspace, user: User, post: SocialPost) -> SocialPost:
        require_permission(self.db, workspace, user, SocialPermission.ADMIN)
        if post.status != SocialPostStatus.PENDING_APPROVAL:
            raise HTTPException(status_code=400, detail="Post is not pending approval")
        post.approval_status = SocialApprovalStatus.APPROVED
        post.approved_by = user.id
        post.status = SocialPostStatus.DRAFT
        self.db.commit()
        write_social_audit(
            self.db,
            workspace_id=workspace.id,
            user_id=user.id,
            action="post.approved",
            entity_id=post.id,
            commit=True,
        )
        self.db.refresh(post)
        return post

    def reject_post(
        self,
        workspace: Workspace,
        user: User,
        post: SocialPost,
        reason: Optional[str] = None,
    ) -> SocialPost:
        require_permission(self.db, workspace, user, SocialPermission.ADMIN)
        if post.status != SocialPostStatus.PENDING_APPROVAL:
            raise HTTPException(status_code=400, detail="Post is not pending approval")
        post.approval_status = SocialApprovalStatus.REJECTED
        post.approved_by = user.id
        post.status = SocialPostStatus.DRAFT
        self.db.commit()
        write_social_audit(
            self.db,
            workspace_id=workspace.id,
            user_id=user.id,
            action="post.rejected",
            entity_id=post.id,
            metadata={"reason": reason},
            commit=True,
        )
        self.db.refresh(post)
        return post

    def request_changes(
        self,
        workspace: Workspace,
        user: User,
        post: SocialPost,
        reason: Optional[str] = None,
    ) -> SocialPost:
        require_permission(self.db, workspace, user, SocialPermission.ADMIN)
        if post.status != SocialPostStatus.PENDING_APPROVAL:
            raise HTTPException(status_code=400, detail="Post is not pending approval")
        post.approval_status = SocialApprovalStatus.CHANGES_REQUESTED
        post.approved_by = user.id
        post.status = SocialPostStatus.DRAFT
        self.db.commit()
        write_social_audit(
            self.db,
            workspace_id=workspace.id,
            user_id=user.id,
            action="post.changes_requested",
            entity_id=post.id,
            metadata={"reason": reason},
            commit=True,
        )
        self.db.refresh(post)
        return post

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def dashboard_stats(self, workspace: Workspace) -> dict:
        accounts = self.db.scalars(
            select(SocialAccount).where(
                SocialAccount.workspace_id == workspace.id,
                SocialAccount.is_active.is_(True),
            )
        ).all()
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        posts_week = self.db.scalar(
            select(func.count()).select_from(SocialPost).where(
                SocialPost.workspace_id == workspace.id,
                SocialPost.created_at >= week_ago,
            )
        ) or 0
        from app.social.analytics.aggregator import AnalyticsAggregator

        overview = AnalyticsAggregator(self.db).overview(
            workspace.id,
            (datetime.now(timezone.utc) - timedelta(days=29)).date().isoformat(),
            datetime.now(timezone.utc).date().isoformat(),
        )
        metrics = overview["metrics"]
        expired = sum(
            1
            for a in accounts
            if a.token_expires_at
            and a.token_expires_at.replace(tzinfo=timezone.utc)
            <= datetime.now(timezone.utc)
        )
        return {
            "connectedAccounts": len(accounts),
            "expiredAccounts": expired,
            "postsThisWeek": posts_week,
            "totalReach": metrics.get("totalReach", 0),
            "avgEngagementRate": metrics.get("avgEngagementRate", 0),
            "usage": usage_snapshot(self.db, workspace),
            "accounts": [
                {
                    "id": str(a.id),
                    "platform": a.platform.value,
                    "accountName": a.account_name,
                    "followerCount": a.follower_count,
                    "tokenStatus": self._token_status(a),
                    "isDefault": a.is_default,
                }
                for a in accounts
            ],
        }

    def activity(self, workspace: Workspace, limit: int = 10) -> list[dict]:
        rows = self.db.scalars(
            select(SocialAuditLog)
            .where(SocialAuditLog.workspace_id == workspace.id)
            .order_by(SocialAuditLog.created_at.desc())
            .limit(limit)
        ).all()
        if rows:
            post_ids = [
                r.entity_id
                for r in rows
                if r.entity_type == "post" and r.entity_id is not None
            ]
            title_by_post_id: dict[uuid.UUID, str] = {}
            if post_ids:
                for post_id, title in self.db.execute(
                    select(SocialPost.id, SocialPost.title).where(
                        SocialPost.id.in_(post_ids)
                    )
                ).all():
                    title_by_post_id[post_id] = title

            return [
                self._serialize_activity_item(
                    id=str(r.id),
                    action=r.action,
                    entity_type=r.entity_type,
                    entity_id=str(r.entity_id) if r.entity_id else None,
                    metadata=r.metadata_json or {},
                    created_at=r.created_at,
                    title_lookup=title_by_post_id,
                )
                for r in rows
            ]
        # Fallback: recent posts status changes
        posts = self.db.scalars(
            select(SocialPost)
            .options(selectinload(SocialPost.platforms))
            .where(SocialPost.workspace_id == workspace.id)
            .order_by(SocialPost.updated_at.desc())
            .limit(limit)
        ).all()
        return [
            self._serialize_activity_item(
                id=str(p.id),
                action=f"post.{p.status.value}",
                entity_type="post",
                entity_id=str(p.id),
                metadata={"title": p.title},
                created_at=p.updated_at,
            )
            for p in posts
        ]

    @staticmethod
    def _activity_type(action: str) -> str:
        normalized = action.lower()
        if any(k in normalized for k in ("publish", "failed")):
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
        title_lookup: Optional[dict[uuid.UUID, str]] = None,
        entity_id: Optional[str] = None,
    ) -> str:
        title = (metadata.get("title") or metadata.get("name") or "").strip()
        if not title and entity_type == "post" and entity_id and title_lookup:
            try:
                title = (title_lookup.get(uuid.UUID(entity_id)) or "").strip()
            except ValueError:
                title = ""
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
        title_lookup: Optional[dict[uuid.UUID, str]] = None,
    ) -> dict:
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
            "entityType": entity_type,
            "entityId": entity_id,
            "metadata": metadata,
            "createdAt": created_at.isoformat() if created_at else None,
        }

    def recommendations(self, workspace: Workspace) -> list[dict]:
        from app.social.models import SocialBrandVoice

        voice = self.db.scalars(
            select(SocialBrandVoice).where(SocialBrandVoice.workspace_id == workspace.id)
        ).first()
        brand = voice.brand_name if voice else workspace.name
        industry = voice.industry if voice else "your industry"
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

    def _settings_row(self, workspace_id: uuid.UUID, create: bool = True) -> SocialSettings:
        row = self.db.scalars(
            select(SocialSettings).where(SocialSettings.workspace_id == workspace_id)
        ).first()
        if row or not create:
            if not row:
                # ephemeral defaults
                return SocialSettings(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    default_posting_times=DEFAULT_POSTING_TIMES,
                    enabled_platforms=DEFAULT_ENABLED_PLATFORMS,
                    notification_events=DEFAULT_NOTIFICATION_EVENTS,
                )
            return row
        row = SocialSettings(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            default_posting_times=dict(DEFAULT_POSTING_TIMES),
            enabled_platforms=dict(DEFAULT_ENABLED_PLATFORMS),
            notification_events=dict(DEFAULT_NOTIFICATION_EVENTS),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _serialize_settings(self, row: SocialSettings, workspace: Workspace) -> dict:
        return {
            "id": str(row.id) if row.id else None,
            "workspaceId": str(workspace.id),
            "timezone": row.timezone or "Asia/Kolkata",
            "defaultLanguage": row.default_language or "en",
            "approvalRequired": bool(row.approval_required),
            "approverUserIds": list(row.approver_user_ids or []),
            "approvalSlaHours": row.approval_sla_hours or 24,
            "approvalSlaAction": row.approval_sla_action or "none",
            "defaultPostingTimes": row.default_posting_times or DEFAULT_POSTING_TIMES,
            "queueGapMinutes": row.queue_gap_minutes or 30,
            "blackoutDates": list(row.blackout_dates or []),
            "defaultTone": row.default_tone or "Professional",
            "defaultCta": row.default_cta or "",
            "hashtagCount": row.hashtag_count or 5,
            "autoFirstComment": bool(row.auto_first_comment),
            "imageGenerationStyle": row.image_generation_style or "Photographic",
            "openaiModel": row.openai_model or "gpt-4o-mini",
            "systemPromptOverride": row.system_prompt_override,
            "enabledPlatforms": row.enabled_platforms or DEFAULT_ENABLED_PLATFORMS,
            "notificationEvents": row.notification_events or DEFAULT_NOTIFICATION_EVENTS,
            "notificationDelivery": row.notification_delivery or "in_app",
            "usage": usage_snapshot(self.db, workspace),
        }

    def _get_template(self, workspace_id: uuid.UUID, template_id: uuid.UUID) -> SocialTemplate:
        row = self.db.get(SocialTemplate, template_id)
        if not row or row.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="Template not found")
        return row

    def _serialize_template(self, row: SocialTemplate) -> dict:
        return {
            "id": str(row.id),
            "workspaceId": str(row.workspace_id),
            "name": row.name,
            "category": row.category,
            "platforms": list(row.platforms or []),
            "captionTemplate": row.caption_template,
            "hashtags": list(row.hashtags or []),
            "isSystem": bool(row.is_system),
            "systemKey": row.system_key,
            "description": row.description or "",
            "goal": row.goal or "general",
            "placeholders": list(row.placeholders or []),
            "imagePrompt": row.image_prompt or "",
            "generateImage": bool(row.generate_image),
            "suggestedTone": row.suggested_tone or "Professional",
            "suggestedCta": row.suggested_cta or "",
            "firstCommentTemplate": row.first_comment_template or "",
            "sortOrder": row.sort_order or 0,
            "createdBy": str(row.created_by) if row.created_by else None,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
        }

    def _token_status(self, account: SocialAccount) -> str:
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
        if expires <= now + timedelta(days=7):
            return "expires_soon"
        return "active"
