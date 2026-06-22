"""多 worker / 橫向擴展驗收測試：可插拔限流後端。

驗收標準
--------
1. 兩個 SlidingWindowRateLimiter 指向**同一個** backend 實例時，計數共享
   （模擬兩個 worker process）——核心 multi-worker 性質。
2. _make_backend() 預設回 MemoryBackend；backend=redis 但 url 空 / redis 未裝
   時 fallback 回 MemoryBackend（不崩潰）。
3. 既有 SlidingWindowRateLimiter(...)._check_rate_limit(...) 行為不變（parity）。
4. RedisBackend 對 fakeredis 的原子滑動視窗（僅在 fakeredis 可用時跑）。
5. GET /healthz 回 200 + 預期 keys。

全部離線、無 time.sleep、不需真實 Redis。
"""

from __future__ import annotations

import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from saas_mvp.auth.ratelimit import (
    SlidingWindowRateLimiter,
    _make_backend,
)
from saas_mvp.auth.ratelimit_backend import (
    MemoryBackend,
    RateLimitBackend,
)
from saas_mvp.app import create_app


# ══════════════════════════════════════════════════════════════════════════════
# 1. 跨 worker 共享：共用後端 → 兩個限制器共享計數
# ══════════════════════════════════════════════════════════════════════════════

