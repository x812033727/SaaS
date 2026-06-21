"""FastAPI application factory."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import saas_mvp
from saas_mvp.auth.dependencies import UIForbidden, UILoginRequired, UITenantDisabled
from saas_mvp.db import init_db
from saas_mvp.routers import auth, notes, tenants, ui
from saas_mvp.routers import quota as quota_router
from saas_mvp.routers import api_keys as api_keys_router
from saas_mvp.routers import usage as usage_router
from saas_mvp.routers import billing as billing_router
from saas_mvp.routers import admin as admin_router
from saas_mvp.routers import line_webhook as line_webhook_router
from saas_mvp.routers import slots as slots_router
from saas_mvp.routers import reservations as reservations_router
from saas_mvp.routers import customers as customers_router

_PKG_DIR = Path(__file__).resolve().parent  # src/saas_mvp


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
    app.include_router(slots_router.router)
    app.include_router(reservations_router.router)
    app.include_router(customers_router.router)

    # ── 伺服器渲染管理 UI（同源）：靜態檔 + UI 路由 + UI 例外 → HTML 行為 ──
    app.mount("/static", StaticFiles(directory=str(_PKG_DIR / "static")), name="static")
    app.include_router(ui.router)

    @app.exception_handler(UILoginRequired)
    async def _ui_login_required(request: Request, exc: UILoginRequired):
        # UI 未登入：重導登入頁（而非 API 的 JSON 401）
        return RedirectResponse("/ui/login", status_code=303)

    @app.exception_handler(UIForbidden)
    async def _ui_forbidden(request: Request, exc: UIForbidden):
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'>"
            "<h1>403 — 沒有權限</h1><p>此頁需要管理員權限。</p>"
            "<p><a href='/ui/'>返回儀表板</a></p>",
            status_code=403,
        )

    @app.exception_handler(UITenantDisabled)
    async def _ui_tenant_disabled(request: Request, exc: UITenantDisabled):
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'>"
            "<h1>帳號已停用</h1><p>此店家帳號已被平台停用，請聯絡管理員。</p>"
            "<p><a href='/ui/logout'>登出</a></p>",
            status_code=403,
        )

    @app.get("/", tags=["root"])
    def root():
        return {
            "service": "saas-mvp",
            "version": saas_mvp.__version__,
            "status": "ok",
        }

    return app


app = create_app()
