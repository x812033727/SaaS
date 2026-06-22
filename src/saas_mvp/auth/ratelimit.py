"""速率限制模組：IP-based（auth 端點）+ per-key/per-tenant（業務端點）。

NOTE: Do NOT add `from __future__ import annotations` here.
  That turns all type hints into strings; FastAPI cannot then identify
  `Request` as the special HTTP-request object and falls back to treating
  it as a required query parameter (422 on every call).

NOTE: 多 worker / 橫向擴展 — in-memory 結構不跨 process 共享。設定
  SAAS_RATE_LIMIT_BACKEND=redis + SAAS_REDIS_URL 即改用 Redis 後端，跨 worker
  共享同一份滑動視窗計數（見 ratelimit_backend.py）。預設仍為 in-memory（dev/
  test/單 worker），公開 API 與行為與舊版完全一致。
"""

import logging
import threading
import time
from typing import Callable, Optional, Tuple

from fastapi import Depends, HTTPException, Request, status

from saas_mvp.config import settings
from saas_mvp.auth.ratelimit_backend import (
    MemoryBackend,
    RateLimitBackend,
    _BoundedTimestampLog,  # re-export：保留既有公開名稱（測試直接 import）
)

_log = logging.getLogger(__name__)

# 公開符號（測試與其他模組依賴的名稱）
__all__ = [
    "_BoundedTimestampLog",
    "_parse_rate",
    "_make_backend",
    "SlidingWindowRateLimiter",
    "BusinessRateLimiter",
    "RateLimitBackend",
    "register_limiter",
    "token_limiter",
    "public_limiter",
    "require_rate_limit",
]


def _make_backend() -> RateLimitBackend:
    """依設定回傳限流後端。

    - ``rate_limit_backend != "redis"`` → :class:`MemoryBackend`（預設）。
    - ``== "redis"`` → 嘗試以 ``redis.from_url(settings.redis_url)`` 建
      :class:`RedisBackend`；若 ``redis`` 套件缺失 / url 空 / 連線失敗，記
      warning 並 **fallback 回 MemoryBackend**（誤設定不得讓服務啟動崩潰）。
    """
    if settings.rate_limit_backend != "redis":
        return MemoryBackend()

    from saas_mvp.auth.ratelimit_backend import RedisBackend

    try:
        if not settings.redis_url:
            raise ValueError("SAAS_REDIS_URL is empty")
        import redis  # lazy：未裝 redis 套件不影響預設路徑

        client = redis.from_url(settings.redis_url)
        client.ping()  # 主動驗證連線；連不上即 fallback（不讓啟動崩潰）
        backend = RedisBackend(client)
        _log.info("rate-limit backend: redis (%s)", settings.redis_url)
        return backend
    except Exception as exc:  # noqa: BLE001 — 任何 redis 故障都 fallback，不崩啟動
        _log.warning(
            "rate-limit backend=redis requested but unavailable (%s); "
            "falling back to in-memory backend (NOT shared across workers)",
            type(exc).__name__,
        )
        return MemoryBackend()


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
    """滑動視窗速率限制。

    - `clock` 可注入假時鐘，讓測試無需 time.sleep。
    - 預設 `clock=time.monotonic`，產線行為不變。
    - 預設 `_log` 使用 `_BoundedTimestampLog(maxsize=10_000)`，防止 DoS 導致
      無限記憶體增長（FIFO eviction）。

    多 worker 後端（選用）：
    - `backend=None`（預設）→ 走既有 in-process 路徑，行為與舊版逐字一致。
    - `backend=RateLimitBackend` → 把判定委派給該後端（例 RedisBackend，跨
      worker 共享）。`namespace` 用來區隔不同限制器的 key 命名空間，避免
      Redis 中跨限制器 key 撞鍵（in-memory 路徑因每實例獨立 `_log` 自然隔離）。
    """

    def __init__(
        self,
        max_calls: int,
        window_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        maxsize: int = 10_000,
        backend: Optional[RateLimitBackend] = None,
        namespace: str = "",
    ) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._log = _BoundedTimestampLog(maxsize=maxsize)
        self._backend = backend
        self._namespace = namespace

    @property
    def window_seconds(self) -> int:
        return self._window

    def _raise_429(self) -> None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit exceeded: max {self._max_calls} requests "
                f"per {self._window}s. Please try again later."
            ),
            headers={"Retry-After": str(self._window)},
        )

    def _check_rate_limit(self, identifier: str) -> None:
        """核心滑動視窗檢查；超限拋 429 帶 Retry-After header。

        Retry-After 值為完整視窗長度（保守但合規）。
        精確最短等待時間的計算方式見 KNOWN_LIMITATIONS.md。

        若設定了 `backend`，判定委派給後端（跨 worker 共享）；否則走既有
        in-process 路徑（行為不變）。
        """
        if self._backend is not None:
            key = f"{self._namespace}:{identifier}" if self._namespace else identifier
            if not self._backend.allow(key, self._max_calls, self._window):
                self._raise_429()
            return

        now = self._clock()
        cutoff = now - self._window

        with self._lock:
            calls = [t for t in self._log.get(identifier) if t > cutoff]
            if len(calls) >= self._max_calls:
                self._raise_429()
            calls.append(now)
            self._log.set(identifier, calls)

    def __call__(self, request: Request) -> None:
        """FastAPI dependency：IP-based rate limit（用於 /auth/register、/auth/token）。"""
        if not settings.rate_limit_enabled:
            return
        ip: str = request.client.host if request.client else "unknown"
        self._check_rate_limit(ip)


# 共用後端：依 SAAS_RATE_LIMIT_BACKEND 選 memory（預設）/ redis。
# 所有 module-level 限制器共用同一個後端實例，redis 模式下即跨 worker 共享；
# 各限制器以不同 namespace 區隔 key，避免跨限制器撞鍵。
_backend = _make_backend()


def effective_backend_name() -> str:
    """回傳**實際生效**的限流後端名稱（"memory" / "redis"）。

    與 ``settings.rate_limit_backend``（設定值）不同：設定 redis 但因套件缺失/
    連線失敗而降級時，此處回 "memory"，讓 /healthz 反映真實狀態。
    """
    return getattr(_backend, "name", "unknown")


# ── Auth endpoint limiters (IP-based，module-level singleton) ─────────────────
# 20/min: generous for dev/test, still protective in production.
register_limiter = SlidingWindowRateLimiter(
    max_calls=20, window_seconds=60, backend=_backend, namespace="register"
)
token_limiter = SlidingWindowRateLimiter(
    max_calls=20, window_seconds=60, backend=_backend, namespace="token"
)

# 公開 / 無認證端點 IP 限制器（/p/{slug}、calendar .ics feeds、/pii/{token}）：
# 60/min per IP — 防止匿名 slug / token 列舉枚舉，對正常瀏覽足夠寬鬆。
# 與其他 IP 限制器一樣由 settings.rate_limit_enabled 控制（測試預設關閉）。
public_limiter = SlidingWindowRateLimiter(
    max_calls=60, window_seconds=60, backend=_backend, namespace="public"
)


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
    key_limiter=SlidingWindowRateLimiter(
        max_calls=_key_calls, window_seconds=_key_window,
        backend=_backend, namespace="key",
    ),
    tenant_limiter=SlidingWindowRateLimiter(
        max_calls=_tenant_calls, window_seconds=_tenant_window,
        backend=_backend, namespace="tenant",
    ),
)
