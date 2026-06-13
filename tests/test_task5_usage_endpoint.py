"""Task #5 驗收測試：/usage 端點

驗收標準（對應 task #5）：
  A. /usage 回傳目前租戶總量 + per-key 明細 + 各自剩餘 quota
  B. 回應欄位：tenant.{plan,daily_limit,used_today,remaining,period}
               api_keys[].{api_key_id,name,key_prefix,used_today,period}
  C. remaining == max(0, daily_limit - used_today)（可手算）
  D. 小樣本計量可逐位核對（呼叫 N 次 → used_today == N）
  E. 未認證 → 401；認證成功 → 200
  F. 可用 X-API-Key 或 Bearer JWT 認證
  G. 跨租戶隔離：Bob 的 /usage 不含 Alice 的 key

全部離線，in-memory SQLite。
"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# 確保所有 model metadata 已載入
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku  # noqa: F401
from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.quota import PLAN_DAILY_LIMITS

# ── In-memory SQLite 設定 ─────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_get_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── 共用 helpers ──────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register(client, email: str, password: str, tenant: str) -> str:
    resp = client.post("/auth/register", json={
        "email": email, "password": password, "tenant_name": tenant,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def _create_key(client, jwt_token: str, name: str = "test-key") -> dict:
    resp = client.post(
        "/api-keys/",
        json={"name": name},
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def alice_jwt(client):
    uid = _uid()
    return _register(client, f"alice5_{uid}@test.com", "AlicePass99!", f"usage-alpha-{uid}")


@pytest.fixture(scope="module")
def bob_jwt(client):
    uid = _uid()
    return _register(client, f"bob5_{uid}@test.com", "BobPass99!!", f"usage-beta-{uid}")


@pytest.fixture(scope="module")
def alice_key(client, alice_jwt):
    return _create_key(client, alice_jwt, "alice-usage-key")


# ── A + E：基本存取與認證 ─────────────────────────────────────

class TestUsageBasicAccess:
    def test_no_auth_returns_401(self, client):
        """未帶任何憑證 → 401。"""
        resp = client.get("/usage/")
        assert resp.status_code == 401

    def test_jwt_auth_returns_200(self, client, alice_jwt):
        """Bearer JWT 認證 → 200。"""
        resp = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"})
        assert resp.status_code == 200

    def test_x_api_key_auth_returns_200(self, client, alice_key):
        """X-API-Key header 認證 → 200。"""
        resp = client.get("/usage/", headers={"X-API-Key": alice_key["plain_key"]})
        assert resp.status_code == 200

    def test_bearer_api_key_auth_returns_200(self, client, alice_key):
        """Bearer <api_key> 認證 → 200。"""
        resp = client.get(
            "/usage/",
            headers={"Authorization": f"Bearer {alice_key['plain_key']}"},
        )
        assert resp.status_code == 200

    def test_invalid_bearer_returns_401(self, client):
        """無效 Bearer → 401。"""
        resp = client.get("/usage/", headers={"Authorization": "Bearer garbage_token"})
        assert resp.status_code == 401


# ── B：回應欄位完整性 ─────────────────────────────────────────

class TestUsageResponseFields:
    def test_response_has_tenant_and_api_keys(self, client, alice_jwt):
        """回應根層級有 tenant 與 api_keys。"""
        data = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        assert "tenant" in data, "缺少 tenant 欄位"
        assert "api_keys" in data, "缺少 api_keys 欄位"

    def test_tenant_has_all_required_fields(self, client, alice_jwt):
        """tenant 物件包含規定的五個欄位。"""
        t = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()["tenant"]
        for field in ("plan", "daily_limit", "used_today", "remaining", "period"):
            assert field in t, f"tenant 缺少欄位: {field}"

    def test_tenant_plan_is_string(self, client, alice_jwt):
        """tenant.plan 是字串（free/pro）。"""
        t = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()["tenant"]
        assert isinstance(t["plan"], str)
        assert t["plan"] in ("free", "pro")

    def test_tenant_daily_limit_positive(self, client, alice_jwt):
        """tenant.daily_limit > 0。"""
        t = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()["tenant"]
        assert isinstance(t["daily_limit"], int)
        assert t["daily_limit"] > 0

    def test_tenant_used_today_non_negative(self, client, alice_jwt):
        """tenant.used_today >= 0。"""
        t = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()["tenant"]
        assert isinstance(t["used_today"], int)
        assert t["used_today"] >= 0

    def test_tenant_remaining_non_negative(self, client, alice_jwt):
        """tenant.remaining >= 0。"""
        t = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()["tenant"]
        assert isinstance(t["remaining"], int)
        assert t["remaining"] >= 0

    def test_tenant_period_is_iso_date(self, client, alice_jwt):
        """tenant.period 是 ISO 8601 日期字串，且等於今天。"""
        t = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()["tenant"]
        period = t["period"]
        assert isinstance(period, str)
        parsed = datetime.date.fromisoformat(period)   # 若格式錯誤會拋例外
        assert parsed == datetime.date.today()

    def test_api_keys_is_list(self, client, alice_jwt):
        """api_keys 是 list。"""
        data = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        assert isinstance(data["api_keys"], list)

    def test_api_key_item_has_required_fields(self, client, alice_jwt):
        """api_keys 內的每個條目含規定的五個欄位。"""
        # 先確保有 key + 有 usage 記錄（呼叫一次 /notes/ ）
        key = _create_key(client, alice_jwt, "field-verify-key")
        client.get("/notes/", headers={"X-API-Key": key["plain_key"]})

        data = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        assert len(data["api_keys"]) >= 1, "api_keys 應至少含一筆"
        for item in data["api_keys"]:
            for field in ("api_key_id", "name", "key_prefix", "used_today", "period"):
                assert field in item, f"api_key item 缺少欄位: {field}"

    def test_api_key_item_period_is_iso_date(self, client, alice_jwt):
        """api_keys[].period 也是 ISO 8601 日期字串。"""
        key = _create_key(client, alice_jwt, "period-check-key")
        client.get("/notes/", headers={"X-API-Key": key["plain_key"]})

        data = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        for item in data["api_keys"]:
            datetime.date.fromisoformat(item["period"])   # 格式錯誤會 raise


# ── C：remaining 公式可手算核對 ──────────────────────────────

class TestUsageRemainingFormula:
    def test_remaining_equals_limit_minus_used(self, client, alice_jwt):
        """remaining == max(0, daily_limit - used_today)。"""
        data = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        t = data["tenant"]
        expected = max(0, t["daily_limit"] - t["used_today"])
        assert t["remaining"] == expected, (
            f"remaining={t['remaining']} ≠ max(0, {t['daily_limit']}-{t['used_today']})={expected}"
        )

    def test_remaining_never_negative(self, client):
        """即使 used_today >= daily_limit，remaining 仍為 0（不會是負數）。"""
        # 建立新租戶，直接在 DB 塞超量值
        uid = _uid()
        jwt = _register(client, f"over5_{uid}@test.com", "OverPass99!", f"over5-{uid}")
        tenant_id = client.get(
            "/tenants/me", headers={"Authorization": f"Bearer {jwt}"}
        ).json()["id"]
        limit = PLAN_DAILY_LIMITS["free"]
        today = datetime.date.today()
        db = _Session()
        try:
            db.execute(
                text(
                    "INSERT INTO api_usage (tenant_id, period, count) "
                    "VALUES (:tid, :dt, :cnt) "
                    "ON CONFLICT(tenant_id, period) DO UPDATE SET count = :cnt"
                ),
                {"tid": tenant_id, "dt": today.isoformat(), "cnt": limit + 10},
            )
            db.commit()
        finally:
            db.close()

        data = client.get("/usage/", headers={"Authorization": f"Bearer {jwt}"}).json()
        assert data["tenant"]["remaining"] == 0, "超量時 remaining 應為 0，不能為負"


# ── D：小樣本計量逐位核對 ──────────────────────────────────

class TestUsageCountAccuracy:
    def test_per_key_count_matches_call_count(self, client, alice_jwt):
        """新 key 呼叫 N 次後，api_keys[].used_today 應精確等於 N。"""
        key = _create_key(client, alice_jwt, "count-exact-key")
        plain = key["plain_key"]
        key_id = key["id"]
        N = 4

        for _ in range(N):
            r = client.get("/notes/", headers={"X-API-Key": plain})
            assert r.status_code == 200

        data = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        per_key = {item["api_key_id"]: item["used_today"] for item in data["api_keys"]}
        assert per_key.get(key_id, 0) == N, (
            f"呼叫 {N} 次，實際 used_today={per_key.get(key_id, 0)}"
        )

    def test_tenant_used_today_includes_api_key_calls(self, client, alice_jwt):
        """API key 呼叫必須反映在 tenant.used_today。"""
        before = client.get(
            "/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}
        ).json()["tenant"]["used_today"]

        key = _create_key(client, alice_jwt, "tenant-total-key")
        M = 3
        for _ in range(M):
            client.get("/notes/", headers={"X-API-Key": key["plain_key"]})

        after = client.get(
            "/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}
        ).json()["tenant"]["used_today"]

        assert after >= before + M, (
            f"tenant.used_today 應增加 ≥ {M}，實際 before={before} after={after}"
        )

    def test_multiple_keys_separate_counts(self, client, alice_jwt):
        """兩個 key 各自累計，互不干擾。"""
        key_a = _create_key(client, alice_jwt, "separate-key-a")
        key_b = _create_key(client, alice_jwt, "separate-key-b")

        # key_a 呼叫 2 次，key_b 呼叫 3 次
        for _ in range(2):
            client.get("/notes/", headers={"X-API-Key": key_a["plain_key"]})
        for _ in range(3):
            client.get("/notes/", headers={"X-API-Key": key_b["plain_key"]})

        data = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        per_key = {item["api_key_id"]: item["used_today"] for item in data["api_keys"]}

        assert per_key.get(key_a["id"], 0) >= 2, "key_a 應有 ≥ 2 次呼叫"
        assert per_key.get(key_b["id"], 0) >= 3, "key_b 應有 ≥ 3 次呼叫"

    def test_no_key_usage_no_api_keys_entry(self, client):
        """剛建立的租戶，沒有 key 呼叫時，api_keys 對應筆數為 0（或無此 key 條目）。"""
        uid = _uid()
        jwt = _register(client, f"fresh5_{uid}@test.com", "FreshPass99!", f"fresh5-{uid}")
        _create_key(client, jwt, "unused-key")   # 建立但不使用

        data = client.get("/usage/", headers={"Authorization": f"Bearer {jwt}"}).json()
        # 未使用的 key 在 api_keys 中不會出現（因 ApiKeyUsage 沒有記錄）
        assert data["api_keys"] == [], (
            f"未使用的 key 不應出現在 api_keys，實際: {data['api_keys']}"
        )


# ── G：跨租戶隔離 ────────────────────────────────────────────

class TestUsageCrossTenantIsolation:
    def test_bob_usage_does_not_show_alice_keys(self, client, alice_jwt, bob_jwt, alice_key):
        """Bob 的 /usage 不含 Alice 的任何 API key。"""
        # 讓 Alice 的 key 有 usage 記錄
        client.get("/notes/", headers={"X-API-Key": alice_key["plain_key"]})

        alice_keys_resp = client.get(
            "/api-keys/", headers={"Authorization": f"Bearer {alice_jwt}"}
        ).json()
        alice_key_ids = {k["id"] for k in alice_keys_resp}

        bob_data = client.get(
            "/usage/", headers={"Authorization": f"Bearer {bob_jwt}"}
        ).json()
        bob_api_key_ids = {item["api_key_id"] for item in bob_data["api_keys"]}

        assert alice_key_ids.isdisjoint(bob_api_key_ids), (
            f"Bob 的 /usage 不應含 Alice 的 key IDs: {alice_key_ids & bob_api_key_ids}"
        )

    def test_tenant_totals_are_independent(self, client, alice_jwt, bob_jwt):
        """Alice 的 tenant.used_today 不受 Bob 的呼叫影響。"""
        # Alice 先查現況
        alice_before = client.get(
            "/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}
        ).json()["tenant"]["used_today"]

        # Bob 呼叫幾次 /notes/
        bob_key = _create_key(client, bob_jwt, "bob-isolation-key")
        for _ in range(3):
            client.get("/notes/", headers={"X-API-Key": bob_key["plain_key"]})

        # Alice 的總量不應改變（/usage 本身不計量）
        alice_after = client.get(
            "/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}
        ).json()["tenant"]["used_today"]

        assert alice_after == alice_before, (
            f"Bob 呼叫不應影響 Alice 的用量：before={alice_before} after={alice_after}"
        )

    def test_alice_key_not_usable_in_bob_context(self, client, alice_key, bob_jwt):
        """Alice 的 key 認證後是 Alice 的租戶，Bob 查不到 Alice 的資料。"""
        # 用 Alice key 認證，應能成功（以 Alice 身份）
        resp_alice_key = client.get("/usage/", headers={"X-API-Key": alice_key["plain_key"]})
        assert resp_alice_key.status_code == 200
        data_via_alice_key = resp_alice_key.json()

        # Bob 用自己的 JWT 查，資料應與 Alice key 查到的不同（不同租戶）
        data_via_bob_jwt = client.get(
            "/usage/", headers={"Authorization": f"Bearer {bob_jwt}"}
        ).json()

        assert data_via_alice_key["tenant"]["plan"] is not None
        # 兩邊查出的 api_keys 集合不應相同（隔離）
        alice_key_ids = {i["api_key_id"] for i in data_via_alice_key["api_keys"]}
        bob_key_ids = {i["api_key_id"] for i in data_via_bob_jwt["api_keys"]}
        assert alice_key_ids.isdisjoint(bob_key_ids), "跨租戶資料洩漏"
