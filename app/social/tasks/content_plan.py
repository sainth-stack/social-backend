"""Background content plan generation (multi-day AI + schedule)."""

from __future__ import annotations

import logging
import uuid

from app.core.database import SessionLocal
from app.social.content_plan import ContentPlanService
from app.social.schemas import ContentPlanGenerateRequest
from app.users.models import User
from app.workspaces.models import Workspace
from workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="app.social.tasks.content_plan.generate_content_plan",
    max_retries=0,
    queue="social_maintenance",
)
def generate_content_plan_task(
    self,
    workspace_id: str,
    user_id: str,
    payload: dict,
) -> dict:
    db = SessionLocal()
    try:
        workspace = db.get(Workspace, uuid.UUID(workspace_id))
        user = db.get(User, uuid.UUID(user_id))
        if not workspace or not user:
            return {"error": "workspace_or_user_not_found"}

        request = ContentPlanGenerateRequest.model_validate(payload)
        total = min(int(request.days), 30)

        def progress(current: int, total_days: int, message: str) -> None:
            self.update_state(
                state="PROGRESS",
                meta={
                    "current": current,
                    "total": total_days,
                    "message": message,
                },
            )

        self.update_state(
            state="PROGRESS",
            meta={"current": 0, "total": total, "message": "Starting content plan…"},
        )
        result = ContentPlanService(db).generate(
            workspace,
            user,
            request,
            progress_callback=progress,
        )
        return result.model_dump(mode="json")
    except Exception as exc:
        logger.exception("generate_content_plan_task failed workspace=%s", workspace_id)
        raise exc
    finally:
        db.close()
