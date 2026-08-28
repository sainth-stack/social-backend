"""Social-module audit logging."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.social.models import SocialAuditLog


def write_social_audit(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    action: str,
    entity_type: str = "post",
    entity_id: Optional[uuid.UUID] = None,
    user_id: Optional[uuid.UUID] = None,
    metadata: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    commit: bool = False,
) -> None:
    db.add(
        SocialAuditLog(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata_json=metadata or {},
            ip_address=ip_address,
        )
    )
    if commit:
        db.commit()
    else:
        db.flush()
