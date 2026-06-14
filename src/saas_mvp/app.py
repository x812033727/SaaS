"""FastAPI application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

import saas_mvp
from saas_mvp.db import init_db
from saas_mvp.routers import auth, notes, tenants
from saas_mvp.routers import quota as quota_router
from saas_mvp.routers import api_keys as api_keys_router
from saas_mvp.routers import usage as usage_router
from saas_mvp.routers import billing as billing_router
from saas_mvp.routers import admin as admin_router
from saas_mvp.routers import line_webhook as line_webhook_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle (replaces deprecated @on_event)."""
    init_db()
    yield
    # teardown hooks go here in future tasks


def create_app() -> FastAPI:
    app = FastAPI(
        title="SaaS MVP",
        description="Multi-tenant SaaS REST API",
        version=saas_mvp.__version__,
        lifespan=lifespan,
    )

    app.include_router(auth.router)
    app.include_router(tenants.router)
    app.include_router(notes.router)
    app.include_router(quota_router.router)
    app.include_router(api_keys_router.router)
    app.include_router(usage_router.router)
    app.include_router(billing_router.router)
    app.include_router(admin_router.router)
    app.include_router(line_webhook_router.router)

    @app.get("/", tags=["root"])
    def root():
        return {
            "service": "saas-mvp",
            "version": saas_mvp.__version__,
            "status": "ok",
        }

    return app


app = create_app()
