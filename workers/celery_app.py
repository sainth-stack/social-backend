from __future__ import annotations

import math
import os

# Patch before Celery worker submodules bind load_average (macOS can raise OSError).
import celery.utils.sysinfo as celery_sysinfo


def _safe_load_average() -> tuple[float, float, float]:
    try:
        return tuple(math.ceil(value * 100) / 100 for value in os.getloadavg())
    except OSError:
        return (0.0, 0.0, 0.0)


celery_sysinfo._load_average = _safe_load_average
celery_sysinfo.load_average = _safe_load_average

from celery import Celery

from app.core.config import settings
from app.core.logging_config import configure_logging
from workers.beat_schedule import beat_schedule

configure_logging(json_logs=settings.json_logs)

celery_app = Celery(
    "opsbrain_social",
    broker=settings.resolved_celery_broker_url,
    backend=settings.resolved_celery_result_backend,
    include=[
        "app.social.tasks.publish",
        "app.social.tasks.scheduler",
        "app.social.tasks.retry",
        "app.social.tasks.analytics_sync",
        "app.social.tasks.maintenance",
        "app.social.tasks.content_plan",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_priority=5,
    broker_transport_options={
        "queue_order_strategy": "priority",
        "priority_steps": list(range(10)),
    },
    result_backend_transport_options={
        "queue_order_strategy": "priority",
        "priority_steps": list(range(10)),
    },
    task_routes={
        "app.social.tasks.publish.*": {"queue": "social_publish"},
        "app.social.tasks.scheduler.*": {"queue": "social_publish"},
        "app.social.tasks.retry.*": {"queue": "social_publish"},
        "app.social.tasks.analytics_sync.*": {"queue": "social_analytics"},
        "app.social.tasks.maintenance.*": {"queue": "social_maintenance"},
        "app.social.tasks.content_plan.*": {"queue": "social_maintenance"},
    },
    beat_schedule=beat_schedule,
)
