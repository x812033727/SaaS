"""R4-C3 — POS REST checkout 補欄位(staff/payment_method/tip/mark_paid)。"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.product import Product  # noqa: E402
from saas_mvp.models.staff import Staff  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "features_default_enabled", True)
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c


def _setup(client) -> tuple[dict[str, str], int, int]:
    """回 (headers, product_id, staff_id)。"""
    r = client.post("/auth/register", json={
        "email": f"pos_{uuid.uuid4().hex[:8]}@x.tw",
        "password": "Test1234!",
        "tenant_name": f"pos_{uuid.uuid4().hex[:8]}",
    })
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    tid = client.get("/tenants/me", headers=headers).json()["id"]
    db = _Session()
    try:
        p = Product(tenant_id=tid, name="洗剪吹", price_cents=80000, stock=10, is_active=True)
        s = Staff(tenant_id=tid, name="Amy", is_active=True)
        db.add_all([p, s])
        db.commit()
        return headers, p.id, s.id
    finally:
        db.close()


def test_checkout_with_staff_and_mark_paid(client):
    headers, pid, sid = _setup(client)
    r = client.post("/booking/pos/checkout", json={
        "items": [{"product_id": pid, "qty": 1}],
        "staff_id": sid,
        "payment_method": "cash",
        "mark_paid": True,
    }, headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "paid"
    assert body["total_cents"] == 80000


def test_tip_without_staff_409_not_500(client):
    """R4-C3 修 bug:StaffRequired 原本未接會冒泡 500。"""
    headers, pid, _ = _setup(client)
    r = client.post("/booking/pos/checkout", json={
        "items": [{"product_id": pid, "qty": 1}],
        "tip_cents": 10000,
        "payment_method": "cash",
        "mark_paid": True,
    }, headers=headers)
    assert r.status_code == 409
    assert "員工" in r.json()["detail"] or "staff" in r.json()["detail"].lower()


def test_unknown_staff_404_not_500(client):
    headers, pid, _ = _setup(client)
    r = client.post("/booking/pos/checkout", json={
        "items": [{"product_id": pid, "qty": 1}],
        "staff_id": 999999,
        "payment_method": "cash",
        "mark_paid": True,
    }, headers=headers)
    assert r.status_code == 404
    assert "Staff" in r.json()["detail"]


def test_omitting_new_fields_keeps_old_behavior(client):
    headers, pid, _ = _setup(client)
    r = client.post("/booking/pos/checkout", json={
        "items": [{"product_id": pid, "qty": 2}],
    }, headers=headers)
    assert r.status_code == 201
    assert r.json()["status"] == "pending"  # 未 mark_paid 維持舊行為
