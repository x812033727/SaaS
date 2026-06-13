"""Task #2 驗收測試：帳單升降級流程。

覆蓋範圍
--------
1. checkout：可設定任意 plan，回 payment_id 前綴 "simulated_"
2. upgrade：free → pro 成功；PlanChangeHistory 有記錄
3. upgrade：同方案 / 降向 → 400
4. downgrade：pro → free 且今日用量 = 0 → 成功
5. downgrade：pro → free 且今日用量 > free 上限 → 409 + current_usage/new_limit
6. 降級不刪歷史用量（count 仍保留）
7. 切換後 check_and_increment 立即套用新 limit

全部離線，in-memory SQLite。
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
from saas_mvp.models import api_key as _ak, api_key_usage as _aku  # noqa: F401
from saas_mvp.models import plan_change_history as _pch  # noqa: F401
from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.models.usage import ApiUsage
from saas_mvp.models.plan_change_history import PlanChangeHistory
from saas_mvp.quota import PLAN_DAILY_LIMITS

# ── In-memory SQLite ──────────────────────────────────────────

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


# ── helpers ───────────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register(client: TestClient, tenant: str | None = None) -> tuple[str, str]:
    """回傳 (email, token)"""
    email = f"user_{_uid()}@example.com"
    tn = tenant or f"tenant_{_uid()}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn,
    })
    assert r.status_code == 201, r.text
    return email, r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Tests ─────────────────────────────────────────────────────

def test_checkout_set_plan(client: TestClient):
    """checkout 可設定 pro plan，回傳 simulated_ payment_id。"""
    _, token = _register(client)
    r = client.post("/billing/checkout", json={"plan": "pro"}, headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["plan"] == "pro"
    assert body["payment_id"].startswith("simulated_"), body["payment_id"]


def test_checkout_invalid_plan(client: TestClient):
    """checkout 傳入不存在 plan → 400。"""
    _, token = _register(client)
    r = client.post("/billing/checkout", json={"plan": "enterprise"}, headers=_auth(token))
    assert r.status_code == 400, r.text


def test_upgrade_free_to_pro(client: TestClient):
    """free → pro 升級成功，PlanChangeHistory 有記錄。"""
    _, token = _register(client)
    r = client.post("/billing/upgrade", json={"plan": "pro"}, headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"] == "pro"
    assert body["payment_id"].startswith("simulated_")

    # 確認 /tenants/me 反映新 plan
    me = client.get("/tenants/me", headers=_auth(token))
    assert me.json()["plan"] == "pro"


def test_upgrade_writes_plan_change_history(client: TestClient):
    """升級後 PlanChangeHistory 有一筆 free→pro 記錄。"""
    _, token = _register(client)
    client.post("/billing/upgrade", json={"plan": "pro"}, headers=_auth(token))

    # 從 DB 直接查歷程表
    db = _Session()
    try:
        # 找最新一筆
        row = db.execute(
            select(PlanChangeHistory).order_by(PlanChangeHistory.id.desc())
        ).scalars().first()
        assert row is not None
        assert row.from_plan == "free"
        assert row.to_plan == "pro"
        assert row.changed_by_user_id is not None
    finally:
        db.close()


def test_upgrade_same_plan_rejected(client: TestClient):
    """已是 pro 再 upgrade pro → 400。"""
    _, token = _register(client)
    client.post("/billing/upgrade", json={"plan": "pro"}, headers=_auth(token))
    r = client.post("/billing/upgrade", json={"plan": "pro"}, headers=_auth(token))
    assert r.status_code == 400, r.text


def test_upgrade_downward_rejected(client: TestClient):
    """upgrade 指定較低方案 → 400（不應走降級路徑）。"""
    _, token = _register(client)
    # 先升 pro
    client.post("/billing/upgrade", json={"plan": "pro"}, headers=_auth(token))
    # 再用 upgrade 打 free → 應 400
    r = client.post("/billing/upgrade", json={"plan": "free"}, headers=_auth(token))
    assert r.status_code == 400, r.text


def test_downgrade_under_limit_succeeds(client: TestClient):
    """今日用量 = 0，pro → free 降級成功。"""
    _, token = _register(client)
    client.post("/billing/upgrade", json={"plan": "pro"}, headers=_auth(token))
    r = client.post("/billing/downgrade", json={"plan": "free"}, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "free"
    # plan 已生效
    me = client.get("/tenants/me", headers=_auth(token))
    assert me.json()["plan"] == "free"


def test_downgrade_over_limit_returns_409(client: TestClient):
    """今日用量超過 free 上限(100) → 409 + current_usage/new_limit。"""
    _, token = _register(client)
    # 先升 pro
    client.post("/billing/upgrade", json={"plan": "pro"}, headers=_auth(token))

    # 取得 tenant_id
    me = client.get("/tenants/me", headers=_auth(token))
    tenant_id = me.json()["id"]

    # 直接在 DB 塞今日用量 = 101（超過 free 限額 100）
    db = _Session()
    today = datetime.date.today()
    try:
        existing = db.execute(
            select(ApiUsage).where(
                ApiUsage.tenant_id == tenant_id,
                ApiUsage.period == today,
            )
        ).scalar_one_or_none()
        if existing:
            existing.count = 101
        else:
            db.add(ApiUsage(tenant_id=tenant_id, period=today, count=101))
        db.commit()
    finally:
        db.close()

    r = client.post("/billing/downgrade", json={"plan": "free"}, headers=_auth(token))
    assert r.status_code == 409, r.text
    body = r.json()["detail"]
    assert body["current_usage"] == 101
    assert body["new_limit"] == PLAN_DAILY_LIMITS["free"]  # 100

    # plan 不應被改（仍是 pro）
    me2 = client.get("/tenants/me", headers=_auth(token))
    assert me2.json()["plan"] == "pro"


def test_downgrade_409_does_not_delete_usage(client: TestClient):
    """降級失敗（409）後歷史用量應保留不刪除。"""
    _, token = _register(client)
    client.post("/billing/upgrade", json={"plan": "pro"}, headers=_auth(token))
    me = client.get("/tenants/me", headers=_auth(token))
    tenant_id = me.json()["id"]

    db = _Session()
    today = datetime.date.today()
    try:
        db.add(ApiUsage(tenant_id=tenant_id, period=today, count=200))
        db.commit()
    finally:
        db.close()

    r = client.post("/billing/downgrade", json={"plan": "free"}, headers=_auth(token))
    assert r.status_code == 409

    # 驗證用量仍在
    db2 = _Session()
    try:
        row = db2.execute(
            select(ApiUsage).where(
                ApiUsage.tenant_id == tenant_id,
                ApiUsage.period == today,
            )
        ).scalar_one_or_none()
        assert row is not None
        assert row.count == 200
    finally:
        db2.close()


def test_downgrade_upward_rejected(client: TestClient):
    """downgrade 指定較高方案 → 400。"""
    _, token = _register(client)
    r = client.post("/billing/downgrade", json={"plan": "pro"}, headers=_auth(token))
    assert r.status_code == 400, r.text


def test_plan_change_takes_effect_on_quota(client: TestClient):
    """升級 pro 後 quota 立即套用新 limit（可累積超過 free 100 次）。"""
    _, token = _register(client)
    me = client.get("/tenants/me", headers=_auth(token))
    tenant_id = me.json()["id"]

    # 先升 pro
    client.post("/billing/upgrade", json={"plan": "pro"}, headers=_auth(token))

    # 把 DB 的今日用量設成 101（超 free 但在 pro 範圍內）
    db = _Session()
    today = datetime.date.today()
    try:
        existing = db.execute(
            select(ApiUsage).where(
                ApiUsage.tenant_id == tenant_id,
                ApiUsage.period == today,
            )
        ).scalar_one_or_none()
        if existing:
            existing.count = 101
        else:
            db.add(ApiUsage(tenant_id=tenant_id, period=today, count=101))
        db.commit()
    finally:
        db.close()

    # 查 /quota/status 確認 limit 已是 pro 的 10000
    r = client.get("/quota/status", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["limit"] == PLAN_DAILY_LIMITS["pro"]   # 10000
    assert body["used"] == 101
