"""Application settings for the OpsBrain AI Social Media Manager backend."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url


def normalize_database_url(raw: str) -> URL:
    url = make_url(raw.strip())
    if url.drivername == "postgresql":
        return url.set(drivername="postgresql+psycopg2")
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        env_ignore_empty=True,
        extra="ignore",
    )

    # ── App ───────────────────────────────────────────────────────────────────
    app_name: str = Field(default="OpsBrain AI Social Media Manager", validation_alias="APP_NAME")
    debug: bool = Field(default=False, validation_alias="DEBUG")
    json_logs: bool = Field(default=False, validation_alias="JSON_LOGS")
    api_v1_prefix: str = Field(default="/api/v1", validation_alias="API_V1_PREFIX")
    cors_origins: str = Field(
        default="http://localhost:3001,http://localhost:3000",
        validation_alias="CORS_ORIGINS",
    )
    frontend_url: str = Field(default="http://localhost:3001", validation_alias="FRONTEND_URL")

    # ── Auth ──────────────────────────────────────────────────────────────────
    jwt_secret_key: str = Field(default="CHANGE_ME_IN_PRODUCTION", validation_alias="JWT_SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", validation_alias="JWT_ALGORITHM")
    access_token_exp_minutes: int = Field(default=60 * 24 * 7, validation_alias="ACCESS_TOKEN_EXP_MINUTES")
    credential_encryption_key: str = Field(default="", validation_alias="CREDENTIAL_ENCRYPTION_KEY")

    # ── Platform admin bootstrap ──────────────────────────────────────────────
    admin_email: Optional[str] = Field(default=None, validation_alias="ADMIN_EMAIL")
    admin_password: Optional[str] = Field(default=None, validation_alias="ADMIN_PASSWORD")

    # ── Database ──────────────────────────────────────────────────────────────
    database_url_override: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_URL", "SQLALCHEMY_DATABASE_URL"),
    )
    db_pool_size: int = Field(default=5, validation_alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=10, validation_alias="DB_MAX_OVERFLOW")
    db_pool_timeout: int = Field(default=10, validation_alias="DB_POOL_TIMEOUT")

    @property
    def database_url(self) -> URL:
        if self.database_url_override and self.database_url_override.strip():
            return normalize_database_url(self.database_url_override)
        raise RuntimeError("DATABASE_URL is required")

    # ── Cache / Queue — Redis ──────────────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0", validation_alias="REDIS_URL")
    celery_broker_url: Optional[str] = Field(default=None, validation_alias="CELERY_BROKER_URL")
    celery_result_backend: Optional[str] = Field(default=None, validation_alias="CELERY_RESULT_BACKEND")

    @property
    def resolved_celery_broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def resolved_celery_result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    # ── LLM — Azure OpenAI (chat + embeddings + image/video generation) ──────
    azure_openai_api_key: Optional[str] = Field(default=None, validation_alias="AZURE_OPENAI_API_KEY")
    azure_openai_endpoint: Optional[str] = Field(default=None, validation_alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_api_version: str = Field(
        default="2024-08-01-preview", validation_alias="AZURE_OPENAI_API_VERSION"
    )
    azure_openai_deployment: str = Field(
        default="gpt-4o-mini", validation_alias="AZURE_OPENAI_DEPLOYMENT"
    )
    # Image generation — gpt-image-2 requires api-version=preview
    azure_openai_image_deployment: str = Field(
        default="gpt-image-2", validation_alias="AZURE_OPENAI_IMAGE_DEPLOYMENT"
    )
    azure_openai_image_api_version: str = Field(
        default="preview", validation_alias="AZURE_OPENAI_IMAGE_API_VERSION"
    )
    # Video generation — Sora 2 (gated preview); optional separate endpoint
    azure_openai_video_deployment: str = Field(
        default="sora-2", validation_alias="AZURE_OPENAI_VIDEO_DEPLOYMENT"
    )
    azure_openai_video_api_version: str = Field(
        default="preview", validation_alias="AZURE_OPENAI_VIDEO_API_VERSION"
    )
    azure_openai_video_endpoint: Optional[str] = Field(
        default=None, validation_alias="AZURE_OPENAI_VIDEO_ENDPOINT"
    )
    azure_openai_video_api_key: Optional[str] = Field(
        default=None, validation_alias="AZURE_OPENAI_VIDEO_API_KEY"
    )
    video_generation_enabled: bool = Field(
        default=True, validation_alias="VIDEO_GENERATION_ENABLED"
    )

    # ── Object Storage — Azure Blob Storage ───────────────────────────────────
    azure_storage_connection_string: Optional[str] = Field(
        default=None, validation_alias="AZURE_STORAGE_CONNECTION_STRING"
    )
    azure_storage_account_name: Optional[str] = Field(
        default=None, validation_alias="AZURE_STORAGE_ACCOUNT_NAME"
    )
    azure_storage_account_key: Optional[str] = Field(
        default=None, validation_alias="AZURE_STORAGE_ACCOUNT_KEY"
    )
    azure_storage_container_name: Optional[str] = Field(
        default=None, validation_alias="AZURE_STORAGE_CONTAINER_NAME"
    )
    azure_storage_prefix: str = Field(default="social", validation_alias="AZURE_STORAGE_PREFIX")

    # ── Meta (Facebook + Instagram) OAuth ─────────────────────────────────────
    meta_app_id: str = Field(default="", validation_alias="META_APP_ID")
    meta_app_secret: str = Field(default="", validation_alias="META_APP_SECRET")
    meta_api_version: str = Field(default="v19.0", validation_alias="META_API_VERSION")
    meta_social_redirect_uri: str = Field(
        default="http://localhost:8001/api/v1/social/oauth/{platform}/callback",
        validation_alias="META_SOCIAL_REDIRECT_URI",
    )
    meta_instagram_app_id: str = Field(default="", validation_alias="META_INSTAGRAM_APP_ID")
    meta_instagram_app_secret: str = Field(default="", validation_alias="META_INSTAGRAM_APP_SECRET")
    meta_instagram_redirect_uri: str = Field(default="", validation_alias="META_INSTAGRAM_REDIRECT_URI")

    # ── LinkedIn OAuth ─────────────────────────────────────────────────────────
    linkedin_client_id: str = Field(default="", validation_alias="LINKEDIN_CLIENT_ID")
    linkedin_client_secret: str = Field(default="", validation_alias="LINKEDIN_CLIENT_SECRET")
    linkedin_redirect_uri: str = Field(
        default="http://localhost:8001/api/v1/social/oauth/linkedin/callback",
        validation_alias="LINKEDIN_REDIRECT_URI",
    )
    linkedin_organization_scopes: bool = Field(
        default=False, validation_alias="LINKEDIN_ORGANIZATION_SCOPES"
    )

    # ── X (Twitter) OAuth 2.0 PKCE ─────────────────────────────────────────────
    x_client_id: str = Field(default="", validation_alias="X_CLIENT_ID")
    x_client_secret: str = Field(default="", validation_alias="X_CLIENT_SECRET")
    x_redirect_uri: str = Field(
        default="http://localhost:8001/api/v1/social/oauth/x/callback",
        validation_alias="X_REDIRECT_URI",
    )

    # ── Misc ──────────────────────────────────────────────────────────────────
    default_workspace_timezone: str = Field(
        default="Asia/Kolkata", validation_alias="DEFAULT_WORKSPACE_TIMEZONE"
    )


settings = Settings()
