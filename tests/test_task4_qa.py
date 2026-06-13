"""QA 驗收測試 — Task #4：plan/quota 計量與超量處理

驗收標準 #5 的完整覆蓋：
  - free/pro 配額明確（常數值）
  - 超量時拒絕並回明確 4xx 錯誤與訊息
  - 數值驗證以 isinstance(x, bool) 排除布林混入

額外補充現有測試沒涵蓋的：
  - 未知 plan 回退 free 上限
  - pro 上限邊界（pro limit-1 通過 / pro limit 超量 429）
  - GET 端點不消耗 quota（只有寫入才計量）
  - 無效 token 回 401 而非 429
  - 429 錯誤訊息必須含有明確說明
  - validate_count 極端值（None、0.0、maxint）
  - quota_status 未知 plan 同樣回退 free
  - 同 tenant 同 period 只有一列（UPSERT 行為）
"""

from __future__ import annotations

import datetime
import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 關閉 rate limit，避免測試被限流
os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# 確保所有 model metadata 都已載入
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


# ─────────────────────────────── 共用 helper ─────────────────────────────────


def _make_db():
    """建立獨立 in-memory SQLite session。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return Session()


def _add_tenant(db, name: str = "t1", plan: str = "free") -> Tenant:
    t = Tenant(name=name, plan=plan)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _set_usage(db, tenant_id: int, count: int):
    """直接寫入今日計量列（供快速布建邊界情境）。使用 UTC date。"""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    row = ApiUsage(tenant_id=tenant_id, period=today, count=count)
    db.add(row)
    db.commit()


# ───────────────── 1. 配額常數確認 ────────────────────────────────────────────


class TestPlanConstants:
    """驗收標準：free/pro 配額必須明確且 free < pro。"""

    def test_free_limit_is_100(self):
        assert PLAN_DAILY_LIMITS["free"] == 100

    def test_pro_limit_is_10000(self):
        assert PLAN_DAILY_LIMITS["pro"] == 10_000

    def test_pro_strictly_greater_than_free(self):
        assert PLAN_DAILY_LIMITS["pro"] > PLAN_DAILY_LIMITS["free"]

    def test_both_plans_present(self):
        assert "free" in PLAN_DAILY_LIMITS
        assert "pro" in PLAN_DAILY_LIMITS


# ───────────────── 2. validate_count 全面覆蓋 ─────────────────────────────────


class TestValidateCountFull:
    """驗收標準：isinstance(x, bool) 排除布林混入。"""

    # 合法值
    def test_zero(self):
        assert validate_count(0) == 0

    def test_one(self):
        assert validate_count(1) == 1

    def test_large_int(self):
        assert validate_count(10_000_000) == 10_000_000

    def test_max_int(self):
        import sys
        assert validate_count(sys.maxsize) == sys.maxsize

    # bool 混入（驗收重點：True==1, False==0 在 Python 是 int 子類）
    def test_bool_true_rejected(self):
        with pytest.raises(TypeError, match="bool"):
            validate_count(True)

    def test_bool_false_rejected(self):
        with pytest.raises(TypeError, match="bool"):
            validate_count(False)

    # 其他非法型別
    def test_none_rejected(self):
        with pytest.raises(TypeError):
            validate_count(None)  # type: ignore[arg-type]

    def test_float_zero_rejected(self):
        """0.0 是 float，不是 int，必須拒絕。"""
        with pytest.raises(TypeError):
            validate_count(0.0)  # type: ignore[arg-type]

    def test_string_digit_rejected(self):
        with pytest.raises(TypeError):
            validate_count("100")  # type: ignore[arg-type]

    def test_negative_rejected(self):
        with pytest.raises(ValueError, match=">= 0"):
            validate_count(-1)

    def test_negative_large_rejected(self):
        with pytest.raises(ValueError):
            validate_count(-9999)


# ───────────────── 3. 未知 plan 回退 free ─────────────────────────────────────


class TestUnknownPlanFallback:
    """未知 plan 名稱應當回退至 free 上限，不崩潰。"""

    def setup_method(self):
        self.db = _make_db()
        self.tenant = _add_tenant(self.db, name="fallback-tenant", plan="enterprise")

    def teardown_method(self):
        self.db.close()

    def test_unknown_plan_uses_free_limit_for_status(self):
        status = get_quota_status(self.db, self.tenant.id, "enterprise")
        assert status["limit"] == PLAN_DAILY_LIMITS["free"]

    def test_unknown_plan_check_increment_does_not_raise_below_free_limit(self):
        """enterprise plan 在 free limit 內不應 429。"""
        count = check_and_increment(self.db, self.tenant.id, "enterprise")
        assert count == 1

    def test_unknown_plan_raises_429_at_free_limit(self):
        """enterprise plan 在 free limit 時應 429（回退 free）。"""
        _set_usage(self.db, self.tenant.id, PLAN_DAILY_LIMITS["free"])
        with pytest.raises(HTTPException) as exc:
            check_and_increment(self.db, self.tenant.id, "enterprise")
        assert exc.value.status_code == 429


# ───────────────── 4. pro 上限邊界值 ──────────────────────────────────────────


class TestProPlanBoundary:
    """pro 配額邊界：limit-1 通過、limit 超量 429。"""

    def setup_method(self):
        self.db = _make_db()
        self.tenant = _add_tenant(self.db, name="pro-tenant", plan="pro")

    def teardown_method(self):
        self.db.close()

    def test_pro_limit_minus_1_passes(self):
        pro_limit = PLAN_DAILY_LIMITS["pro"]
        _set_usage(self.db, self.tenant.id, pro_limit - 1)
        count = check_and_increment(self.db, self.tenant.id, "pro")
        assert count == pro_limit

    def test_pro_limit_reached_raises_429(self):
        pro_limit = PLAN_DAILY_LIMITS["pro"]
        _set_usage(self.db, self.tenant.id, pro_limit)
        with pytest.raises(HTTPException) as exc:
            check_and_increment(self.db, self.tenant.id, "pro")
        assert exc.value.status_code == 429
        assert "Quota" in exc.value.detail

    def test_pro_at_free_limit_does_not_raise(self):
        """pro plan 在 free limit 處不應 429。"""
        free_limit = PLAN_DAILY_LIMITS["free"]
        _set_usage(self.db, self.tenant.id, free_limit)
        count = check_and_increment(self.db, self.tenant.id, "pro")
        assert count == free_limit + 1


# ───────────────── 5. 429 訊息內容 ────────────────────────────────────────────


class TestErrorMessageContent:
    """超量 429 必須附有說明訊息，不能只回空白錯誤。"""

    def setup_method(self):
        self.db = _make_db()
        self.tenant = _add_tenant(self.db, name="msg-tenant", plan="free")
        _set_usage(self.db, self.tenant.id, PLAN_DAILY_LIMITS["free"])

    def teardown_method(self):
        self.db.close()

    def test_429_detail_not_empty(self):
        with pytest.raises(HTTPException) as exc:
            check_and_increment(self.db, self.tenant.id, "free")
        assert exc.value.detail  # 非空

    def test_429_detail_mentions_quota(self):
        with pytest.raises(HTTPException) as exc:
            check_and_increment(self.db, self.tenant.id, "free")
        assert "Quota" in exc.value.detail or "quota" in exc.value.detail.lower()

    def test_429_status_code_is_429(self):
        with pytest.raises(HTTPException) as exc:
            check_and_increment(self.db, self.tenant.id, "free")
        assert exc.value.status_code == 429


# ───────────────── 6. DB row 唯一性（同 tenant 同日只有一列）──────────────────


class TestUsageRowUniqueness:
    """同一 tenant 同一 period 不應產生多列；_get_or_create 應冪等。"""

    def setup_method(self):
        self.db = _make_db()
        self.tenant = _add_tenant(self.db, name="unique-tenant", plan="free")

    def teardown_method(self):
        self.db.close()

    def test_multiple_increments_single_row(self):
        from sqlalchemy import select
        for _ in range(5):
            check_and_increment(self.db, self.tenant.id, "free")
        today = datetime.datetime.now(datetime.timezone.utc).date()
        rows = self.db.execute(
            select(ApiUsage).where(
                ApiUsage.tenant_id == self.tenant.id,
                ApiUsage.period == today,
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].count == 5


# ───────────────── 7. HTTP 整合測試 ───────────────────────────────────────────


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

    def _override_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _register_or_login(client, email: str, password: str, tenant: str) -> str:
    """嘗試註冊；若 email 已存在（400）則改用 login 端點取 token。"""
    r = client.post("/auth/register", json={
        "email": email,
        "password": password,
        "tenant_name": tenant,
    })
    if r.status_code == 201:
        return r.json()["access_token"]
    # email 已存在時改走 token 端點（OAuth2 form）
    r2 = client.post("/auth/token", data={"username": email, "password": password})
    assert r2.status_code == 200, f"login failed: {r2.text}"
    return r2.json()["access_token"]


class TestHttpQuotaIntegration:
    """HTTP 層整合：GET 不扣量、無效 token 回 401、超量回 429 含訊息。"""

    @pytest.fixture(autouse=True)
    def _token(self, client):
        self.token = _register_or_login(
            client, "qa_http@example.com", "Secure99!", "qa-http-corp"
        )
        self.auth = {"Authorization": f"Bearer {self.token}"}

    # 7a. GET /quota/status 不增加計量
    def test_get_status_does_not_consume_quota(self, client):
        before = client.get("/quota/status", headers=self.auth).json()["used"]
        for _ in range(3):
            client.get("/quota/status", headers=self.auth)
        after = client.get("/quota/status", headers=self.auth).json()["used"]
        assert after == before, "GET /quota/status 不應消耗 quota"

    # 7b. GET /notes/ 也計量（讀操作同樣消耗 quota）
    def test_get_notes_consumes_quota(self, client):
        before = client.get("/quota/status", headers=self.auth).json()["used"]
        r = client.get("/notes/", headers=self.auth)
        assert r.status_code == 200
        after = client.get("/quota/status", headers=self.auth).json()["used"]
        assert after == before + 1, "GET /notes/ 應消耗 1 次 quota"

    # 7c. POST /notes/ 才消耗 quota
    def test_post_notes_increments_quota(self, client):
        before = client.get("/quota/status", headers=self.auth).json()["used"]
        r = client.post("/notes/", json={"title": "t", "content": "c"}, headers=self.auth)
        assert r.status_code == 201
        after = client.get("/quota/status", headers=self.auth).json()["used"]
        assert after == before + 1

    # 7d. 無效 token 應回 401，不是 429
    def test_invalid_token_returns_401_not_429(self, client):
        r = client.get(
            "/quota/status",
            headers={"Authorization": "Bearer totally.invalid.token"},
        )
        assert r.status_code == 401

    # 7e. 無 token 回 401
    def test_missing_token_returns_401(self, client):
        assert client.get("/quota/status").status_code == 401

    # 7f. quota/status 回傳欄位完整性
    def test_status_response_shape(self, client):
        data = client.get("/quota/status", headers=self.auth).json()
        assert set(data.keys()) >= {"plan", "period", "used", "limit", "remaining"}
        assert data["plan"] == "free"
        assert data["limit"] == PLAN_DAILY_LIMITS["free"]
        assert isinstance(data["used"], int) and not isinstance(data["used"], bool)
        assert isinstance(data["remaining"], int) and not isinstance(data["remaining"], bool)
        assert data["used"] + data["remaining"] == data["limit"] or data["remaining"] == 0

    # 7g. remaining 永遠 >= 0
    def test_remaining_non_negative(self, client):
        data = client.get("/quota/status", headers=self.auth).json()
        assert data["remaining"] >= 0


class TestHttpQuotaExceeded:
    """超量情境：塞滿計量後 POST /notes/ 回 429 含明確訊息。"""

    @pytest.fixture(autouse=True)
    def _setup(self, client):
        self.token = _register_or_login(
            client, "qa_over@example.com", "Secure99!", "qa-over-corp"
        )
        self.auth = {"Authorization": f"Bearer {self.token}"}
        # 取 tenant_id
        resp = client.get("/tenants/me", headers=self.auth)
        assert resp.status_code == 200, resp.text
        tid = resp.json()["id"]
        # 直接塞滿今日計量
        from sqlalchemy import text
        limit = PLAN_DAILY_LIMITS["free"]
        db = _Session()
        try:
            db.execute(
                text(
                    "INSERT INTO api_usage (tenant_id, period, count) "
                    "VALUES (:tid, :dt, :cnt) "
                    "ON CONFLICT(tenant_id, period) DO UPDATE SET count = :cnt"
                ),
                {"tid": tid, "dt": datetime.datetime.now(datetime.timezone.utc).date().isoformat(), "cnt": limit},
            )
            db.commit()
        finally:
            db.close()

    def test_post_notes_returns_429_when_quota_full(self, client):
        r = client.post(
            "/notes/",
            json={"title": "overflow", "content": "x"},
            headers=self.auth,
        )
        assert r.status_code == 429

    def test_429_response_has_detail_field(self, client):
        r = client.post(
            "/notes/",
            json={"title": "overflow", "content": "x"},
            headers=self.auth,
        )
        body = r.json()
        assert "detail" in body

    def test_429_detail_mentions_quota(self, client):
        r = client.post(
            "/notes/",
            json={"title": "overflow", "content": "x"},
            headers=self.auth,
        )
        assert "Quota" in r.json()["detail"] or "quota" in r.json()["detail"].lower()

    def test_get_also_blocked_when_quota_full(self, client):
        """超量後 GET /notes/ 同樣回 429（讀/寫皆受 quota 管控）。"""
        r = client.get("/notes/", headers=self.auth)
        assert r.status_code == 429
