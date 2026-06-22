"""結構化日誌設定。

- ``SAAS_LOG_FORMAT=json``：每行一筆 JSON（給 log 聚合器 / Loki / CloudWatch）。
- ``SAAS_LOG_FORMAT=text``（預設）：人類可讀單行（dev / 本機）。
- ``SAAS_LOG_LEVEL``：root logger 等級（預設 INFO）。

兩種格式都會自動附上目前請求的 ``request_id``（從 contextvar 取得），
讓同一請求跨模組的所有 log 可被串接。``configure_logging()`` 冪等，
重複呼叫只會替換 handler，不會疊加。
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from saas_mvp.obs.context import get_request_id

# JSON 不需重複輸出的標準 LogRecord 屬性（其餘 extra=... 欄位才會被收進 JSON）。
_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)


class _RequestIdFilter(logging.Filter):
    """把目前 contextvar 的 request_id 注入每筆 LogRecord。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True


class JsonFormatter(logging.Formatter):
    """單行 JSON formatter；含 timestamp / level / logger / message / request_id
    以及任何透過 ``logger.info(..., extra={...})`` 帶入的欄位。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            # filter 通常已注入 record.request_id；缺漏時回退到 contextvar。
            "request_id": getattr(record, "request_id", None) or get_request_id(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # 收進使用者以 extra=... 帶入的自訂欄位（如 method/path/status/duration_ms）
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """人類可讀單行；有 request_id 時以 ``[rid]`` 前綴顯示。"""

    _FMT = "%(asctime)s %(levelname)-7s %(name)s%(rid)s %(message)s"

    def format(self, record: logging.LogRecord) -> str:
        rid = getattr(record, "request_id", None) or get_request_id()
        record.rid = f" [{rid}]" if rid else ""
        return logging.Formatter(self._FMT).format(record)


def configure_logging(log_format: str | None = None, level: str | None = None) -> None:
    """設定 root logger 的 handler / formatter（冪等）。

    參數留空時讀 settings（``SAAS_LOG_FORMAT`` / ``SAAS_LOG_LEVEL``）。
    """
    from saas_mvp.config import settings

    fmt = (log_format or settings.log_format or "text").lower()
    lvl_name = (level or settings.log_level or "INFO").upper()
    lvl = getattr(logging, lvl_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())

    root = logging.getLogger()
    root.setLevel(lvl)
    # 冪等：移除先前由本函式安裝的 handler，避免重複輸出
    for h in list(root.handlers):
        if getattr(h, "_saas_obs", False):
            root.removeHandler(h)
    handler._saas_obs = True  # type: ignore[attr-defined]
    root.addHandler(handler)
