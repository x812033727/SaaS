"""集中式未捕捉例外處理：統一 JSON error envelope + 錯誤追蹤。

未被任何路由 / 既有 handler 處理的例外，原本只會變成 Starlette 預設的
純文字 ``Internal Server Error`` 500。這裡掛一個 app 層 ``Exception`` handler：

- 回傳**一致的 JSON envelope**：``{"error": {type, message, request_id}}``，
  讓前端 / 客服能用 request_id 對帳同一筆請求的伺服器日誌。
- **不外洩**內部例外訊息 / traceback 給用戶端（只回通用訊息）；
  完整 traceback 連同 request_id 記在伺服器端 ERROR 日誌。
- 計入 Prometheus counter ``http_unhandled_exceptions_total{type=...}``。

注意：Starlette 的 ``ServerErrorMiddleware`` 位於中介層最外層，呼叫本 handler
後仍會**重新拋出**例外（供 ASGI server 記錄 / 測試的 raise_server_exceptions），
故既有測試行為不變；本 handler 也因此在 ObservabilityMiddleware 之外執行，
request_id 改從 ``scope`` 取（contextvar 此時可能已被 reset），回應 header
亦由本 handler 自行補上 ``X-Request-ID``。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from saas_mvp.obs import metrics
from saas_mvp.obs.context import get_request_id

_log = logging.getLogger("saas_mvp.error")

HTTP_UNHANDLED_EXCEPTIONS = "http_unhandled_exceptions_total"

# ObservabilityMiddleware 寫進 scope 的 key（contextvar reset 後仍可取得）。
SCOPE_REQUEST_ID = "saas_request_id"


def _request_id(request: Request) -> str:
    return request.scope.get(SCOPE_REQUEST_ID) or get_request_id()


def install_error_handlers(app: FastAPI) -> None:
    """在 app 上註冊集中式未捕捉例外 handler。"""

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception):  # noqa: ANN202
        rid = _request_id(request)
        exc_type = type(exc).__name__
        metrics.REGISTRY.inc_counter(
            HTTP_UNHANDLED_EXCEPTIONS,
            {"type": exc_type},
            help_text="Total unhandled exceptions converted to HTTP 500.",
        )
        # 完整 traceback 記伺服器端（含 request_id 供對帳），不回傳給用戶端。
        _log.error(
            "unhandled exception: %s",
            exc_type,
            exc_info=exc,
            extra={"request_id": rid, "exc_type": exc_type, "path": request.url.path},
        )
        body = {
            "error": {
                "type": "InternalServerError",
                "message": "伺服器發生未預期錯誤，請稍後再試或聯絡客服。",
                "request_id": rid,
            }
        }
        headers = {"X-Request-ID": rid} if rid else None
        return JSONResponse(body, status_code=500, headers=headers)


# ── F3/M2-003:遮罩後 traceback 摘要 ─────────────────────────────────────────

_TB_SENSITIVE_PATTERNS = ("Bearer ", "access_token", "channel_secret", "Authorization")


def safe_traceback(exc: BaseException, *, limit: int = 8, max_chars: int = 4000) -> str:
    """取例外 traceback 摘要供落 DB 診斷:最後 N 個 frame、不含 locals、
    截斷長度、遮罩已知敏感 pattern 所在行。永不拋錯。"""
    import traceback as _tb

    try:
        lines = _tb.format_exception(type(exc), exc, exc.__traceback__, limit=limit)
        out = []
        for ln in lines:
            if any(pat in ln for pat in _TB_SENSITIVE_PATTERNS):
                out.append("    [redacted line]\n")
            else:
                out.append(ln)
        return "".join(out)[:max_chars]
    except Exception:  # noqa: BLE001 — 診斷輔助不得拋錯
        return f"{type(exc).__name__} (traceback unavailable)"
