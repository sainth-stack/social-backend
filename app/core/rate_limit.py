"""Simple Redis-backed rate limiter for public auth endpoints."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

from workers.redis.client import get_redis_client

logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def enforce_rate_limit(
    request: Request,
    *,
    key_prefix: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Raise 429 if the caller exceeded `limit` hits in `window_seconds`."""
    ip = _client_ip(request)
    key = f"rl:{key_prefix}:{ip}"
    try:
        redis = get_redis_client()
        count = redis.incr(key)
        if count == 1:
            redis.expire(key, window_seconds)
        if int(count) > limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        # Fail open if Redis is down — do not block auth entirely.
        logger.warning("Rate limit check failed (%s); allowing request", exc)
