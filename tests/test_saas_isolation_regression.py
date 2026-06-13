"""跨租戶隔離回歸測試（Task #5 整合層）

驗收標準
--------
1. Billing 端點只能操作自己的 tenant（無 tenant_id 參數，設計上不可能跨租）
   → 驗證 upgrade 後只有 actor 的租戶 plan 改變，他人不受影響
2. 被停用租戶在所有新功能端點（billing / notes）一律 403
3. Admin 非 admin 全 403（cross-tenant 概況對一般使用者不可見）
4. Rate limiter 各 tenant 計數獨立，A 超限不影響 B（整合路徑）
5. 升降級後 quota 立即切換；另一租戶 quota 不受影響
6. 跨租戶嘗試讀取 /admin/tenants/{id}/usage → 403（非 admin）
7. API key 只屬於建立者的 tenant，admin /api-keys 可見所有 key

全部離線，in-memory SQLite，無 time.sleep。
"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku              # noqa: F401
from saas_mvp.models import plan_change_history as _pch                         # noqa: F401
from saas_mvp.app import create_app
from saas_mvp.auth.ratelimit import SlidingWindowRateLimiter, require_rate_limit
from saas_mvp.auth.dependencies import Actor, get_current_actor
from saas_mvp.db import Base, get_db
from saas_mvp.models.usage import ApiUsage
from saas_mvp.quota import PLAN_DAILY_LIMITS

# ── 假時鐘 ────────────────────────────────────────────────────────────────────

class FakeClock:
    def __init__(self, start: float = 0.0):
        self.t = start
    def __call__(self) -> float:
        return self.t
    def advance(self, s: float) -> None:
        self.t += s


# ── In-memory SQLite ──────────────────────────────────────────────────────────

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
def client():
    Base.metadata.create_all(bind=_engine)
    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── 工具 ─────────────────────────────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register(client, tenant=None):
    """回傳 (token, tenant_id)"""
    email = f"u_{_uid()}@reg.com"
    tn = tenant or f"t_{_uid()}"
    r = client.post("/auth/register", json={"email": email, "password": "Test1234!", "tenant_name": tn})
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    me = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
    return token, me.json()["id"]


def _make_admin(token):
    from saas_mvp.auth.security import decode_access_token
    from saas_mvp.models.user import User
    uid = int(decode_access_token(token)["sub"])
    db = _Session()
    try:
        u = db.get(User, uid)
        u.is_admin = True
        db.commit()
    finally:
        db.close()


def _disable_tenant(client, admin_token, tid):
    r = client.patch(
        f"/admin/tenants/{tid}",
        json={"is_active": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200


def _enable_tenant(client, admin_token, tid):
    client.patch(
        f"/admin/tenants/{tid}",
        json={"is_active": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Billing 操作只影響 actor 自己的 tenant
# ═══════════════════════════════════════════════════════════════════════════════

class TestBillingIsolation:
    def test_upgrade_does_not_affect_other_tenant(self, client):
        """Alice 升 pro 不影響 Bob 的方案。"""
        alice_token, alice_tid = _register(client)
        bob_token, bob_tid = _register(client)

        # Alice 升 pro
        r = client.post("/billing/upgrade", json={"plan": "pro"},
                        headers={"Authorization": f"Bearer {alice_token}"})
        assert r.status_code == 200

        # Alice → pro
        me_a = client.get("/tenants/me", headers={"Authorization": f"Bearer {alice_token}"})
        assert me_a.json()["plan"] == "pro"

        # Bob → 仍 free（未受影響）
        me_b = client.get("/tenants/me", headers={"Authorization": f"Bearer {bob_token}"})
        assert me_b.json()["plan"] == "free"

    def test_downgrade_409_does_not_affect_other_tenant(self, client):
        """Alice 降級 409，不影響 Bob。"""
        alice_token, alice_tid = _register(client)
        bob_token, bob_tid = _register(client)

        # Alice 先升 pro，再塞超量資料
        client.post("/billing/upgrade", json={"plan": "pro"},
                    headers={"Authorization": f"Bearer {alice_token}"})
        today = datetime.date.today()
        db = _Session()
        try:
            db.add(ApiUsage(tenant_id=alice_tid, period=today, count=200))
            db.commit()
        finally:
            db.close()

        # Alice 降級 → 409
        r = client.post("/billing/downgrade", json={"plan": "free"},
                        headers={"Authorization": f"Bearer {alice_token}"})
        assert r.status_code == 409

        # Bob 的方案不受影響
        me_b = client.get("/tenants/me", headers={"Authorization": f"Bearer {bob_token}"})
        assert me_b.json()["plan"] == "free"

    def test_quota_scope_per_tenant(self, client):
        """升降級後 quota 只影響自己的 tenant。"""
        alice_token, alice_tid = _register(client)
        bob_token, bob_tid = _register(client)

        # Alice 升 pro
        client.post("/billing/upgrade", json={"plan": "pro"},
                    headers={"Authorization": f"Bearer {alice_token}"})

        # Alice quota limit = 10000
        r_a = client.get("/quota/status", headers={"Authorization": f"Bearer {alice_token}"})
        assert r_a.json()["limit"] == PLAN_DAILY_LIMITS["pro"]

        # Bob quota limit = 100（仍 free）
        r_b = client.get("/quota/status", headers={"Authorization": f"Bearer {bob_token}"})
        assert r_b.json()["limit"] == PLAN_DAILY_LIMITS["free"]


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 停用租戶在新端點一律 403
# ═══════════════════════════════════════════════════════════════════════════════

class TestDisabledTenantBlocked:
    @pytest.fixture(scope="class")
    def setup(self, client):
        admin_token, _ = _register(client)
        _make_admin(admin_token)
        victim_token, victim_tid = _register(client)
        # 先升 pro（確認停用後 billing 也被 403）
        client.post("/billing/upgrade", json={"plan": "pro"},
                    headers={"Authorization": f"Bearer {victim_token}"})
        # 停用
        _disable_tenant(client, admin_token, victim_tid)
        yield {
            "admin_token": admin_token,
            "victim_token": victim_token,
            "victim_tid": victim_tid,
            "client": client,
        }

    def test_notes_blocked(self, setup):
        c, tok = setup["client"], setup["victim_token"]
        r = c.get("/notes/", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403
        assert "disabled" in r.json()["detail"]

    def test_billing_upgrade_blocked(self, setup):
        c, tok = setup["client"], setup["victim_token"]
        r = c.post("/billing/upgrade", json={"plan": "pro"},
                   headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403

    def test_billing_downgrade_blocked(self, setup):
        c, tok = setup["client"], setup["victim_token"]
        r = c.post("/billing/downgrade", json={"plan": "free"},
                   headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403

    def test_quota_status_blocked(self, setup):
        c, tok = setup["client"], setup["victim_token"]
        r = c.get("/quota/status", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403

    def test_re_enable_restores_access(self, setup):
        c = setup["client"]
        _enable_tenant(c, setup["admin_token"], setup["victim_tid"])
        r = c.get("/notes/", headers={"Authorization": f"Bearer {setup['victim_token']}"})
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 非 admin 無法透過 admin 端點存取他人資料
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdminCrossTenantBlocked:
    def test_regular_user_cannot_list_tenants(self, client):
        token, _ = _register(client)
        r = client.get("/admin/tenants", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_regular_user_cannot_get_other_usage(self, client):
        alice_token, _ = _register(client)
        _, bob_tid = _register(client)
        r = client.get(
            f"/admin/tenants/{bob_tid}/usage",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert r.status_code == 403

    def test_regular_user_cannot_patch_other_tenant(self, client):
        alice_token, _ = _register(client)
        _, bob_tid = _register(client)
        r = client.patch(
            f"/admin/tenants/{bob_tid}",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert r.status_code == 403

    def test_regular_user_cannot_list_api_keys(self, client):
        token, _ = _register(client)
        r = client.get("/admin/api-keys", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Rate limiter 各 tenant 計數獨立（整合路徑）
# ═══════════════════════════════════════════════════════════════════════════════

class TestRateLimitIsolation:
    def test_tenant_a_limit_does_not_block_tenant_b(self):
        """注入假時鐘：A 超限，B 仍可正常請求。"""
        from fastapi import Depends

        fake = FakeClock()
        key_lim = SlidingWindowRateLimiter(max_calls=1, window_seconds=60, clock=fake)
        tenant_lim = SlidingWindowRateLimiter(max_calls=1, window_seconds=60, clock=fake)

        class _AlwaysOn:
            def __init__(self):
                self._k, self._t = key_lim, tenant_lim
            def __call__(self, actor: Actor = Depends(get_current_actor)):
                if actor.api_key_id is not None:
                    self._k._check_rate_limit(f"key:{actor.api_key_id}")
                else:
                    self._t._check_rate_limit(f"tenant:{actor.user.tenant_id}")

        Base.metadata.create_all(bind=_engine)
        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[require_rate_limit] = _AlwaysOn()

        with TestClient(app, raise_server_exceptions=True) as c:
            alice_token, _ = _register(c)
            bob_token, _ = _register(c)

            # Alice 用完額度
            r = c.get("/notes/", headers={"Authorization": f"Bearer {alice_token}"})
            assert r.status_code == 200
            r = c.get("/notes/", headers={"Authorization": f"Bearer {alice_token}"})
            assert r.status_code == 429

            # Bob 獨立計數，仍可請求
            r = c.get("/notes/", headers={"Authorization": f"Bearer {bob_token}"})
            assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 5. API key 跨租戶隔離：Bob 的 key 無法存取 Alice 的資料
# ═══════════════════════════════════════════════════════════════════════════════

class TestApiKeyIsolation:
    def test_bob_key_authenticates_as_bob_not_alice(self, client):
        """Bob 用自己的 API key 打 /tenants/me，得到的是 Bob 的 tenant，不是 Alice。"""
        alice_token, alice_tid = _register(client)
        bob_token, bob_tid = _register(client)

        # Bob 建立 API key
        r = client.post(
            "/api-keys/",
            json={"name": "bob-key"},
            headers={"Authorization": f"Bearer {bob_token}"},
        )
        assert r.status_code == 201
        bob_key = r.json()["plain_key"]

        # 用 Bob 的 key 打 /tenants/me
        me = client.get("/tenants/me", headers={"X-API-Key": bob_key})
        assert me.status_code == 200
        assert me.json()["id"] == bob_tid
        assert me.json()["id"] != alice_tid

    def test_notes_created_via_key_belong_to_key_tenant(self, client):
        """用 API key 建立的 note 屬於 key 所屬 tenant，Alice 看不到。"""
        alice_token, alice_tid = _register(client)
        bob_token, bob_tid = _register(client)

        # Bob 取 key
        r = client.post(
            "/api-keys/",
            json={"name": "bob-note-key"},
            headers={"Authorization": f"Bearer {bob_token}"},
        )
        bob_key = r.json()["plain_key"]

        # Bob 用 key 建 note
        rn = client.post(
            "/notes/",
            json={"title": "Bob's secret", "content": "hidden"},
            headers={"X-API-Key": bob_key},
        )
        assert rn.status_code == 201
        bob_note_id = rn.json()["id"]

        # Alice 看不到 Bob 的 note
        alice_list = client.get("/notes/", headers={"Authorization": f"Bearer {alice_token}"})
        ids = [n["id"] for n in alice_list.json()]
        assert bob_note_id not in ids
