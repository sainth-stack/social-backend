"""One-time startup bootstrap: seed the platform admin user from env vars."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.security import hash_password

logger = logging.getLogger(__name__)


def seed_platform_admin() -> None:
    """Create (or promote) the platform admin defined by ADMIN_EMAIL / ADMIN_PASSWORD.

    Idempotent — safe to run on every startup. Does nothing if either env var
    is unset. If the user already exists, only ensures ``is_platform_admin``
    and ``is_active`` are set (the password is left untouched).
    """
    if not settings.admin_email or not settings.admin_password:
        return

    from app.users.models import User
    from app.workspaces.models import SocialLevel, Workspace, WorkspaceMember, WorkspacePlan, WorkspaceRole

    email = settings.admin_email.lower().strip()
    db = SessionLocal()
    try:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user:
            if not user.is_platform_admin or not user.is_active:
                user.is_platform_admin = True
                user.is_active = True
                db.commit()
                logger.info("Promoted existing user %s to platform admin", email)
            return

        user = User(
            email=email,
            password_hash=hash_password(settings.admin_password),
            full_name="Platform Admin",
            is_active=True,
            is_platform_admin=True,
        )
        db.add(user)
        db.flush()

        workspace = Workspace(
            name="OpsBrain Admin",
            plan=WorkspacePlan.ENTERPRISE,
            owner_user_id=user.id,
            is_active=True,
        )
        db.add(workspace)
        db.flush()

        db.add(
            WorkspaceMember(
                user_id=user.id,
                workspace_id=workspace.id,
                role=WorkspaceRole.OWNER,
                social_level=SocialLevel.ADMIN,
            )
        )
        db.commit()
        logger.info("Seeded platform admin user %s", email)
    except IntegrityError:
        db.rollback()
        # Another uvicorn worker seeded the same admin concurrently.
        logger.info("Platform admin %s already exists", email)
    except Exception:
        db.rollback()
        logger.exception("Failed to seed platform admin (non-fatal)")
    finally:
        db.close()
