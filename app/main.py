from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.admin.router import router as admin_router
from app.auth.router import router as auth_router
from app.core.bootstrap import seed_platform_admin
from app.core.config import settings
from app.core.database import SessionLocal, init_db
from app.core.exceptions import register_exception_handlers
from app.core.logging_config import configure_logging
from app.plans.router import router as plans_router
from app.social.router import oauth_callback_router as social_oauth_callback_router
from app.social.router import router as social_router
from app.workspaces.router import router as workspaces_router
from workers.redis.client import ping_redis

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    configure_logging(json_logs=settings.json_logs)

    if settings.jwt_secret_key == "CHANGE_ME_IN_PRODUCTION" and not settings.debug:
        logger.warning(
            "JWT_SECRET_KEY is still the insecure default. Set a strong secret before production."
        )

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.debug,
    )

    register_exception_handlers(app)

    allow_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
    if "*" in allow_origins and not settings.debug:
        logger.warning("CORS_ORIGINS contains '*' — restrict this in production.")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["health"])
    def health() -> dict:
        db_ok = False
        redis_ok = False
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
                db_ok = True
        except Exception as exc:
            logger.warning("Health DB check failed: %s", exc)
        try:
            redis_ok = ping_redis()
        except Exception as exc:
            logger.warning("Health Redis check failed: %s", exc)

        status = "ok" if db_ok else "degraded"
        return {
            "status": status,
            "service": settings.app_name,
            "checks": {
                "database": "ok" if db_ok else "error",
                "redis": "ok" if redis_ok else "error",
            },
        }

    @app.on_event("startup")
    def _startup() -> None:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(init_db)
            try:
                future.result(timeout=15)
            except concurrent.futures.TimeoutError:
                print("[db] startup check timed out (non-fatal) — continuing anyway")
            except Exception as exc:
                print(f"[db] startup check failed (non-fatal): {exc}")

        try:
            seed_platform_admin()
        except Exception as exc:
            print(f"[bootstrap] platform admin seed failed (non-fatal): {exc}")

    app.include_router(auth_router, prefix=settings.api_v1_prefix)
    app.include_router(workspaces_router, prefix=settings.api_v1_prefix)
    app.include_router(plans_router, prefix=settings.api_v1_prefix)
    app.include_router(admin_router, prefix=settings.api_v1_prefix)
    app.include_router(social_router, prefix=settings.api_v1_prefix)
    app.include_router(social_oauth_callback_router)

    return app


app = create_app()
