"""速率限制模組：IP-based（auth 端點）+ per-key/per-tenant（業務端點）。

NOTE: Do NOT add `from __future__ import annotations` here.
  That turns all type hints into strings; FastAPI cannot then identify
  `Request` as the special HTTP-request object and falls back to treating
  it as a required query parameter (422 on every call).

NOTE: 單 worker 限定 — in-memory dict 不跨 process 共享，多 worker 需改 Redis 後端。
"""

import threading
import time
from collections import defaultdict
from typing import Callable, Optional, Tuple

from fastapi import Depends, HTTPException, Request, status

from saas_mvp.config import settings


# ── 工具函式 ──────────────────────────────────────────────────────────────────

def _parse_rate(rate_str: str, default_calls: int, default_window: int) -> Tuple[int, int]:
    """解析 '{calls}/{window_seconds}' 格式；失敗回預設值。"""
    try:
        calls_str, window_str = rate_str.split("/", 1)
        return int(calls_str.strip()), int(window_str.strip())
    except (ValueError, AttributeError):
        return default_calls, default_window


# ── 核心速率限制器 ────────────────────────────────────────────────────────────

class SlidingWindowRateLimiter:
    """滑動視窗速率限制（執行緒安全，in-process）。

    - `clock` 可注入假時鐘，讓測試無需 time.sleep。
    - 預設 `clock=time.monotonic`，產線行為不變。
    """

    def __init__(
        self,
        max_calls: int,
        window_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # TODO: replace with LRU(maxsize=10_000) before multi-tenant scale
        self._log: dict = defaultdict(list)   # {identifier: [monotonic_ts, ...]}

    @property
    def window_seconds(self) -> int:
        return self._window

    def _check_rate_limit(self, identifier: str) -> None:
        """核心滑動視窗檢查；超限拋 429 帶 Retry-After header。"""
        now = self._clock()
        cutoff = now - self._window

        with self._lock:
            calls = [t for t in self._log[identifier] if t > cutoff]
            if len(calls) >= self._max_calls:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        f"Rate limit exceeded: max {self._max_calls} requests "
                        f"per {self._window}s. Please try again later."
                    ),
                    headers={"Retry-After": str(self._window)},
                )
            calls.append(now)
            self._log[identifier] = calls

    def __call__(self, request: Request) -> None:
        """FastAPI dependency：IP-based rate limit（用於 /auth/register、/auth/token）。"""
        if not settings.rate_limit_enabled:
            return
        ip: str = request.client.host if request.client else "unknown"
        self._check_rate_limit(ip)


# ── Auth endpoint limiters (IP-based，module-level singleton) ─────────────────
# 20/min: generous for dev/test, still protective in production.
register_limiter = SlidingWindowRateLimiter(max_calls=20, window_seconds=60)
token_limiter = SlidingWindowRateLimiter(max_calls=20, window_seconds=60)


# ── Business endpoint limiter (per-key / per-tenant) ─────────────────────────

# 延遲 import 避免頂層循環（auth/dependencies 不依賴 ratelimit）
from saas_mvp.auth.dependencies import Actor, get_current_actor  # noqa: E402


class BusinessRateLimiter:
    """業務端點速率限制：per-API-key 優先，否則 per-tenant。

    用法（router 層）::

        router = APIRouter(dependencies=[Depends(require_rate_limit)])

    測試時用 ``app.dependency_overrides[require_rate_limit]`` 替換為
    帶假時鐘的 ``BusinessRateLimiter`` 實例，無需 time.sleep。
    """

    def __init__(
        self,
        key_limiter: SlidingWindowRateLimiter,
        tenant_limiter: SlidingWindowRateLimiter,
    ) -> None:
        self._key_lim = key_limiter
        self._tenant_lim = tenant_limiter

    def __call__(
        self,
        actor: Actor = Depends(get_current_actor),
    ) -> None:
        """FastAPI dependency：根據 actor 選擇 per-key 或 per-tenant 限制。"""
        if not settings.rate_limit_enabled:
            return
        if actor.api_key_id is not None:
            self._key_lim._check_rate_limit(f"key:{actor.api_key_id}")
        else:
            self._tenant_lim._check_rate_limit(f"tenant:{actor.user.tenant_id}")


# Module-level singleton — 解析 SAAS_KEY_RATE_LIMIT / SAAS_TENANT_RATE_LIMIT
_key_calls, _key_window = _parse_rate(settings.key_rate_limit, 100, 60)
_tenant_calls, _tenant_window = _parse_rate(settings.tenant_rate_limit, 1000, 60)

require_rate_limit = BusinessRateLimiter(
    key_limiter=SlidingWindowRateLimiter(max_calls=_key_calls, window_seconds=_key_window),
    tenant_limiter=SlidingWindowRateLimiter(max_calls=_tenant_calls, window_seconds=_tenant_window),
)
