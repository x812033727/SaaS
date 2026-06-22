"""可觀測性 ASGI middleware：request-id + 存取日誌 + HTTP 指標。

刻意用純 ASGI middleware（非 Starlette ``BaseHTTPMiddleware``），避免後者
與 streaming / background task 的已知互動問題，且能在 inner app 路由完成後
讀到 ``scope["route"]`` 取得「路由樣板」(如 ``/s/{token}``) 當指標 label，
避免以原始路徑當 label 造成 cardinality 爆炸。

對每個 HTTP 請求：
1. 取用戶端帶入的 request-id（``X-Request-ID``）或新生一個，存入 contextvar，
   並回寫到回應 header，讓呼叫端可串接。
2. 計時並擷取最終 status code。
3. 記一行結構化存取日誌（method/path/status/duration_ms/request_id）。
4. 更新 Prometheus 指標（總數 counter / 延遲 histogram / in-progress gauge）。
"""

from __future__ import annotations

import logging
import secrets
import time

from saas_mvp.obs import metrics
from saas_mvp.obs.context import request_id_var

_access_log = logging.getLogger("saas_mvp.access")

_REQUEST_ID_HEADER = b"x-request-id"
_MAX_CLIENT_RID_LEN = 128  # 防止用戶端塞超長字串污染 log / header


def _new_request_id() -> str:
    return secrets.token_hex(8)  # 16 hex chars，足夠唯一且簡短


def _client_request_id(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name.lower() == _REQUEST_ID_HEADER:
            rid = value.decode("latin-1").strip()
            # 只接受合理長度的可列印 token，否則忽略改用新生
            if rid and len(rid) <= _MAX_CLIENT_RID_LEN and rid.isprintable():
                return rid
            return None
    return None


def _route_template(scope) -> str:
    """取路由樣板當 label（控制 cardinality）；未匹配則 "unmatched"。"""
    route = scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    return "unmatched"


class ObservabilityMiddleware:
    def __init__(self, app, *, metrics_enabled: bool = True) -> None:
        self.app = app
        self.metrics_enabled = metrics_enabled

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        rid = _client_request_id(headers) or _new_request_id()
        token = request_id_var.set(rid)
        # 同步寫進 scope：集中式例外 handler 在本 middleware 之外（ServerErrorMiddleware
        # 最外層）執行，屆時 contextvar 可能已 reset，改從 scope 取 request_id。
        scope["saas_request_id"] = rid

        method = scope.get("method", "GET")
        rid_bytes = rid.encode("latin-1")
        status_holder = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
                # 把 request-id 加進回應 header（去重）
                resp_headers = [
                    (k, v) for (k, v) in message.get("headers", [])
                    if k.lower() != _REQUEST_ID_HEADER
                ]
                resp_headers.append((_REQUEST_ID_HEADER, rid_bytes))
                message = {**message, "headers": resp_headers}
            await send(message)

        if self.metrics_enabled:
            metrics.REGISTRY.inc_gauge(
                metrics.HTTP_IN_PROGRESS, 1.0,
                help_text="In-flight HTTP requests being processed.",
            )
        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # 例外仍要計量/記錄，再往外拋給 app 的 exception handler
            duration = time.perf_counter() - start
            self._record(scope, method, 500, duration, exc=True)
            if self.metrics_enabled:
                metrics.REGISTRY.inc_gauge(metrics.HTTP_IN_PROGRESS, -1.0)
            request_id_var.reset(token)
            raise
        else:
            duration = time.perf_counter() - start
            self._record(scope, method, status_holder["code"], duration, exc=False)
            if self.metrics_enabled:
                metrics.REGISTRY.inc_gauge(metrics.HTTP_IN_PROGRESS, -1.0)
            request_id_var.reset(token)

    def _record(self, scope, method: str, status: int, duration: float, *, exc: bool) -> None:
        path_tmpl = _route_template(scope)
        if self.metrics_enabled:
            metrics.REGISTRY.inc_counter(
                metrics.HTTP_REQUESTS_TOTAL,
                {"method": method, "path": path_tmpl, "status": str(status)},
                help_text="Total HTTP requests by method, route template and status.",
            )
            metrics.REGISTRY.observe(
                metrics.HTTP_REQUEST_DURATION, duration,
                {"method": method, "path": path_tmpl},
                help_text="HTTP request latency in seconds by method and route template.",
            )
        level = logging.ERROR if exc or status >= 500 else logging.INFO
        _access_log.log(
            level, "%s %s %s %.1fms", method, scope.get("path", ""), status,
            duration * 1000.0,
            extra={
                "method": method,
                "path": scope.get("path", ""),
                "route": path_tmpl,
                "status": status,
                "duration_ms": round(duration * 1000.0, 1),
            },
        )
