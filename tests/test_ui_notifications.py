"""/ui/notifications 通知與推播歷程頁（唯讀）測試。

涵蓋：三個 tab 渲染、狀態篩選、跨租戶不可見、推播用量摘要、未登入重導。
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
from saas_mvp.models import booking_notification as _bn  # noqa: F401,E402
from saas_mvp.models import campaign as _camp, campaign_send as _cs  # noqa: F401,E402
from saas_mvp.models import push_usage as _pu  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.booking_notification import BookingNotification  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.campaign import Campaign  # noqa: E402
from saas_mvp.models.campaign_send import CampaignSend  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.push_usage import PushUsage  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402

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
    email = f"nt_{_uid()}@example.com"
    password = "Test1234!"
    r = client.post("/auth/register", json={
        "email": email, "password": password,
        "tenant_name": f"nt_t_{_uid()}",
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
    assert r2.status_code == 303
    return tid


_SLOT_SEQ = iter(range(100_000))


def _seed_notification(tid: int, *, status="sent", text="您的預約已改期") -> None:
    db = _Session()
    try:
        slot = BookingSlot(
            tenant_id=tid,
            # (tenant_id, slot_start) 唯一 → 每次種子用不同時間
            slot_start=datetime.datetime(
                2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc
            ) + datetime.timedelta(minutes=next(_SLOT_SEQ)),
            max_capacity=4,
        )
        db.add(slot)
        db.flush()
        resv = Reservation(
            tenant_id=tid, slot_id=slot.id, line_user_id="Un", party_size=1
        )
        db.add(resv)
        db.flush()
        db.add(BookingNotification(
            tenant_id=tid,
            reservation_id=resv.id,
            line_user_id="Un",
            kind="change",
            status=status,
            payload_text=text,
        ))
        db.commit()
    finally:
        db.close()


def _seed_campaign_send(tid: int, *, status="sent") -> None:
    db = _Session()
    try:
        camp = Campaign(
            tenant_id=tid, type="broadcast", name="週年慶群發",
            message_template="hi",
        )
        db.add(camp)
        cust = Customer(
            tenant_id=tid, line_user_id=f"U{uuid.uuid4().hex}",
            display_name="客",
        )
        db.add(cust)
        db.flush()
        db.add(CampaignSend(
            tenant_id=tid, campaign_id=camp.id, customer_id=cust.id,
            line_user_id=cust.line_user_id, period_key="oneoff",
            status=status,
        ))
        db.commit()
    finally:
        db.close()


class TestNotificationsPage:
    def test_requires_login(self, client):
        r = client.get("/ui/notifications", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/ui/login"

    def test_booking_tab_renders(self, client):
        tid = _register_and_login(client)
        _seed_notification(tid, text="您的預約已改期至 06/01")
        r = client.get("/ui/notifications")
        assert r.status_code == 200
        assert "預約異動通知" in r.text
        assert "您的預約已改期至 06/01" in r.text

    def test_status_filter(self, client):
        tid = _register_and_login(client)
        _seed_notification(tid, status="sent", text="已送出的通知")
        _seed_notification(tid, status="failed", text="失敗的通知")
        r = client.get("/ui/notifications?tab=booking&status=failed")
        assert "失敗的通知" in r.text
        assert "已送出的通知" not in r.text

    def test_campaign_tab_shows_campaign_name(self, client):
        tid = _register_and_login(client)
        _seed_campaign_send(tid)
        r = client.get("/ui/notifications?tab=campaign")
        assert "行銷發送紀錄" in r.text
        assert "週年慶群發" in r.text

    def test_usage_tab_shows_quota(self, client):
        tid = _register_and_login(client)
        db = _Session()
        try:
            period = datetime.datetime.now(
                datetime.timezone.utc
            ).strftime("%Y%m")
            db.add(PushUsage(tenant_id=tid, period=period, count=42))
            db.commit()
        finally:
            db.close()
        r = client.get("/ui/notifications?tab=usage")
        assert "推播用量" in r.text
        assert "42" in r.text

    def test_htmx_returns_partial(self, client):
        tid = _register_and_login(client)
        _seed_notification(tid)
        r = client.get("/ui/notifications", headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert "<html" not in r.text

    def test_cross_tenant_invisible(self, client):
        tid_a = _register_and_login(client)
        _seed_notification(tid_a, text="A租戶的通知")
        client.get("/ui/logout")
        _register_and_login(client)
        r = client.get("/ui/notifications")
        assert "A租戶的通知" not in r.text
