"""Short-lived Redis cache for dashboard payloads, invalidated by bumping a per-workspace version."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from workers.redis.client import get_redis_client

logger = logging.getLogger(__name__)

DASHBOARD_CACHE_TTL_SECONDS = 30


def _version_key(workspace_id: str) -> str:
    return f"dash:ver:{workspace_id}"


def cached_dashboard(workspace_id: str, kind: str, compute: Callable[[], dict]) -> dict:
    try:
        redis = get_redis_client()
        version = redis.get(_version_key(workspace_id)) or "0"
        key = f"dash:{kind}:{workspace_id}:{version}"
        raw = redis.get(key)
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning("dashboard cache read failed: %s", exc)
        return compute()

    payload = compute()
    try:
        redis.setex(key, DASHBOARD_CACHE_TTL_SECONDS, json.dumps(payload, default=_json_default))
    except Exception as exc:
        logger.warning("dashboard cache write failed: %s", exc)
    return payload


def invalidate_dashboard(workspace_id: str) -> None:
    try:
        redis = get_redis_client()
        pipe = redis.pipeline(transaction=False)
        pipe.incr(_version_key(workspace_id))
        pipe.expire(_version_key(workspace_id), 86400)
        pipe.execute()
    except Exception as exc:
        logger.warning("dashboard cache invalidation failed: %s", exc)


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    return str(value)
