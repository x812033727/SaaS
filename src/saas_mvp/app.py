"""FastAPI application factory."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import saas_mvp
from saas_mvp.auth.dependencies import UIForbidden, UILoginRequired, UITenantDisabled
from saas_mvp.config import settings
from saas_mvp.db import init_db
from saas_mvp.obs import ObservabilityMiddleware, configure_logging, install_error_handlers
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
from saas_mvp.routers import coupons as coupons_router
from saas_mvp.routers import analytics as analytics_router
from saas_mvp.routers import products as products_router
from saas_mvp.routers import orders as orders_router
from saas_mvp.routers import payments as payments_router
from saas_mvp.routers import locations as locations_router
from saas_mvp.routers import staff as staff_router
from saas_mvp.routers import staff_portal as staff_portal_router
from saas_mvp.routers import services as services_router
from saas_mvp.routers import calendar as calendar_router
from saas_mvp.routers import profile as profile_router
from saas_mvp.routers import portfolio as portfolio_router
from saas_mvp.routers import public as public_router
from saas_mvp.routers import oauth as oauth_router
from saas_mvp.routers import campaigns as campaigns_router
from saas_mvp.routers import pos as pos_router
from saas_mvp.routers import ai as ai_router
from saas_mvp.routers import pii as pii_router
from saas_mvp.routers import booking_form as booking_form_router
from saas_mvp.routers import flex_menu as flex_menu_router
from saas_mvp.routers import rich_menu as rich_menu_router
from saas_mvp.routers import auto_reply_rules as auto_reply_rules_router

_PKG_DIR = Path(__file__).resolve().parent  # src/saas_mvp


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle (replaces deprecated @on_event)."""
    init_db()
    # SSE 即時通知：events_backend=redis 時啟動跨 worker pub/sub listener
    # （memory 預設回 None，行內單 worker 廣播，行為不變）。
    from saas_mvp.services import events as events_svc

    events_task = await events_svc.start_redis_fanout()
    try:
        yield
    finally:
        if events_task is not None:
            events_task.cancel()
            try:
                await events_task
            except BaseException:  # noqa: BLE001 — 收尾不得拋（含 CancelledError）
                pass


def create_app() -> FastAPI:
    # 結構化日誌（JSON/text）：在建立 app、掛 middleware 之前先設定好 root logger。
    configure_logging()

    app = FastAPI(
        title="SaaS MVP",
        description="Multi-tenant SaaS REST API",
        version=saas_mvp.__version__,
        lifespan=lifespan,
    )

    # 可觀測性：request-id 串接 + 結構化存取日誌 + Prometheus HTTP 指標。
    app.add_middleware(ObservabilityMiddleware, metrics_enabled=settings.metrics_enabled)

    # 集中式未捕捉例外 → 一致 JSON envelope + 錯誤追蹤（不外洩內部訊息）。
    install_error_handlers(app)

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
    app.include_router(coupons_router.router)
    app.include_router(analytics_router.router)
    app.include_router(products_router.router)
    app.include_router(orders_router.router)
    app.include_router(payments_router.router)
    app.include_router(locations_router.router)
    app.include_router(staff_router.router)
    app.include_router(staff_portal_router.router)
    app.include_router(services_router.router)
    app.include_router(calendar_router.router)
    app.include_router(profile_router.router)
    app.include_router(portfolio_router.router)
    app.include_router(public_router.router)
    app.include_router(oauth_router.router)
    app.include_router(campaigns_router.router)
    app.include_router(pos_router.router)
    app.include_router(ai_router.router)
    app.include_router(pii_router.router)
    app.include_router(booking_form_router.router)
    app.include_router(flex_menu_router.router)
    app.include_router(rich_menu_router.router)
    app.include_router(auto_reply_rules_router.router)

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

    @app.get("/healthz", tags=["root"])
    def healthz():
        """輕量就緒/存活探針（供 load balancer / k8s probe）：無認證、低成本。

        - db：對 DB 跑一次 ``SELECT 1``，OK → "ok"，例外 → "error"。
        - rate_limit_backend：目前生效的限流後端（"memory" / "redis"）。
        DB 不可用時回 503，讓 LB 把此 worker 拉出輪替。
        """
        from sqlalchemy import text

        from saas_mvp.auth.ratelimit import effective_backend_name
        from saas_mvp.db import SessionLocal

        db_status = "ok"
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 — 探針不得拋例外，僅回報狀態
            db_status = "error"
        finally:
            db.close()

        from saas_mvp.services.events import broker as _event_broker

        body = {
            "status": "ok" if db_status == "ok" else "error",
            "db": db_status,
            # 回報實際生效後端（設定 redis 但降級時會是 "memory"）
            "rate_limit_backend": effective_backend_name(),
            # SSE 事件廣播：實際生效後端（redis 啟用成功才是 "redis"）
            "events_backend": "redis" if _event_broker.redis_enabled() else "memory",
        }
        status_code = 200 if db_status == "ok" else 503
        return JSONResponse(body, status_code=status_code)

    @app.get("/readyz", tags=["root"])
    def readyz():
        """就緒探針（readiness）：相依檢查通過才回 200，否則 503。

        與 ``/healthz`` 的差異：``/healthz`` 偏存活/輕量；``/readyz`` 是
        「是否可接流量」的就緒判斷，逐項回報相依狀態（目前為 DB + 限流後端），
        供 LB / k8s readiness probe 在相依未就緒時暫不導流。
        """
        from sqlalchemy import text

        from saas_mvp.auth.ratelimit import effective_backend_name
        from saas_mvp.db import SessionLocal

        checks: dict[str, str] = {}
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            checks["db"] = "ok"
        except Exception:  # noqa: BLE001 — 探針不得拋例外
            checks["db"] = "error"
        finally:
            db.close()

        ready = all(v == "ok" for v in checks.values())
        body = {
            "status": "ready" if ready else "not_ready",
            "checks": checks,
            "rate_limit_backend": effective_backend_name(),
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get("/metrics", tags=["root"])
    def metrics_endpoint(request: Request):
        """Prometheus 文字格式指標（per-worker）。

        - ``SAAS_METRICS_ENABLED=false`` → 404（完全停用）。
        - ``SAAS_METRICS_TOKEN`` 非空 → 需帶 ``Authorization: Bearer <token>``，
          否則 401；留空代表不設限（僅應在內網曝露）。
        """
        from saas_mvp.obs import REGISTRY
        from saas_mvp.obs.metrics import CONTENT_TYPE

        if not settings.metrics_enabled:
            return JSONResponse({"detail": "metrics disabled"}, status_code=404)

        token = settings.metrics_token
        if token:
            auth_header = request.headers.get("authorization", "")
            expected = f"Bearer {token}"
            # 定長比較避免時序側信道
            import hmac

            if not hmac.compare_digest(auth_header, expected):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)

        # 業務 gauges（cancel_failed 訂閱數、卡住的 webhook pending 等）於
        # scrape 當下即時查 DB；REGISTRY 是 per-process，cron 行程無法曝露，
        # 故收在端點內。collect 失敗絕不可毀掉 /metrics（HTTP 指標仍要能刮）。
        try:
            from saas_mvp.db import SessionLocal
            from saas_mvp.obs.business import collect_business_gauges

            with SessionLocal() as _db:
                collect_business_gauges(_db)
        except Exception:  # noqa: BLE001
            pass

        return PlainTextResponse(REGISTRY.render(), media_type=CONTENT_TYPE)

    return app


app = create_app()