class FakeSharedBackend(RateLimitBackend):
    """以「共享 dict」模擬跨 process 共享的限流後端（如 Redis）。

    用一把 lock + 單調計數器當「邏輯時鐘」，做純滑動視窗計數；重點不在時鐘
    精度，而在於：**同一個實例**被多個 SlidingWindowRateLimiter 共用時，計數
    必須累加在一起——這正是 multi-worker 共享後端的本質。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, list[int]] = {}
        self._tick = 0

    def allow(self, key: str, max_calls: int, window_seconds: int) -> bool:
        with self._lock:
            self._tick += 1
            now = self._tick
            cutoff = now - window_seconds
            calls = [t for t in self._store.get(key, []) if t > cutoff]
            if len(calls) >= max_calls:
                self._store[key] = calls
                return False
            calls.append(now)
            self._store[key] = calls
            return True


def test_two_limiters_sharing_backend_share_the_limit():
    """模擬兩個 worker：limiter_a / limiter_b 指向同一 backend，計數合併。

    max_calls=2：經 A 一次 + 經 B 一次後額度用罄，第三次（無論走 A 或 B）→ 429。
    這證明「經由限制器 A 的呼叫會計入限制器 B」的核心 multi-worker 性質。
    """
    backend = FakeSharedBackend()
    # window 很大，確保不過期；namespace 相同代表同一邏輯資源（per-IP/per-key）
    limiter_a = SlidingWindowRateLimiter(
        max_calls=2, window_seconds=10_000, backend=backend, namespace="ip"
    )
    limiter_b = SlidingWindowRateLimiter(
        max_calls=2, window_seconds=10_000, backend=backend, namespace="ip"
    )

    # worker A 放行一次
    limiter_a._check_rate_limit("1.2.3.4")
    # worker B 放行一次（共享後端 → 計數累加到 2）
    limiter_b._check_rate_limit("1.2.3.4")

    # 第三次：無論哪個 worker 都應超限
    with pytest.raises(HTTPException) as exc_a:
        limiter_a._check_rate_limit("1.2.3.4")
    assert exc_a.value.status_code == 429
    assert exc_a.value.headers["Retry-After"] == "10000"

    with pytest.raises(HTTPException):
        limiter_b._check_rate_limit("1.2.3.4")


def test_shared_backend_namespaces_isolate_keys():
    """不同 namespace（不同限制器種類）即使共用 backend，也不互相計數。"""
    backend = FakeSharedBackend()
    reg = SlidingWindowRateLimiter(
        max_calls=1, window_seconds=10_000, backend=backend, namespace="register"
    )
    tok = SlidingWindowRateLimiter(
        max_calls=1, window_seconds=10_000, backend=backend, namespace="token"
    )

    reg._check_rate_limit("ip")          # register 用罄
    tok._check_rate_limit("ip")          # token 獨立 namespace，仍可放行
    with pytest.raises(HTTPException):
        reg._check_rate_limit("ip")      # register 超限


def test_shared_backend_distinct_identifiers_independent():
    """同一限制器、同一 backend，不同 identifier（IP/key）彼此獨立計數。"""
    backend = FakeSharedBackend()
    lim = SlidingWindowRateLimiter(
        max_calls=1, window_seconds=10_000, backend=backend, namespace="ip"
    )
    lim._check_rate_limit("a")
    lim._check_rate_limit("b")  # 不同 identifier，獨立
    with pytest.raises(HTTPException):
        lim._check_rate_limit("a")


# ══════════════════════════════════════════════════════════════════════════════
# 2. _make_backend() 預設 + fallback
# ══════════════════════════════════════════════════════════════════════════════

def test_make_backend_defaults_to_memory(monkeypatch):
    from saas_mvp.config import settings

    monkeypatch.setattr(settings, "rate_limit_backend", "memory", raising=False)
    backend = _make_backend()
    assert isinstance(backend, MemoryBackend)


def test_make_backend_redis_empty_url_falls_back_to_memory(monkeypatch):
    """backend=redis 但 SAAS_REDIS_URL 空 → fallback MemoryBackend（不崩潰）。"""
    from saas_mvp.config import settings

    monkeypatch.setattr(settings, "rate_limit_backend", "redis", raising=False)
    monkeypatch.setattr(settings, "redis_url", "", raising=False)
    backend = _make_backend()
    assert isinstance(backend, MemoryBackend)


def test_make_backend_redis_unreachable_falls_back_to_memory(monkeypatch):
    """backend=redis、url 指向不可達位址 → fallback MemoryBackend（不拋例外）。"""
    from saas_mvp.config import settings

    monkeypatch.setattr(settings, "rate_limit_backend", "redis", raising=False)
    # 127.0.0.1:1（保留 port，連線必失敗）
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0", raising=False)
    backend = _make_backend()
    assert isinstance(backend, MemoryBackend)


def test_make_backend_redis_not_installed_falls_back(monkeypatch):
    """模擬 redis 套件未安裝 → import 失敗 → fallback MemoryBackend。"""
    import builtins

    from saas_mvp.config import settings

    monkeypatch.setattr(settings, "rate_limit_backend", "redis", raising=False)
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0", raising=False)

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "redis":
            raise ImportError("No module named 'redis'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    backend = _make_backend()
    assert isinstance(backend, MemoryBackend)


# ══════════════════════════════════════════════════════════════════════════════
# 3. 既有 in-memory 行為 parity（backend=None 路徑不變）
# ══════════════════════════════════════════════════════════════════════════════

class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def test_inmemory_path_unchanged_parity():
    """無 backend（預設）時，滑動視窗行為與舊版逐字一致：N 次後 429、視窗過期重置。"""
    clock = _FakeClock(start=1000.0)
    lim = SlidingWindowRateLimiter(max_calls=2, window_seconds=60, clock=clock)

    lim._check_rate_limit("u")
    lim._check_rate_limit("u")
    with pytest.raises(HTTPException) as exc:
        lim._check_rate_limit("u")
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"

    clock.advance(61)
    lim._check_rate_limit("u")  # 視窗過期 → 重置，不應拋


def test_memory_backend_matches_inmemory_path():
    """MemoryBackend.allow 與舊 in-process 路徑語意一致（共用同一假時鐘）。"""
    clock = _FakeClock()
    backend = MemoryBackend(clock=clock)
    assert backend.allow("k", max_calls=1, window_seconds=60) is True
    assert backend.allow("k", max_calls=1, window_seconds=60) is False  # 超限
    clock.advance(61)
    assert backend.allow("k", max_calls=1, window_seconds=60) is True   # 重置


# ══════════════════════════════════════════════════════════════════════════════
# 4. RedisBackend 對 fakeredis（僅在 fakeredis 可用時）
# ══════════════════════════════════════════════════════════════════════════════

def test_redis_backend_atomic_sliding_window_with_fakeredis():
    """以 fakeredis 驗證 RedisBackend 的 Lua 原子滑動視窗。

    僅當 fakeredis 套件可用時執行；否則 skip（不把 redis/fakeredis 列為硬依賴，
    共享後端模擬測試已涵蓋核心 multi-worker 性質）。
    """
    fakeredis = pytest.importorskip("fakeredis")
    pytest.importorskip("redis")
    # RedisBackend 走 Lua（EVALSHA）；fakeredis 需 lua 引擎（lupa）才支援腳本。
    # 無 lua 時乾淨 skip 而非以 ResponseError 報錯。
    pytest.importorskip("lupa")

    from saas_mvp.auth.ratelimit_backend import RedisBackend

    client = fakeredis.FakeStrictRedis()
    backend = RedisBackend(client)

    # max_calls=3：前三次放行，第四次超限
    assert backend.allow("k", 3, 60) is True
    assert backend.allow("k", 3, 60) is True
    assert backend.allow("k", 3, 60) is True
    assert backend.allow("k", 3, 60) is False

    # 不同 key 獨立
    assert backend.allow("other", 3, 60) is True


# ══════════════════════════════════════════════════════════════════════════════
# 5. /healthz 探針
# ══════════════════════════════════════════════════════════════════════════════

def test_healthz_returns_expected_keys():
    client = TestClient(create_app())
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert "rate_limit_backend" in body


def test_root_contract_unchanged():
    """新增 /healthz 不得破壞既有 / root 契約。"""
    client = TestClient(create_app())
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "saas-mvp"
    assert body["status"] == "ok"
