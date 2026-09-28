"""Redis helpers for content-plan Celery jobs (cancel + workspace ownership)."""

from __future__ import annotations

from fastapi import HTTPException, status

from workers.redis.client import get_redis_client

_JOB_TTL_SECONDS = 86_400
_CANCEL_TTL_SECONDS = 86_400


def _job_key(job_id: str) -> str:
    return f"content_plan:job:{job_id}"


def _cancel_key(job_id: str) -> str:
    return f"content_plan:cancel:{job_id}"


def register_content_plan_job(job_id: str, workspace_id: str) -> None:
    redis = get_redis_client()
    redis.setex(_job_key(job_id), _JOB_TTL_SECONDS, str(workspace_id))


def verify_content_plan_job_workspace(job_id: str, workspace_id: str) -> None:
    redis = get_redis_client()
    owner = redis.get(_job_key(job_id))
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found or expired")
    if str(owner) != str(workspace_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Job not in this workspace")


def request_content_plan_cancel(job_id: str) -> None:
    redis = get_redis_client()
    redis.setex(_cancel_key(job_id), _CANCEL_TTL_SECONDS, "1")


def is_content_plan_cancelled(job_id: str) -> bool:
    redis = get_redis_client()
    return bool(redis.get(_cancel_key(job_id)))


def clear_content_plan_job_flags(job_id: str) -> None:
    redis = get_redis_client()
    redis.delete(_cancel_key(job_id))
