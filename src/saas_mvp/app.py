"""FastAPI application factory."""

from fastapi import FastAPI

from saas_mvp.db import init_db
from saas_mvp.routers import auth, notes


def create_app() -> FastAPI:
    app = FastAPI(
        title="SaaS MVP",
        description="Multi-tenant SaaS REST API",
        version="0.1.0",
    )

    @app.on_event("startup")
    def on_startup():
        init_db()

    app.include_router(auth.router)
    app.include_router(notes.router)

    @app.get("/", tags=["root"])
    def root():
        return {"service": "saas-mvp", "version": "0.1.0", "status": "ok"}

    return app


app = create_app()
