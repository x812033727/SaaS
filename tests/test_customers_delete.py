"""顧客刪除（delete_customer）測試 — 逐表驗證關聯處理 + 跨租戶 404 + REST。

SQLite 未開 PRAGMA foreign_keys，FK ondelete 不會自動執行；
delete_customer 必須在應用層明做：
  SET NULL：reservations / line_messages / coupon_redemptions / orders
  DELETE  ：point_transactions / customer_tag_links / campaign_sends
"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
from saas_mvp.models import point_transaction as _pt  # noqa: F401,E402
from saas_mvp.models import customer_tag as _ct, customer_tag_link as _ctl  # noqa: F401,E402
from saas_mvp.models import campaign as _cp, campaign_send as _cs  # noqa: F401,E402
from saas_mvp.models import coupon as _co, coupon_redemption as _cr  # noqa: F401,E402
from saas_mvp.models import order as _o, order_item as _oi  # noqa: F401,E402
from saas_mvp.models import line_message as _lm  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.auth.security import decode_access_token  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.campaign import Campaign  # noqa: E402
from saas_mvp.models.campaign_send import CampaignSend  # noqa: E402
from saas_mvp.models.coupon import Coupon  # noqa: E402
from saas_mvp.models.coupon_redemption import CouponRedemption  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.customer_tag import CustomerTag  # noqa: E402
from saas_mvp.models.customer_tag_link import CustomerTagLink  # noqa: E402
from saas_mvp.models.line_message import LineMessage  # noqa: E402
from saas_mvp.models.order import Order  # noqa: E402
from saas_mvp.models.point_transaction import PointTransaction  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.services import customers as customers_svc  # noqa: E402

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


def _register(client) -> str:
    r = client.post("/auth/register", json={
        "email": f"u_{uuid.uuid4().hex[:8]}@example.com",
        "password": "Test1234!",
        "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _tenant_id_of(token: str) -> int:
    db = _Session()
    try:
        payload = decode_access_token(token)
        return db.query(User).filter(User.id == int(payload["sub"])).first().tenant_id
    finally:
        db.close()


def _seed_full_customer(tid: int) -> dict:
    """建一位顧客 + 全部 7 種關聯列，回傳各 id。"""
    db = _Session()
    try:
        cust = Customer(tenant_id=tid, line_user_id=f"U{uuid.uuid4().hex[:12]}")
        db.add(cust)
        db.flush()
        slot = BookingSlot(
            tenant_id=tid,
            slot_start=datetime.datetime(2030, 1, 1, 10, 0),
            max_capacity=5,
        )
        db.add(slot)
        db.flush()
        resv = Reservation(
            tenant_id=tid, slot_id=slot.id, customer_id=cust.id, party_size=1,
        )
        msg = LineMessage(
            tenant_id=tid, line_user_id=cust.line_user_id,
            customer_id=cust.id, direction="in", text="hi",
        )
        order = Order(tenant_id=tid, customer_id=cust.id)
        coupon = Coupon(
            tenant_id=tid, code=f"C{uuid.uuid4().hex[:6]}", name="券",
            discount_type="amount", discount_value=10,
        )
        campaign = Campaign(tenant_id=tid, name="活動", type="broadcast",
                            message_template="hi")
        db.add_all([resv, msg, order, coupon, campaign])
        db.flush()
        redemption = CouponRedemption(
            tenant_id=tid, coupon_id=coupon.id, customer_id=cust.id,
            line_user_id=cust.line_user_id,
        )
        points = PointTransaction(
            tenant_id=tid, customer_id=cust.id, delta=10, reason="test",
        )
        tag = CustomerTag(tenant_id=tid, name=f"tag{uuid.uuid4().hex[:6]}")
        db.add_all([redemption, points, tag])
        db.flush()
        link = CustomerTagLink(tenant_id=tid, customer_id=cust.id, tag_id=tag.id)
        send = CampaignSend(
            tenant_id=tid, campaign_id=campaign.id, customer_id=cust.id,
            period_key="once",
        )
        db.add_all([link, send])
        db.commit()
        return {
            "customer_id": cust.id,
            "reservation_id": resv.id,
            "message_id": msg.id,
            "order_id": order.id,
            "redemption_id": redemption.id,
            "points_id": points.id,
            "tag_id": tag.id,
            "send_id": send.id,
        }
    finally:
        db.close()


class TestDeleteCustomer:
    def test_detach_and_purge_all_tables(self, client):
        tid = _tenant_id_of(_register(client))
        ids = _seed_full_customer(tid)
        db = _Session()
        try:
            customers_svc.delete_customer(
                db, tenant_id=tid, customer_id=ids["customer_id"]
            )
            # 顧客本體刪除
            assert db.query(Customer).filter(
                Customer.id == ids["customer_id"]).first() is None
            # SET NULL 四表：列還在、customer_id 已去識別化
            for model, key in (
                (Reservation, "reservation_id"),
                (LineMessage, "message_id"),
                (Order, "order_id"),
                (CouponRedemption, "redemption_id"),
            ):
                row = db.query(model).filter(model.id == ids[key]).first()
                assert row is not None, f"{model.__name__} 歷史列不應被刪"
                assert row.customer_id is None, f"{model.__name__} 應去識別化"
            # DELETE 三表：列一併刪除
            for model, key in (
                (PointTransaction, "points_id"),
                (CustomerTagLink, None),
                (CampaignSend, "send_id"),
            ):
                q = db.query(model)
                if key:
                    q = q.filter(model.id == ids[key])
                else:
                    q = q.filter(model.customer_id == ids["customer_id"])
                assert q.first() is None, f"{model.__name__} 應連同刪除"
            # 標籤本身保留（只刪掛載）
            assert db.query(CustomerTag).filter(
                CustomerTag.id == ids["tag_id"]).first() is not None
        finally:
            db.close()

    def test_cross_tenant_404(self, client):
        tid_a = _tenant_id_of(_register(client))
        tid_b = _tenant_id_of(_register(client))
        ids = _seed_full_customer(tid_a)
        db = _Session()
        try:
            with pytest.raises(HTTPException) as exc:
                customers_svc.delete_customer(
                    db, tenant_id=tid_b, customer_id=ids["customer_id"]
                )
            assert exc.value.status_code == 404
        finally:
            db.close()

    def test_rest_delete(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        ids = _seed_full_customer(tid)
        r = client.delete(
            f"/booking/customers/{ids['customer_id']}", headers=_auth(token)
        )
        assert r.status_code == 204
        assert client.get(
            f"/booking/customers/{ids['customer_id']}", headers=_auth(token)
        ).status_code == 404
        # 再刪一次 → 404
        assert client.delete(
            f"/booking/customers/{ids['customer_id']}", headers=_auth(token)
        ).status_code == 404
