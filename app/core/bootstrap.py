"""One-time startup bootstrap: seed the platform admin user from env vars."""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.database import get_database
from app.core.security import hash_password
from app.core.mongo_utils import new_id, utcnow

logger = logging.getLogger(__name__)


def seed_platform_admin() -> None:
    """Create (or promote) the platform admin defined by ADMIN_EMAIL / ADMIN_PASSWORD.

    Idempotent — safe to run on every startup. Does nothing if either env var
    is unset. If the user already exists, only ensures ``is_platform_admin``
    and ``is_active`` are set (the password is left untouched).
    """
    if not settings.admin_email or not settings.admin_password:
        return

    db = get_database()
    email = settings.admin_email.lower().strip()

    user = db["users"].find_one({"email": email})
    if user:
        if not user.get("is_platform_admin") or not user.get("is_active"):
            db["users"].update_one(
                {"email": email},
                {"$set": {"is_platform_admin": True, "is_active": True}},
            )
            logger.info("Promoted existing user %s to platform admin", email)
        return

    user_id = new_id()
    now = utcnow()
    db["users"].insert_one(
        {
            "id": user_id,
            "email": email,
            "password_hash": hash_password(settings.admin_password),
            "full_name": "Platform Admin",
            "is_active": True,
            "is_platform_admin": True,
            "google_id": None,
            "created_at": now,
            "updated_at": now,
        }
    )

    ws_id = new_id()
    db["workspaces"].insert_one(
        {
            "id": ws_id,
            "name": "OpsBrain Admin",
            "plan": "enterprise",
            "owner_user_id": user_id,
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
    )

    mem_id = new_id()
    db["workspace_members"].insert_one(
        {
            "id": mem_id,
            "user_id": user_id,
            "workspace_id": ws_id,
            "role": "owner",
            "social_level": "admin",
            "created_at": now,
        }
    )
    logger.info("Seeded platform admin user %s", email)
