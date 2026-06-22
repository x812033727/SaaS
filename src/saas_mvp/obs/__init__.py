"""可觀測性 (observability) 層：結構化日誌、request-id 串接、Prometheus 指標。

對外公開 API：
- ``configure_logging()``：依 settings 設定 root logger（JSON / text）。
- ``ObservabilityMiddleware``：ASGI middleware，掛上即得 request-id + 存取日誌 + 指標。
- ``REGISTRY`` / ``render``：Prometheus 計量器（``/metrics`` 用）。
- ``get_request_id()``：取目前請求的 request-id（跨模組串接 log 用）。
"""

from saas_mvp.obs.context import get_request_id, request_id_var
from saas_mvp.obs.errors import install_error_handlers
from saas_mvp.obs.logging import configure_logging
from saas_mvp.obs.metrics import CONTENT_TYPE, REGISTRY
from saas_mvp.obs.middleware import ObservabilityMiddleware

__all__ = [
    "configure_logging",
    "ObservabilityMiddleware",
    "install_error_handlers",
    "REGISTRY",
    "CONTENT_TYPE",
    "get_request_id",
    "request_id_var",
]
