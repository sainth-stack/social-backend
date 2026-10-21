from __future__ import annotations

import fnmatch
import time
from typing import Any

import mongomock
import pytest
from fastapi.testclient import TestClient

import workers.redis.client as redis_client
from app.core.database import _ensure_indexes, get_db
from app.main import app


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, tuple[str, float | None]] = {}

    def _alive(self, key: str) -> bool:
        item = self.store.get(key)
        if item is None:
            return False
        if item[1] is not None and item[1] <= time.monotonic():
            del self.store[key]
            return False
        return True

    def get(self, key: str) -> Any:
        return self.store[key][0] if self._alive(key) else None

    def set(self, key: str, value: Any, ex: int | None = None, nx: bool = False) -> bool:
        if nx and self._alive(key):
            return False
        self.store[key] = (str(value), time.monotonic() + ex if ex else None)
        return True

    def setex(self, key: str, ttl: int, value: Any) -> bool:
        return self.set(key, value, ex=ttl)

    def incr(self, key: str) -> int:
        expiry = self.store[key][1] if self._alive(key) else None
        value = int(self.get(key) or 0) + 1
        self.store[key] = (str(value), expiry)
        return value

    def expire(self, key: str, ttl: int) -> bool:
        if not self._alive(key):
            return False
        self.store[key] = (self.store[key][0], time.monotonic() + ttl)
        return True

    def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    def keys(self, pattern: str = "*") -> list[str]:
        return [k for k in list(self.store) if self._alive(k) and fnmatch.fnmatch(k, pattern)]

    def ping(self) -> bool:
        return True

    def pipeline(self, transaction: bool = True) -> "FakePipeline":
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def queue(*args: Any, **kwargs: Any) -> "FakePipeline":
            self.ops.append((name, args, kwargs))
            return self

        return queue

    def execute(self) -> list[Any]:
        results = [getattr(self.redis, name)(*args, **kwargs) for name, args, kwargs in self.ops]
        self.ops = []
        return results


@pytest.fixture()
def db():
    database = mongomock.MongoClient(tz_aware=False)["test_db"]
    _ensure_indexes(database)
    return database


@pytest.fixture()
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "_cached_client", fake)
    return fake


@pytest.fixture()
def client(db, fake_redis, monkeypatch):
    sent: list[tuple[str, dict]] = []
    from app.email.service import email_service

    monkeypatch.setattr(
        email_service, "send_welcome", lambda **kw: sent.append(("welcome", kw)) or True
    )
    monkeypatch.setattr(
        email_service, "send_password_reset", lambda **kw: sent.append(("reset", kw)) or True
    )

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    test_client = TestClient(app)
    test_client.sent_emails = sent  # type: ignore[attr-defined]
    yield test_client
    app.dependency_overrides.clear()
