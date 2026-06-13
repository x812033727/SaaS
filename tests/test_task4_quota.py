"""Task #4 驗收測試：plan/quota 計量與超量攔截

測試重點
--------
1. validate_count：合法整數通過、bool/str/負數拒絕
2. check_and_increment：未超量正常遞增；邊界值（limit-1 → limit）；超量 429
3. get_quota_status：回傳正確 used / remaining
4. free/pro 各自有不同 limit
5. 不同租戶 quota 計量互不影響（隔離）
6. HTTP 端點 GET /quota/status 回傳正確結構
7. POST /notes/ 超量回 429
"""

from __future__ import annotations

import datetime
import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# 確保所有 model 都已載入
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.app import create_app
from saas_mvp.auth.security import hash_password
from saas_mvp.db import Base, get_db
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.usage import ApiUsage
from saas_mvp.models.user import User
from saas_mvp.quota import (
    PLAN_DAILY_LIMITS,
    check_and_increment,
    get_quota_status,
    validate_count,
)


# ──────────────────────────── helpers ────────────────────────────────────────

def make_engine():
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def make_session(engine):
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


# ──────────────────────────── 1. validate_count ───────────────────────────────

class TestValidateCount:
    def test_valid_zero(self):
        assert validate_count(0) == 0

    def test_valid_positive(self):
        assert validate_count(42) == 42

    def test_bool_true_rejected(self):
        with pytest.raises(TypeError, match="bool"):
            validate_count(True)

    def test_bool_false_rejected(self):
        with pytest.raises(TypeError, match="bool"):
            validate_count(False)

    def test_string_rejected(self):
        with pytest.raises(TypeError):
            validate_count("5")  # type: ignore[arg-type]

    def test_float_rejected(self):
        with pytest.raises(TypeError):
            validate_count(3.0)  # type: ignore[arg-type]

    def test_negative_rejected(self):
        with pytest.raises(ValueError, match=">= 0"):
            validate_count(-1)


# ──────────────────────────── 2. check_and_increment ─────────────────────────

class TestCheckAndIncrement:
    @pytest.fixture(autouse=True)
    def session(self):
        engine = make_engine()
        self.db = make_session(engine)
        tenant = Tenant(name="test-tenant-quota", plan="free")
        self.db.add(tenant)
        self.db.commit()
        self.tenant_id = tenant.id
        yield
        self.db.close()

    def test_first_call_returns_1(self):
        count = check_and_increment(self.db, self.tenant_id, "free")
        assert count == 1

    def test_increments_sequentially(self):
        check_and_increment(self.db, self.tenant_id, "free")
        count = check_and_increment(self.db, self.tenant_id, "free")
        assert count == 2

    def test_raises_429_when_limit_reached(self):
        """free 上限為 100；直接把 DB 計數設到 limit，下一次應 429。"""
        limit = PLAN_DAILY_LIMITS["free"]
        today = datetime.date.today()
        row = ApiUsage(tenant_id=self.tenant_id, period=today, count=limit)
        self.db.add(row)
        self.db.commit()

        with pytest.raises(HTTPException) as exc_info:
            check_and_increment(self.db, self.tenant_id, "free")
        assert exc_info.value.status_code == 429
        assert "Quota exceeded" in exc_info.value.detail

    def test_boundary_at_limit_minus_1_passes(self):
        """count = limit-1 時，呼叫應成功（返回 limit）。"""
        limit = PLAN_DAILY_LIMITS["free"]
        today = datetime.date.today()
        row = ApiUsage(tenant_id=self.tenant_id, period=today, count=limit - 1)
        self.db.add(row)
        self.db.commit()

        count = check_and_increment(self.db, self.tenant_id, "free")
        assert count == limit   # 剛好到滿，不超量

    def test_pro_has_higher_limit(self):
        """pro 上限遠高於 free；直接設到 free 上限呼叫 pro plan 不會 429。"""
        free_limit = PLAN_DAILY_LIMITS["free"]
        today = datetime.date.today()
        row = ApiUsage(tenant_id=self.tenant_id, period=today, count=free_limit)
        self.db.add(row)
        self.db.commit()

        count = check_and_increment(self.db, self.tenant_id, "pro")
        assert count == free_limit + 1


