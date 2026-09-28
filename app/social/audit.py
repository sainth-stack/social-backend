"""Social-module audit logging."""

from __future__ import annotations

from typing import Any, Optional

from pymongo.database import Database

from app.core.mongo_utils import new_id, utcnow


def write_social_audit(
    db: Database,
    *,
    workspace_id: str,
    action: str,
    entity_type: str = "post",
    entity_id: Optional[str] = None,
    user_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    commit: bool = False,  # kept for API compatibility; MongoDB auto-commits
) -> None:
    db["social_audit_logs"].insert_one(
        {
            "id": new_id(),
            "workspace_id": str(workspace_id),
            "user_id": str(user_id) if user_id else None,
            "action": action,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id else None,
            "metadata_json": metadata or {},
            "ip_address": ip_address,
            "created_at": utcnow(),
        }
    )
