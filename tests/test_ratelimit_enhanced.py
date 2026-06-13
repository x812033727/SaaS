"""Task #3 驗收測試：強化速率限制

驗收標準
--------
1. SlidingWindowRateLimiter 接受可注入 clock；假時鐘推進可重現 429，無 sleep。
2. per-API-key：同一 key 在視窗內超過 N 次 → 429 + Retry-After header。
3. per-tenant（JWT）：同一 tenant 在視窗內超過 N 次 → 429 + Retry-After header。
4. 時窗過期後計數重置，請求恢復正常。
5. 不同 API key 各自獨立計數，不互相干擾。
6. rate_limit_enabled=false 時 BusinessRateLimiter 直接放行（不拋 429）。

全部離線，in-memory SQLite，無任何 time.sleep。
"""

from __future__ import annotations

import pytest
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku              # noqa: F401
from saas_mvp.app import create_app
from saas_mvp.auth.dependencies import Actor, get_current_actor
from saas_mvp.auth.ratelimit import (
    BusinessRateLimiter,
    SlidingWindowRateLimiter,
    require_rate_limit,
)
from saas_mvp.db import Base, get_db


# ── 假時鐘 ────────────────────────────────────────────────────────────────────

class FakeClock:
    """可手動遞增的假時鐘，讓速率限制測試無需 sleep。"""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ── 測試專用 BusinessRateLimiter（不檢查 settings.rate_limit_enabled）─────────

class _AlwaysOnRateLimiter:
    """測試用：無論 settings，一律執行速率限制（驗證假時鐘行為）。"""

    def __init__(
        self,
        key_limiter: SlidingWindowRateLimiter,
        tenant_limiter: SlidingWindowRateLimiter,
    ) -> None:
        self._key_lim = key_limiter
        self._tenant_lim = tenant_limiter

    def __call__(self, actor: Actor = Depends(get_current_actor)) -> None:
        if actor.api_key_id is not None:
            self._key_lim._check_rate_limit(f"key:{actor.api_key_id}")
        else:
            self._tenant_lim._check_rate_limit(f"tenant:{actor.user.tenant_id}")


# ── In-memory SQLite（module-scope，所有測試共用）────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _override_get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module")
def base_app():
    """基底 app（含 in-memory DB）；rate_limit override 各測試自行覆蓋。"""
    Base.metadata.create_all(bind=_engine)
    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db
    return app


@pytest.fixture()
def make_client(base_app):
    """接受 limiter，注入 dependency_overrides 並回傳 TestClient；測試後清理。"""
    created_clients = []

    def factory(limiter) -> TestClient:
        base_app.dependency_overrides[require_rate_limit] = limiter
        c = TestClient(base_app, raise_server_exceptions=True)
        created_clients.append(c)
        return c

    yield factory
    base_app.dependency_overrides.pop(require_rate_limit, None)


# ── 工具函式 ──────────────────────────────────────────────────────────────────

