"""/ui/customers 顧客 CRM 頁測試。

涵蓋：列表+搜尋+分頁、detail 編輯 phone/note、標籤建立/整批同步、
點數調整（含扣點不足）、預約歷史顯示、跨租戶 404、未登入重導。
"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
from saas_mvp.models import customer_tag as _ct, customer_tag_link as _ctl  # noqa: F401,E402
from saas_mvp.models import point_transaction as _pt  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

Base.metadata.create_all(bind=_engine)
_app = create_app()


def _override_get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


_app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture()
def client():
    with TestClient(_app, raise_server_exceptions=True) as c:
        yield c


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register_and_login(client) -> int:
    """API 註冊取 tenant_id，再走 /ui/login 取得 cookie session。"""
    email = f"cust_{_uid()}@example.com"
    password = "Test1234!"
    r = client.post("/auth/register", json={
        "email": email, "password": password,
        "tenant_name": f"cust_t_{_uid()}",
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    tid = client.get(
        "/tenants/me", headers={"Authorization": f"Bearer {token}"}
    ).json()["id"]
    r2 = client.post(
        "/ui/login", data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert r2.status_code == 303, r2.text
    return tid


def _seed_customer(tid: int, *, name="王小明", phone=None) -> int:
    db = _Session()
    try:
        c = Customer(
            tenant_id=tid,
            line_user_id=f"U{uuid.uuid4().hex}",
            display_name=name,
            phone=phone,
        )
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


class TestListPage:
    def test_requires_login(self, client):
        r = client.get("/ui/customers", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/ui/login"

    def test_list_renders_customers(self, client):
        tid = _register_and_login(client)
        _seed_customer(tid, name="王小明")
        _seed_customer(tid, name="李大華")
        r = client.get("/ui/customers")
        assert r.status_code == 200
        assert "王小明" in r.text and "李大華" in r.text

    def test_search_filters(self, client):
        tid = _register_and_login(client)
        _seed_customer(tid, name="王小明", phone="0912345678")
        _seed_customer(tid, name="李大華")
        r = client.get("/ui/customers?q=0912")
        assert "王小明" in r.text
        assert "李大華" not in r.text

    def test_pagination(self, client):
        tid = _register_and_login(client)
        for i in range(25):
            _seed_customer(tid, name=f"客人{i}")
        r1 = client.get("/ui/customers")
        assert "第 1 / 2 頁" in r1.text
        r2 = client.get("/ui/customers?page=2")
        assert "第 2 / 2 頁" in r2.text

    def test_htmx_returns_partial(self, client):
        tid = _register_and_login(client)
        _seed_customer(tid)
        r = client.get("/ui/customers", headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert "<html" not in r.text  # partial 不含整頁骨架

    def test_cross_tenant_invisible(self, client):
        tid_a = _register_and_login(client)
        _seed_customer(tid_a, name="A租戶顧客")
        # 換帳號登入 B 租戶
        client.get("/ui/logout")
        _register_and_login(client)
        r = client.get("/ui/customers")
        assert "A租戶顧客" not in r.text


class TestDetail:
    def test_detail_renders(self, client):
        tid = _register_and_login(client)
        cid = _seed_customer(tid, name="王小明", phone="0911")
        r = client.get(f"/ui/customers/{cid}")
        assert r.status_code == 200
        assert "王小明" in r.text and "0911" in r.text

    def test_cross_tenant_404(self, client):
        tid_a = _register_and_login(client)
        cid = _seed_customer(tid_a)
        client.get("/ui/logout")
        _register_and_login(client)
        r = client.get(f"/ui/customers/{cid}")
        assert r.status_code == 404

    def test_update_phone_note(self, client):
        tid = _register_and_login(client)
        cid = _seed_customer(tid)
        r = client.post(f"/ui/customers/{cid}", data={
            "phone": "0987654321", "note": "常客,偏好下午",
        })
        assert r.status_code == 200
        assert "基本資料已更新" in r.text
        db = _Session()
        try:
            c = db.get(Customer, cid)
            assert c.phone == "0987654321"
            assert c.note == "常客,偏好下午"
        finally:
            db.close()

    def test_reservation_history_shown(self, client):
        tid = _register_and_login(client)
        cid = _seed_customer(tid)
        db = _Session()
        try:
            c = db.get(Customer, cid)
            slot = BookingSlot(
                tenant_id=tid,
                slot_start=datetime.datetime(
                    2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc
                ),
                max_capacity=4,
            )
            db.add(slot)
            db.flush()
            from saas_mvp.services import booking as booking_svc

            booking_svc.book_slot(
                db, tenant_id=tid, slot_id=slot.id, party_size=2,
                line_user_id=c.line_user_id,
            )
        finally:
            db.close()
        r = client.get(f"/ui/customers/{cid}")
        assert "2030-06-01 18:00" in r.text


class TestTags:
    def test_create_and_sync_tags(self, client):
        tid = _register_and_login(client)
        cid = _seed_customer(tid)
        # 建標籤
        r = client.post("/ui/customers/tags", data={
            "customer_id": cid, "name": "VIP", "color": "#ff6600",
        })
        assert r.status_code == 200
        assert "VIP" in r.text

        # 找出 tag id（從 checkbox value）
        db = _Session()
        try:
            from saas_mvp.models.customer_tag import CustomerTag

            tag = db.query(CustomerTag).filter(
                CustomerTag.tenant_id == tid, CustomerTag.name == "VIP"
            ).one()
            tag_id = tag.id
        finally:
            db.close()

        # 勾選 → attach
        r2 = client.post(f"/ui/customers/{cid}/tags", data={"tag_ids": [tag_id]})
        assert "標籤已更新" in r2.text
        assert "checked" in r2.text

        # 全部取消勾選 → detach
        r3 = client.post(f"/ui/customers/{cid}/tags", data={})
        assert "標籤已更新" in r3.text
        assert "checked" not in r3.text


class TestPoints:
    def test_earn_and_redeem(self, client):
        tid = _register_and_login(client)
        cid = _seed_customer(tid)
        r = client.post(f"/ui/customers/{cid}/points", data={
            "delta": 50, "reason": "開卡禮",
        })
        assert "已加 50 點" in r.text
        r2 = client.post(f"/ui/customers/{cid}/points", data={
            "delta": -20, "reason": "折抵",
        })
        assert "已扣 20 點" in r2.text
        db = _Session()
        try:
            assert db.get(Customer, cid).points_balance == 30
        finally:
            db.close()

    def test_insufficient_points_error(self, client):
        tid = _register_and_login(client)
        cid = _seed_customer(tid)
        r = client.post(f"/ui/customers/{cid}/points", data={
            "delta": -999, "reason": "折抵",
        })
        assert "點數不足" in r.text
        db = _Session()
        try:
            assert db.get(Customer, cid).points_balance == 0
        finally:
            db.close()
