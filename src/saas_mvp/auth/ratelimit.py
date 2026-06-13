"""速率限制模組：IP-based（auth 端點）+ per-key/per-tenant（業務端點）。

NOTE: Do NOT add `from __future__ import annotations` here.
  That turns all type hints into strings; FastAPI cannot then identify
  `Request` as the special HTTP-request object and falls back to treating
  it as a required query parameter (422 on every call).

NOTE: 單 worker 限定 — in-memory 結構不跨 process 共享，多 worker 需改 Redis 後端。
"""

import threading
import time
from collections import OrderedDict
from typing import Callable, List, Tuple

from fastapi import Depends, HTTPException, Request, status

from saas_mvp.config import settings


# ── 有界時間戳日誌（純 stdlib，無外部依賴）──────────────────────────────────

class _BoundedTimestampLog:
    """以 OrderedDict 實作的有界 key → timestamps 映射。

    超出 maxsize 時 FIFO evict 最舊 key，防止 DoS 導致無限記憶體增長。
    不使用 defaultdict，改提供 get/set 明確介面。
    """

    def __init__(self, maxsize: int = 10_000) -> None:
        self._maxsize = maxsize
        self._data: "OrderedDict[str, List[float]]" = OrderedDict()

    def get(self, key: str) -> "List[float]":
        """回傳 key 對應的時間戳列表；key 不存在回空列表。"""
        return self._data.get(key, [])

    def set(self, key: str, value: "List[float]") -> None:
        """設定 key 的值；若 key 已存在則移至末尾（LRU 更新）；
        若 key 不存在且容量已滿，則 FIFO evict 最舊 key。"""
        if key in self._data:
            self._data.move_to_end(key)
        elif len(self._data) >= self._maxsize:
            self._data.popitem(last=False)  # 刪最舊
        self._data[key] = value

    def clear(self) -> None:
        """清除所有 entry（測試用，相容 defaultdict.clear()）。"""
        self._data.clear()


# ── 工具函式 ──────────────────────────────────────────────────────────────────

def _parse_rate(rate_str: str, default_calls: int, default_window: int) -> Tuple[int, int]:
    """解析 '{calls}/{window_seconds}' 格式；失敗或非正值均回預設值。

    防護：calls <= 0 或 window <= 0 視為無效，回 default，避免
    SAAS_KEY_RATE_LIMIT=0/60 把 max_calls 設為 0 而全面拒絕請求。
    """
    try:
        calls_str, window_str = rate_str.split("/", 1)
        calls = int(calls_str.strip())
        window = int(window_str.strip())
        if calls <= 0 or window <= 0:
            return default_calls, default_window
        return calls, window
    except (ValueError, AttributeError):
        return default_calls, default_window


# ── 核心速率限制器 ────────────────────────────────────────────────────────────

class SlidingWindowRateLimiter:
    """滑動視窗速率限制（執行緒安全，in-process）。

    - `clock` 可注入假時鐘，讓測試無需 time.sleep。
    - 預設 `clock=time.monotonic`，產線行為不變。
    - `_log` 使用 `_BoundedTimestampLog(maxsize=10_000)`，防止 DoS 導致
      無限記憶體增長（FIFO eviction）。
    """

    def __init__(
        self,
        max_calls: int,
        window_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        maxsize: int = 10_000,
    ) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._log = _BoundedTimestampLog(maxsize=maxsize)

    @property
    def window_seconds(self) -> int:
        return self._window

    def _check_rate_limit(self, identifier: str) -> None:
        """核心滑動視窗檢查；超限拋 429 帶 Retry-After header。

        Retry-After 值為完整視窗長度（保守但合規）。
        精確最短等待時間的計算方式見 KNOWN_LIMITATIONS.md。
        """
        now = self._clock()
        cutoff = now - self._window

        with self._lock:
            calls = [t for t in self._log.get(identifier) if t > cutoff]
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
            self._log.set(identifier, calls)

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

# 在 auth subpackage 內部 import 同 subpackage 的 dependencies 模組——
# auth/dependencies.py 不依賴 ratelimit.py，故無循環依賴。
from saas_mvp.auth.dependencies import Actor, get_current_actor  # noqa: E402


class BusinessRateLimiter:
    """業務端點速率限制：per-API-key 優先，否則 per-tenant。

    用法（router 層）::

        router = APIRouter(dependencies=[Depends(require_rate_limit)])

    測試時以 ``app.dependency_overrides[require_rate_limit]`` 替換為
    帶假時鐘的實例（_AlwaysOnRateLimiter 或 BusinessRateLimiter），無需 time.sleep。
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
