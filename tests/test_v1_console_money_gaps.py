"""R12-C1 — console 金流能力補齊:訂單列表/退款、定金退款、候補管理。

/ui 退役後這些能力原本只存在於被重導的 /ui 頁(能力缺口審計);
本檔驗證 console JSON 端點鏡像原語意:owner 限定、audit、部分退款、
租戶隔離、服務層錯誤 → 422。
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.auth.security import create_access_token
from saas_mvp.db import Base, get_db
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.booking_waitlist import WaitlistEntry
from saas_mvp.models.order import Order
from saas_mvp.models.reservation import Reservation
from saas_mvp.models.user import User
from saas_mvp.services import features as features_svc

_SLOT_START = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def v1_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)
    app = create_app()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, session_factory


def _register(client: TestClient, prefix: str = "mg") -> tuple[int, dict[str, str]]:
    unique = uuid.uuid4().hex[:8]
    r = client.post(
        "/auth/register",
        json={
            "email": f"{prefix}-{unique}@example.com",
            "password": "safe-password-123",
            "tenant_name": f"{prefix}-{unique}",
        },
    )
    assert r.status_code == 201, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ctx = client.get("/api/v1/context", headers=headers).json()
    return ctx["tenant"]["id"], headers


def _staff_headers(session_factory, tenant_id: int) -> dict[str, str]:
    """建一個非 owner 成員,回其 Bearer headers。"""
    db = session_factory()
    try:
        u = User(
            email=f"staff-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            tenant_id=tenant_id,
            role="staff",
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        token = create_access_token(u.id, tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        db.close()


def _enable(session_factory, tenant_id: int, feature: str) -> None:
    db = session_factory()
    try:
        features_svc.set_enabled(
            db, tenant_id, feature, True, actor_user_id=None, source="test"
        )
        db.commit()
    finally:
        db.close()


def _paid_order(session_factory, tenant_id: int, *, total=50000) -> int:
    db = session_factory()
    try:
        o = Order(
            tenant_id=tenant_id, total_cents=total, status="paid",
            payment_provider="stub",
        )
        db.add(o)
        db.commit()
        return o.id
    finally:
        db.close()


class TestOrders:
    def test_list_orders(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.PRODUCT_SALES)
        _paid_order(sf, tid)
        r = client.get("/api/v1/orders", headers=headers)
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.headers["X-Total-Count"] == "1"

    def test_list_tenant_isolated(self, v1_client):
        client, sf = v1_client
        tid_a, headers_a = _register(client, "a")
        tid_b, _ = _register(client, "b")
        _enable(sf, tid_a, features_svc.PRODUCT_SALES)
        _paid_order(sf, tid_b)
        r = client.get("/api/v1/orders", headers=headers_a)
        assert r.status_code == 200 and r.json() == []

    def test_refund_full_stub(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.PRODUCT_SALES)
        oid = _paid_order(sf, tid)
        r = client.post(f"/api/v1/orders/{oid}/refund", json={}, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["refunded_cents"] == 50000
        assert body["refund_status"] == "refunded"

    def test_refund_partial(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.PRODUCT_SALES)
        oid = _paid_order(sf, tid)
        r = client.post(
            f"/api/v1/orders/{oid}/refund",
            json={"amount_twd": 100},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["refunded_cents"] == 10000

    def test_refund_requires_owner(self, v1_client):
        client, sf = v1_client
        tid, _headers = _register(client)
        _enable(sf, tid, features_svc.PRODUCT_SALES)
        oid = _paid_order(sf, tid)
        staff = _staff_headers(sf, tid)
        r = client.post(f"/api/v1/orders/{oid}/refund", json={}, headers=staff)
        assert r.status_code == 403

    def test_refund_cross_tenant_422(self, v1_client):
        client, sf = v1_client
        tid_a, headers_a = _register(client, "a")
        tid_b, _ = _register(client, "b")
        _enable(sf, tid_a, features_svc.PRODUCT_SALES)
        oid_b = _paid_order(sf, tid_b)
        r = client.post(
            f"/api/v1/orders/{oid_b}/refund", json={}, headers=headers_a
        )
        assert r.status_code == 422

    def test_unpaid_order_422(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.PRODUCT_SALES)
        db = sf()
        try:
            o = Order(tenant_id=tid, total_cents=1000, status="pending")
            db.add(o)
            db.commit()
            oid = o.id
        finally:
            db.close()
        r = client.post(f"/api/v1/orders/{oid}/refund", json={}, headers=headers)
        assert r.status_code == 422

    def test_manual_refund_needs_note(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.PRODUCT_SALES)
        oid = _paid_order(sf, tid)
        r = client.post(
            f"/api/v1/orders/{oid}/refund/manual", json={}, headers=headers
        )
        assert r.status_code == 422  # note 必填
        r2 = client.post(
            f"/api/v1/orders/{oid}/refund/manual",
            json={"note": "已在綠界後台退款"},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["refund_status"] == "refunded"


def _cancelled_deposit_resv(session_factory, tenant_id: int) -> int:
    db = session_factory()
    try:
        slot = BookingSlot(
            tenant_id=tenant_id, slot_start=_SLOT_START, max_capacity=4
        )
        db.add(slot)
        db.flush()
        resv = Reservation(
            tenant_id=tenant_id,
            slot_id=slot.id,
            party_size=1,
            status="cancelled",
            deposit_status="paid",
            deposit_cents=20000,
            deposit_merchant_trade_no=f"DEP{uuid.uuid4().hex[:12]}",
            deposit_provider="stub",
        )
        db.add(resv)
        db.commit()
        return resv.id
    finally:
        db.close()


class TestDepositRefund:
    def test_refund_full(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        rid = _cancelled_deposit_resv(sf, tid)
        r = client.post(
            f"/api/v1/reservations/{rid}/deposit-refund", json={}, headers=headers
        )
        assert r.status_code == 200, r.text
        assert r.json()["deposit_status"] == "refunded"

    def test_requires_owner(self, v1_client):
        client, sf = v1_client
        tid, _ = _register(client)
        rid = _cancelled_deposit_resv(sf, tid)
        staff = _staff_headers(sf, tid)
        r = client.post(
            f"/api/v1/reservations/{rid}/deposit-refund", json={}, headers=staff
        )
        assert r.status_code == 403

    def test_not_cancelled_422(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        db = sf()
        try:
            slot = BookingSlot(
                tenant_id=tid, slot_start=_SLOT_START, max_capacity=4
            )
            db.add(slot)
            db.flush()
            resv = Reservation(
                tenant_id=tid, slot_id=slot.id, party_size=1,
                status="confirmed", deposit_status="paid", deposit_cents=20000,
            )
            db.add(resv)
            db.commit()
            rid = resv.id
        finally:
            db.close()
        r = client.post(
            f"/api/v1/reservations/{rid}/deposit-refund", json={}, headers=headers
        )
        assert r.status_code == 422

    def test_manual_confirm(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        rid = _cancelled_deposit_resv(sf, tid)
        r = client.post(
            f"/api/v1/reservations/{rid}/deposit-refund/manual",
            json={"note": "已在金流後台退款"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["deposit_status"] == "refunded"

    def test_cross_tenant_422(self, v1_client):
        client, sf = v1_client
        tid_a, headers_a = _register(client, "a")
        tid_b, _ = _register(client, "b")
        rid_b = _cancelled_deposit_resv(sf, tid_b)
        r = client.post(
            f"/api/v1/reservations/{rid_b}/deposit-refund",
            json={},
            headers=headers_a,
        )
        assert r.status_code == 422


class TestWaitlist:
    def _entry(self, sf, tenant_id: int) -> int:
        db = sf()
        try:
            slot = BookingSlot(
                tenant_id=tenant_id, slot_start=_SLOT_START, max_capacity=1
            )
            db.add(slot)
            db.flush()
            e = WaitlistEntry(
                tenant_id=tenant_id, slot_id=slot.id,
                line_user_id="Uwl1", display_name="候補客",
                party_size=2, status="waiting",
            )
            db.add(e)
            db.commit()
            return e.id
        finally:
            db.close()

    def test_list(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        self._entry(sf, tid)
        r = client.get("/api/v1/waitlist", headers=headers)
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["display_name"] == "候補客"
        assert rows[0]["slot_start"] is not None

    def test_list_tenant_isolated(self, v1_client):
        client, sf = v1_client
        tid_a, headers_a = _register(client, "a")
        tid_b, _ = _register(client, "b")
        self._entry(sf, tid_b)
        r = client.get("/api/v1/waitlist", headers=headers_a)
        assert r.status_code == 200 and r.json() == []

    def test_cancel(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        eid = self._entry(sf, tid)
        r = client.post(f"/api/v1/waitlist/{eid}/cancel", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cancelled"

    def test_cancel_cross_tenant_404(self, v1_client):
        client, sf = v1_client
        tid_a, headers_a = _register(client, "a")
        tid_b, _ = _register(client, "b")
        eid_b = self._entry(sf, tid_b)
        r = client.post(f"/api/v1/waitlist/{eid_b}/cancel", headers=headers_a)
        assert r.status_code == 404
