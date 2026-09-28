from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utc_midnight(d: date) -> datetime:
    """MongoDB BSON has no plain date type — store/query calendar days as UTC midnight."""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def as_date(val: Any) -> date:
    """Normalize values read from Mongo (datetime, date, or YYYY-MM-DD string)."""
    if isinstance(val, datetime):
        dt = val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        return date.fromisoformat(val[:10])
    raise TypeError(f"Unsupported date value: {type(val)!r}")


def date_range_filter(start: date, end: date) -> dict[str, datetime]:
    """Inclusive calendar-day range for a datetime ``date`` field in Mongo."""
    return {
        "$gte": utc_midnight(start),
        "$lt": utc_midnight(end) + timedelta(days=1),
    }


def new_id() -> str:
    return str(uuid.uuid4())


def to_str_id(val: Any) -> str | None:
    if val is None:
        return None
    return str(val)


def public_doc(doc: dict | None) -> dict | None:
    """Return a copy of a Mongo document without the driver ``_id`` field."""
    if doc is None:
        return None
    out = dict(doc)
    out.pop("_id", None)
    return out
