"""Background content plan generation (multi-day AI + schedule)."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import ValidationError

from app.core.database import get_database
from app.social.content_plan import ContentPlanService
from app.social.schemas import ContentPlanGenerateRequest
from app.social.tasks.task_errors import format_task_exception
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
    db = get_database()

    workspace = db["workspaces"].find_one({"id": str(workspace_id)})
    user = db["users"].find_one({"id": str(user_id)})
    if not workspace or not user:
        return {"error": "workspace_or_user_not_found"}

    try:
        request = ContentPlanGenerateRequest.model_validate(payload)
    except ValidationError as exc:
        msg = format_task_exception(exc)
        logger.error("content plan payload invalid workspace=%s: %s", workspace_id, msg)
        return {"error": msg}
    total = min(int(request.days), 30)

    def progress(current: int, total_days: int, message: str) -> None:
        self.update_state(
            state="PROGRESS",
            meta={"current": current, "total": total_days, "message": message},
        )

    self.update_state(
        state="PROGRESS",
        meta={"current": 0, "total": total, "message": "Starting content plan…"},
    )
    try:
        from app.social.content_plan_jobs import is_content_plan_cancelled

        def cancel_check() -> bool:
            return is_content_plan_cancelled(self.request.id)

        result = ContentPlanService(db).generate(
            workspace,
            user,
            request,
            progress_callback=progress,
            cancel_check=cancel_check,
        )
        return result.model_dump(mode="json")
    except HTTPException as exc:
        msg = format_task_exception(exc)
        logger.warning("generate_content_plan_task rejected workspace=%s: %s", workspace_id, msg)
        return {"error": msg}
    except ValidationError as exc:
        msg = format_task_exception(exc)
        logger.exception("generate_content_plan_task validation workspace=%s: %s", workspace_id, msg)
        return {"error": msg}
    except Exception as exc:
        msg = format_task_exception(exc)
        logger.exception("generate_content_plan_task failed workspace=%s: %s", workspace_id, msg)
        raise RuntimeError(msg) from exc
    finally:
        from app.social.content_plan_jobs import clear_content_plan_job_flags

        clear_content_plan_job_flags(self.request.id)
