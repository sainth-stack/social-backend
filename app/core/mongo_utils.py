from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