def _register(client: TestClient, email: str, pw: str, tenant: str) -> str:
    r = client.post("/auth/register", json={
        "email": email, "password": pw, "tenant_name": tenant,
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _create_api_key(client: TestClient, token: str, name: str) -> str:
    r = client.post(
        "/api-keys/",
        json={"name": name},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["plain_key"]


# ══════════════════════════════════════════════════════════════════════════════
# 1. 單元測試：SlidingWindowRateLimiter 假時鐘注入
# ══════════════════════════════════════════════════════════════════════════════

def test_sliding_window_clock_injection_no_sleep():
    """假時鐘推進 → 429；再推進超過視窗 → 計數重置，無 sleep。"""
    fake = FakeClock(start=1000.0)
    lim = SlidingWindowRateLimiter(max_calls=3, window_seconds=60, clock=fake)

    # 3 次在視窗內，均通過
    lim._check_rate_limit("u")
    fake.advance(1)
    lim._check_rate_limit("u")
    fake.advance(1)
    lim._check_rate_limit("u")

    # 第 4 次超限 → 429
    with pytest.raises(HTTPException) as exc:
        lim._check_rate_limit("u")
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"

    # 推進超過視窗 → 舊記錄全過期
    fake.advance(61)
    lim._check_rate_limit("u")  # 不應拋例外


def test_retry_after_equals_window_seconds():
    """Retry-After header 的值等於 window_seconds（整數字串）。"""
    fake = FakeClock()
    lim = SlidingWindowRateLimiter(max_calls=1, window_seconds=120, clock=fake)
    lim._check_rate_limit("u")  # 用完唯一額度

    with pytest.raises(HTTPException) as exc:
        lim._check_rate_limit("u")
    assert exc.value.headers["Retry-After"] == "120"


def test_different_identifiers_are_independent():
    """不同 identifier 的計數彼此獨立。"""
    fake = FakeClock()
    lim = SlidingWindowRateLimiter(max_calls=1, window_seconds=60, clock=fake)

    lim._check_rate_limit("id_a")   # id_a 用完
    lim._check_rate_limit("id_b")   # id_b 獨立，應通過

    with pytest.raises(HTTPException):
        lim._check_rate_limit("id_a")   # id_a 再用 → 超限


# ══════════════════════════════════════════════════════════════════════════════
# 2. 整合測試：per-API-key 速率限制
# ══════════════════════════════════════════════════════════════════════════════

def test_per_key_rate_limit_429(make_client):
    """API key 在視窗內超過 max_calls → 429 + Retry-After；視窗過期後恢復。"""
    fake = FakeClock()
    key_lim = SlidingWindowRateLimiter(max_calls=2, window_seconds=60, clock=fake)
    tenant_lim = SlidingWindowRateLimiter(max_calls=1000, window_seconds=60, clock=fake)
    client = make_client(_AlwaysOnRateLimiter(key_lim, tenant_lim))

    token = _register(client, "key1@rl-test.com", "password1", "RL-KeyTenant1")
    api_key = _create_api_key(client, token, "rl-key")
    hdrs = {"X-API-Key": api_key}

    # 第 1、2 次通過
    assert client.get("/notes/", headers=hdrs).status_code == 200
    assert client.get("/notes/", headers=hdrs).status_code == 200

    # 第 3 次超限
    r = client.get("/notes/", headers=hdrs)
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "60"

    # 推進時鐘過視窗，恢復
    fake.advance(61)
    assert client.get("/notes/", headers=hdrs).status_code == 200


def test_per_key_different_keys_independent(make_client):
    """不同 API key 的計數彼此獨立。"""
    fake = FakeClock()
    key_lim = SlidingWindowRateLimiter(max_calls=1, window_seconds=60, clock=fake)
    tenant_lim = SlidingWindowRateLimiter(max_calls=1000, window_seconds=60, clock=fake)
    client = make_client(_AlwaysOnRateLimiter(key_lim, tenant_lim))

    token = _register(client, "key2@rl-test.com", "password1", "RL-KeyTenant2")
    key_a = _create_api_key(client, token, "key-a")
    key_b = _create_api_key(client, token, "key-b")

    # key_a 用掉額度
    assert client.get("/notes/", headers={"X-API-Key": key_a}).status_code == 200
    # key_b 獨立，仍可用
    assert client.get("/notes/", headers={"X-API-Key": key_b}).status_code == 200
    # key_a 超限
    assert client.get("/notes/", headers={"X-API-Key": key_a}).status_code == 429


# ══════════════════════════════════════════════════════════════════════════════
# 3. 整合測試：per-tenant 速率限制（JWT 認證）
# ══════════════════════════════════════════════════════════════════════════════

def test_per_tenant_rate_limit_429(make_client):
    """JWT 認證時以 tenant_id 計數；超限 → 429 + Retry-After；視窗過期後恢復。"""
    fake = FakeClock()
    key_lim = SlidingWindowRateLimiter(max_calls=1000, window_seconds=60, clock=fake)
    tenant_lim = SlidingWindowRateLimiter(max_calls=2, window_seconds=30, clock=fake)
    client = make_client(_AlwaysOnRateLimiter(key_lim, tenant_lim))

    token = _register(client, "jwt1@rl-test.com", "password1", "RL-JwtTenant1")
    hdrs = {"Authorization": f"Bearer {token}"}

    assert client.get("/notes/", headers=hdrs).status_code == 200
    assert client.get("/notes/", headers=hdrs).status_code == 200

    r = client.get("/notes/", headers=hdrs)
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "30"  # window_seconds=30

    fake.advance(31)
    assert client.get("/notes/", headers=hdrs).status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# 4. 單元測試：BusinessRateLimiter 的 rate_limit_enabled 旗標
# ══════════════════════════════════════════════════════════════════════════════

def test_business_rate_limiter_disabled_bypasses_check():
    """rate_limit_enabled=False 時，BusinessRateLimiter 直接 return，不拋 429。"""
    from saas_mvp.config import settings
    from dataclasses import dataclass

    fake = FakeClock()
    # max_calls=0：只要有任何請求就超限（若有檢查的話）
    key_lim = SlidingWindowRateLimiter(max_calls=0, window_seconds=60, clock=fake)
    tenant_lim = SlidingWindowRateLimiter(max_calls=0, window_seconds=60, clock=fake)
    prod_limiter = BusinessRateLimiter(key_lim, tenant_lim)

    # 模擬 actor（duck typing，不用真正的 Actor dataclass）
    class _FakeUser:
        tenant_id = 99

    class _FakeActor:
        api_key_id = None
        user = _FakeUser()

    original = settings.rate_limit_enabled
    try:
        settings.rate_limit_enabled = False
        # 直接呼叫（不透過 FastAPI DI）；Depends 的 default 只是 marker，不會自動解析
        prod_limiter.__call__(actor=_FakeActor())  # 不應拋例外
    finally:
        settings.rate_limit_enabled = original
