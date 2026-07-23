from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.admin.router import router as admin_router
from app.auth.router import router as auth_router
from app.core.bootstrap import seed_platform_admin
from app.core.config import settings
from app.core.database import init_db
from app.core.logging_config import configure_logging
from app.plans.router import router as plans_router
from app.social.router import oauth_callback_router as social_oauth_callback_router
from app.social.router import router as social_router
from app.workspaces.router import router as workspaces_router


def create_app() -> FastAPI:
    configure_logging(json_logs=settings.json_logs)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.debug,
    )

    allow_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["health"])
    def health() -> dict:
        return {"status": "ok", "service": settings.app_name}

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

    # ── Authenticated API routers (all under /api/v1) ───────────────────────────
    app.include_router(auth_router, prefix=settings.api_v1_prefix)
    app.include_router(workspaces_router, prefix=settings.api_v1_prefix)
    app.include_router(plans_router, prefix=settings.api_v1_prefix)
    app.include_router(admin_router, prefix=settings.api_v1_prefix)
    app.include_router(social_router, prefix=settings.api_v1_prefix)

    # ── Public routers (no auth — OAuth provider callbacks) ─────────────────────
    app.include_router(social_oauth_callback_router)

    return app


app = create_app()
