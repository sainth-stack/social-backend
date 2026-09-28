from __future__ import annotations

from collections.abc import Generator

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.database import Database

from app.core.config import settings

_client: MongoClient | None = None


def _get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(settings.mongodb_url, serverSelectionTimeoutMS=10000)
    return _client


def get_database() -> Database:
    return _get_client()[settings.mongodb_db_name]


def get_db() -> Generator[Database, None, None]:
    db = get_database()
    yield db


def init_db() -> None:
    safe_url = settings.mongodb_url.split("@")[-1] if "@" in settings.mongodb_url else settings.mongodb_url
    print(f"[db] connecting to MongoDB: {safe_url}")
    _get_client().admin.command("ping")
    print("[db] MongoDB connected successfully")
    _ensure_indexes(get_database())


def _ensure_indexes(db: Database) -> None:
    db["users"].create_index([("email", ASCENDING)], unique=True)
    db["users"].create_index([("google_id", ASCENDING)], sparse=True)
    db["workspaces"].create_index([("owner_user_id", ASCENDING)])
    db["workspace_members"].create_index(
        [("user_id", ASCENDING), ("workspace_id", ASCENDING)], unique=True
    )
    db["social_accounts"].create_index(
        [("workspace_id", ASCENDING), ("platform", ASCENDING), ("platform_account_id", ASCENDING)],
        unique=True,
    )
    db["social_posts"].create_index([("workspace_id", ASCENDING), ("status", ASCENDING)])
    db["social_posts"].create_index([("workspace_id", ASCENDING), ("scheduled_at", ASCENDING)])
    db["social_post_platforms"].create_index([("post_id", ASCENDING)])
    db["social_media_assets"].create_index(
        [("workspace_id", ASCENDING), ("created_at", DESCENDING)]
    )
    db["social_analytics_daily"].create_index(
        [("workspace_id", ASCENDING), ("social_account_id", ASCENDING), ("date", ASCENDING)],
        unique=True,
    )
    db["social_audit_logs"].create_index(
        [("workspace_id", ASCENDING), ("created_at", DESCENDING)]
    )
    db["social_settings"].create_index([("workspace_id", ASCENDING)], unique=True)
    db["social_brand_voices"].create_index([("workspace_id", ASCENDING)], unique=True)
    db["social_templates"].create_index([("workspace_id", ASCENDING)])
    db["plans_ai_usage"].create_index(
        [("workspace_id", ASCENDING), ("created_at", DESCENDING)]
    )
    db["plan_overrides"].create_index([("plan_key", ASCENDING)], unique=True)
    print("[db] MongoDB indexes ensured")
