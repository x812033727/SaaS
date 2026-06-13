"""FastAPI application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

import saas_mvp
from saas_mvp.db import init_db
from saas_mvp.routers import auth, notes, tenants


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

    @app.get("/", tags=["root"])
    def root():
        return {
            "service": "saas-mvp",
            "version": saas_mvp.__version__,
            "status": "ok",
        }

    return app


app = create_app()