# ──────────────────────────── 3. get_quota_status ────────────────────────────

class TestGetQuotaStatus:
    @pytest.fixture(autouse=True)
    def session(self):
        engine = make_engine()
        self.db = make_session(engine)
        tenant = Tenant(name="status-tenant", plan="pro")
        self.db.add(tenant)
        self.db.commit()
        self.tenant_id = tenant.id
        yield
        self.db.close()

    def test_zero_usage_when_no_calls(self):
        status = get_quota_status(self.db, self.tenant_id, "pro")
        assert status["used"] == 0
        assert status["limit"] == PLAN_DAILY_LIMITS["pro"]
        assert status["remaining"] == PLAN_DAILY_LIMITS["pro"]
        assert "period" in status
        assert status["plan"] == "pro"

    def test_usage_reflects_increments(self):
        check_and_increment(self.db, self.tenant_id, "pro")
        check_and_increment(self.db, self.tenant_id, "pro")
        status = get_quota_status(self.db, self.tenant_id, "pro")
        assert status["used"] == 2
        assert status["remaining"] == PLAN_DAILY_LIMITS["pro"] - 2

    def test_remaining_never_negative(self):
        """即使 count 超過 limit（手動注入），remaining 應不為負。"""
        today = datetime.date.today()
        row = ApiUsage(
            tenant_id=self.tenant_id,
            period=today,
            count=PLAN_DAILY_LIMITS["pro"] + 999,
        )
        self.db.add(row)
        self.db.commit()
        status = get_quota_status(self.db, self.tenant_id, "pro")
        assert status["remaining"] == 0


# ──────────────────────────── 4. 不同租戶隔離 ────────────────────────────────

class TestTenantQuotaIsolation:
    """兩個租戶的 quota 計量必須完全獨立。"""

    @pytest.fixture(autouse=True)
    def session(self):
        engine = make_engine()
        self.db = make_session(engine)
        ta = Tenant(name="iso-alpha", plan="free")
        tb = Tenant(name="iso-beta", plan="free")
        self.db.add_all([ta, tb])
        self.db.commit()
        self.ta_id, self.tb_id = ta.id, tb.id
        yield
        self.db.close()

    def test_alpha_calls_do_not_affect_beta(self):
        for _ in range(5):
            check_and_increment(self.db, self.ta_id, "free")
        status_b = get_quota_status(self.db, self.tb_id, "free")
        assert status_b["used"] == 0

    def test_independent_counters(self):
        for _ in range(3):
            check_and_increment(self.db, self.ta_id, "free")
        for _ in range(7):
            check_and_increment(self.db, self.tb_id, "free")
        assert get_quota_status(self.db, self.ta_id, "free")["used"] == 3
        assert get_quota_status(self.db, self.tb_id, "free")["used"] == 7


# ──────────────────────────── 5. HTTP 端點 ───────────────────────────────────

# module-level engine 供 HTTP 測試與直接 DB 操作共用
_http_engine = make_engine()
_HttpSession = sessionmaker(autocommit=False, autoflush=False, bind=_http_engine)


@pytest.fixture(scope="module")
def http_client():
    Base.metadata.create_all(bind=_http_engine)
    app = create_app()

    def override_get_db():
        db = _HttpSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture(scope="module")
