"""Per-request context (request id) propagated via contextvars.

contextvar 在 async / 多 worker 下每個請求各自獨立，不會互相污染；
log formatter 與 middleware 都從這裡讀取目前請求的 request-id。
"""

from __future__ import annotations

import contextvars

# 目前請求的 request-id；無請求脈絡時為 ""（例如 ops 腳本、啟動期）。
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "saas_request_id", default=""
)


def get_request_id() -> str:
    """回傳目前脈絡的 request-id（無則空字串）。"""
    return request_id_var.get()


def set_request_id(value: str) -> contextvars.Token:
    """設定目前脈絡的 request-id，回傳 token 供之後 reset。"""
    return request_id_var.set(value)


def reset_request_id(token: contextvars.Token) -> None:
    """還原 request-id 至 set 之前的狀態（避免脈絡外洩）。"""
    request_id_var.reset(token)
