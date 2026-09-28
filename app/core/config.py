"""Application settings for the OpsBrain AI Social Media Manager backend."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # ── Google OAuth (user sign-in) ───────────────────────────────────────────
    google_client_id: str = Field(default="", validation_alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", validation_alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = Field(
        default="http://localhost:8000/api/v1/auth/google/callback",
        validation_alias="GOOGLE_REDIRECT_URI",
    )

    # ── Platform admin bootstrap ──────────────────────────────────────────────
    admin_email: Optional[str] = Field(default=None, validation_alias="ADMIN_EMAIL")
    admin_password: Optional[str] = Field(default=None, validation_alias="ADMIN_PASSWORD")

    # ── Database — MongoDB ────────────────────────────────────────────────────
    mongodb_url: str = Field(default="mongodb://localhost:27017", validation_alias="MONGODB_URL")
    mongodb_db_name: str = Field(default="social_media", validation_alias="MONGODB_DB_NAME")

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

    # ── LLM — OpenAI ─────────────────────────────────────────────────────────
    openai_api_key: Optional[str] = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", validation_alias="OPENAI_BASE_URL")
    openai_deployment: str = Field(default="gpt-4o-mini", validation_alias="OPENAI_DEPLOYMENT")
    openai_image_deployment: str = Field(default="dall-e-3", validation_alias="OPENAI_IMAGE_DEPLOYMENT")
    video_generation_enabled: bool = Field(default=False, validation_alias="VIDEO_GENERATION_ENABLED")

    # ── Object Storage — Amazon S3 ────────────────────────────────────────────
    aws_access_key_id: Optional[str] = Field(default=None, validation_alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: Optional[str] = Field(
        default=None, validation_alias="AWS_SECRET_ACCESS_KEY"
    )
    aws_region: str = Field(default="ap-south-1", validation_alias="AWS_REGION")
    aws_bucket_name: Optional[str] = Field(default=None, validation_alias="AWS_BUCKET_NAME")
    aws_storage_prefix: str = Field(default="peers", validation_alias="AWS_STORAGE_PREFIX")

    # ── Meta (Facebook + Instagram) OAuth ─────────────────────────────────────
    meta_app_id: str = Field(default="", validation_alias="META_APP_ID")
    meta_app_secret: str = Field(default="", validation_alias="META_APP_SECRET")
    meta_api_version: str = Field(default="v19.0", validation_alias="META_API_VERSION")
    meta_social_redirect_uri: str = Field(
        default="http://localhost:8000/api/v1/social/oauth/{platform}/callback",
        validation_alias="META_SOCIAL_REDIRECT_URI",
    )
    meta_instagram_app_id: str = Field(default="", validation_alias="META_INSTAGRAM_APP_ID")
    meta_instagram_app_secret: str = Field(default="", validation_alias="META_INSTAGRAM_APP_SECRET")
    meta_instagram_redirect_uri: str = Field(default="", validation_alias="META_INSTAGRAM_REDIRECT_URI")

    # ── LinkedIn OAuth ─────────────────────────────────────────────────────────
    linkedin_client_id: str = Field(default="", validation_alias="LINKEDIN_CLIENT_ID")
    linkedin_client_secret: str = Field(default="", validation_alias="LINKEDIN_CLIENT_SECRET")
    linkedin_redirect_uri: str = Field(
        default="http://localhost:8000/api/v1/social/oauth/linkedin/callback",
        validation_alias="LINKEDIN_REDIRECT_URI",
    )
    linkedin_organization_scopes: bool = Field(
        default=False, validation_alias="LINKEDIN_ORGANIZATION_SCOPES"
    )

    # ── X (Twitter) OAuth 2.0 PKCE ─────────────────────────────────────────────
    x_client_id: str = Field(default="", validation_alias="X_CLIENT_ID")
    x_client_secret: str = Field(default="", validation_alias="X_CLIENT_SECRET")
    x_redirect_uri: str = Field(
        default="http://localhost:8000/api/v1/social/oauth/x/callback",
        validation_alias="X_REDIRECT_URI",
    )

    # ── Email — Resend (shared with OpsBrain-Backend) ─────────────────────────
    resend_api_key: Optional[str] = Field(default=None, validation_alias="RESEND_API_KEY")
    resend_from_email: str = Field(
        default="noreply@opsbrainai.com", validation_alias="RESEND_FROM_EMAIL"
    )

    # ── Misc ──────────────────────────────────────────────────────────────────
    default_workspace_timezone: str = Field(
        default="Asia/Kolkata", validation_alias="DEFAULT_WORKSPACE_TIMEZONE"
    )


settings = Settings()
