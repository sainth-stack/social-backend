from __future__ import annotations

import logging

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)


class _NullRedis:
    """Drop-in stub returned when the real Redis is unreachable.

    Every read returns None/False so callers that cache-miss gracefully
    proceed without caching. Writes are silently ignored.
    """

    def get(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        return False

    def setex(self, *args, **kwargs):
        return False

    def incr(self, *args, **kwargs):
        return 0

    def decr(self, *args, **kwargs):
        return 0

    def delete(self, *args, **kwargs):
        return 0

    def ping(self) -> bool:
        return False

    def exists(self, *args, **kwargs):
        return 0

    def expire(self, *args, **kwargs):
        return False

    def publish(self, *args, **kwargs):
        return 0

    def lpush(self, *args, **kwargs):
        return 0

    def rpush(self, *args, **kwargs):
        return 0

    def lrange(self, *args, **kwargs):
        return []

    def keys(self, *args, **kwargs):
        return []

    def hset(self, *args, **kwargs):
        return 0

    def hget(self, *args, **kwargs):
        return None

    def hgetall(self, *args, **kwargs):
        return {}

    def pipeline(self, *args, **kwargs):
        return self

    def execute(self, *args, **kwargs):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


_cached_client: "redis.Redis | None" = None


def get_redis_client() -> redis.Redis:
    """Shared Redis client. Falls back to a no-op stub if unreachable."""
    global _cached_client
    if _cached_client is not None and not isinstance(_cached_client, _NullRedis):
        return _cached_client
    try:
        client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        client.ping()
        _cached_client = client
        return client  # type: ignore[return-value]
    except Exception as exc:
        logger.warning(
            "Redis unavailable (%s) — using in-process no-op stub.",
            exc,
        )
        return _NullRedis()  # type: ignore[return-value]


def ping_redis() -> bool:
    try:
        client = get_redis_client()
        return bool(client.ping())
    except Exception:
        return False
