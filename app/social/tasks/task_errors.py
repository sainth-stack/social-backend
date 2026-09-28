"""Human-readable Celery task failures (JSON backend drops Pydantic/FastAPI exception args)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException
from pydantic import ValidationError


def format_task_exception(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        return str(exc)
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            parts: list[str] = []
            for item in detail:
                if isinstance(item, dict):
                    loc = ".".join(str(x) for x in item.get("loc", ()))
                    msg = item.get("msg") or item.get("message") or ""
                    parts.append(f"{loc}: {msg}".strip(": "))
                else:
                    parts.append(str(item))
            return "; ".join(p for p in parts if p) or "Request validation failed"
        if detail is not None:
            return str(detail)
        return f"HTTP {exc.status_code}"
    text = str(exc).strip()
    if text and "ValidationError" not in text:
        return text
    name = exc.__class__.__name__
    return text or name


def celery_failure_message(result: Any, *, fallback: str = "Task failed") -> str:
    """Best-effort error string from a failed AsyncResult."""
    err = getattr(result, "result", None)
    if isinstance(err, dict) and err.get("error"):
        return str(err["error"])
    if isinstance(err, BaseException):
        msg = format_task_exception(err)
        if msg and not msg.startswith("<class"):
            return msg
    tb = getattr(result, "traceback", None)
    if tb:
        lines = [ln.strip() for ln in str(tb).strip().splitlines() if ln.strip()]
        for line in reversed(lines):
            if line.startswith("RuntimeError:"):
                return line.partition(":")[2].strip() or line
            if ": " in line and not line.startswith("File "):
                return line
        if lines:
            return lines[-1]
    if err is not None:
        text = str(err)
        if text and not text.startswith("<class"):
            return text
    return fallback