def free_token(http_client):
    resp = http_client.post("/auth/register", json={
        "email": "quota_user@example.com",
        "password": "QuotaPass99!",
        "tenant_name": "quota-corp",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


class TestQuotaEndpoint:
    def test_status_200(self, http_client, free_token):
        resp = http_client.get(
            "/quota/status",
            headers={"Authorization": f"Bearer {free_token}"},
        )
        assert resp.status_code == 200

    def test_status_fields(self, http_client, free_token):
        data = http_client.get(
            "/quota/status",
            headers={"Authorization": f"Bearer {free_token}"},
        ).json()
        assert data["plan"] == "free"
        assert data["limit"] == PLAN_DAILY_LIMITS["free"]
        assert isinstance(data["used"], int)
        assert isinstance(data["remaining"], int)
        assert "period" in data

    def test_unauthenticated_returns_401(self, http_client):
        resp = http_client.get("/quota/status")
        assert resp.status_code == 401

    def test_quota_exceeded_returns_429(self, http_client):
        """新建 tenant，手動讓 DB count = limit，再 POST /notes/ 應回 429。"""
        resp = http_client.post("/auth/register", json={
            "email": "overlimit@example.com",
            "password": "OverPass99!",
            "tenant_name": "over-corp",
        })
        assert resp.status_code == 201, resp.text
        token = resp.json()["access_token"]

        # 取 tenant_id
        tenant_id = http_client.get(
            "/tenants/me",
            headers={"Authorization": f"Bearer {token}"},
        ).json()["id"]

        # 直接用同一個 engine 的 session 塞滿配額
        limit = PLAN_DAILY_LIMITS["free"]
        today = datetime.date.today()
        direct_db = _HttpSession()
        try:
            direct_db.execute(
                text(
                    "INSERT INTO api_usage (tenant_id, period, count) "
                    "VALUES (:tid, :dt, :cnt) "
                    "ON CONFLICT(tenant_id, period) DO UPDATE SET count = :cnt"
                ),
                {"tid": tenant_id, "dt": today.isoformat(), "cnt": limit},
            )
            direct_db.commit()
        finally:
            direct_db.close()

        # POST /notes/ 應觸發 quota 超量 429
        resp2 = http_client.post(
            "/notes/",
            json={"title": "should fail", "content": "x"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 429
        assert "Quota" in resp2.json()["detail"]

    def test_get_notes_also_consumes_quota(self, http_client):
        """GET /notes/ 也應計量（讀操作不可繞過 quota）。"""
        # 建一個新 tenant，初始 used=0
        resp = http_client.post("/auth/register", json={
            "email": "get_quota@example.com",
            "password": "GetQuota99!",
            "tenant_name": "get-corp",
        })
        assert resp.status_code == 201, resp.text
        token = resp.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        # 呼叫前確認 used=0
        before = http_client.get("/quota/status", headers=auth).json()
        assert before["used"] == 0

        # GET /notes/ 應計量 +1
        list_resp = http_client.get("/notes/", headers=auth)
        assert list_resp.status_code == 200

        after = http_client.get("/quota/status", headers=auth).json()
        assert after["used"] == 1  # GET /notes/ 消耗了 1 次配額

    def test_get_single_note_also_consumes_quota(self, http_client):
        """GET /notes/{id} 也應計量；超量後讀操作同樣 429。"""
        resp = http_client.post("/auth/register", json={
            "email": "single_quota@example.com",
            "password": "Single999!",
            "tenant_name": "single-corp",
        })
        assert resp.status_code == 201, resp.text
        token = resp.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        # 先建一筆 note（消耗 1 quota）
        note_id = http_client.post(
            "/notes/", json={"title": "t", "content": "c"}, headers=auth,
        ).json()["id"]

        # 設定 count = limit
        tenant_id = http_client.get("/tenants/me", headers=auth).json()["id"]
        limit = PLAN_DAILY_LIMITS["free"]
        today = datetime.date.today()
        direct_db = _HttpSession()
        try:
            direct_db.execute(
                text(
                    "INSERT INTO api_usage (tenant_id, period, count) "
                    "VALUES (:tid, :dt, :cnt) "
                    "ON CONFLICT(tenant_id, period) DO UPDATE SET count = :cnt"
                ),
                {"tid": tenant_id, "dt": today.isoformat(), "cnt": limit},
            )
            direct_db.commit()
        finally:
            direct_db.close()

        # GET /notes/{id} 應也觸發 429
        resp2 = http_client.get(f"/notes/{note_id}", headers=auth)
        assert resp2.status_code == 429
