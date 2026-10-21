"""HTTP access logging with correlation id."""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings
from app.core.db_metrics import start_request_metrics
from app.core.logging_config import set_correlation_id

logger = logging.getLogger("app.access")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex[:16]
        set_correlation_id(correlation_id)
        metrics = start_request_metrics()
        start = time.perf_counter()
        client = request.client.host if request.client else "unknown"
        path = request.url.path
        method = request.method

        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "request failed method=%s path=%s client=%s duration_ms=%.1f",
                method,
                path,
                client,
                elapsed_ms,
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000
        db_ms = metrics.micros / 1000
        logger.info(
            "request method=%s path=%s status=%s client=%s duration_ms=%.1f db_ops=%d db_ms=%.1f",
            method,
            path,
            response.status_code,
            client,
            elapsed_ms,
            metrics.count,
            db_ms,
        )
        response.headers["X-Correlation-Id"] = correlation_id
        if settings.debug:
            response.headers["Server-Timing"] = (
                f'db;dur={db_ms:.1f};desc="{metrics.count} ops", total;dur={elapsed_ms:.1f}'
            )
        return response
