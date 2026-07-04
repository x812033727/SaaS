"""行銷自動化測試 — 受眾挑選、冪等發送、上限、獎勵派發（原子）、feature gating。"""

from __future__ import annotations

import datetime
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t  # noqa: F401
from saas_mvp.models import user as _u  # noqa: F401
from saas_mvp.models import customer as _c  # noqa: F401
from saas_mvp.models import campaign as _camp  # noqa: F401
from saas_mvp.models import campaign_send as _cs  # noqa: F401
from saas_mvp.models import coupon as _coupon  # noqa: F401
from saas_mvp.models import coupon_redemption as _cr  # noqa: F401
from saas_mvp.models import point_transaction as _pt  # noqa: F401
from saas_mvp.models import tenant_feature as _tf  # noqa: F401
from saas_mvp.models import feature_change_history as _fch  # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import FakeLinePushClient, get_push_client
from saas_mvp.models.campaign import (
    CAMPAIGN_BIRTHDAY,
    CAMPAIGN_BROADCAST,
    CAMPAIGN_REACTIVATION,
    Campaign,
)
from saas_mvp.models.campaign_send import CAMPAIGN_SEND_SENT, CampaignSend
from saas_mvp.models.coupon import Coupon
from saas_mvp.models.customer import Customer
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.services import features as features_svc
from saas_mvp.services import marketing as marketing_svc

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_NOW = datetime.datetime(2030, 6, 15, 9, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db, *, marketing=True) -> int:
    t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
    db.add(t)
    db.flush()
    if marketing:
        features_svc.set_enabled(
            db, t.id, features_svc.MARKETING_AUTO, True,
            actor_user_id=None, source="admin",
        )
    return t.id


def _customer(db, tid, *, line="U" + "a" * 10, birthday=None, last_booked=None, name="A") -> Customer:
    c = Customer(
        tenant_id=tid,
        line_user_id=line,
        display_name=name,
        birthday=birthday,
        last_booked_at=last_booked,
    )
    db.add(c)
    db.flush()
    return c


def test_birthday_eligibility_by_month_day(db):
    tid = _tenant(db)
    match = _customer(db, tid, line="Umatch", birthday=datetime.date(1990, 6, 15))
    _customer(db, tid, line="Uother", birthday=datetime.date(1990, 6, 16))
    _customer(db, tid, line="Unobd", birthday=None)
    db.commit()
    camp = Campaign(
        tenant_id=tid, type=CAMPAIGN_BIRTHDAY, name="bd",
        message_template="生日快樂 {name}",
    )
    db.add(camp)
    db.commit()
    elig = marketing_svc.eligible_customers(db, camp, _NOW)
    assert [c.id for c in elig] == [match.id]


def test_reactivation_dormant_boundary(db):
    tid = _tenant(db)
    # dormant_days default 90; cutoff = NOW - 90d
    old = _customer(
        db, tid, line="Uold",
        last_booked=_NOW - datetime.timedelta(days=120),
    )
    _customer(
        db, tid, line="Urecent",
        last_booked=_NOW - datetime.timedelta(days=10),
    )
    db.commit()
    camp = Campaign(
        tenant_id=tid, type=CAMPAIGN_REACTIVATION, name="re",
        message_template="想念您 {name}",
    )
    db.add(camp)
    db.commit()
    elig = marketing_svc.eligible_customers(db, camp, _NOW)
    assert [c.id for c in elig] == [old.id]


def test_idempotent_send_one_per_period(db):
    tid = _tenant(db)
    _customer(db, tid, line="U1", birthday=datetime.date(1990, 6, 15))
    db.commit()
    camp = Campaign(
        tenant_id=tid, type=CAMPAIGN_BIRTHDAY, name="bd",
        message_template="hi {name}",
    )
    db.add(camp)
    db.commit()
    fake = FakeLinePushClient()
    r1 = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=100, push_client=fake)
    r2 = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=100, push_client=fake)
    assert r1["sent"] == 1
    assert r2["sent"] == 0  # 同 period_key 不重送
    sends = db.execute(
        select(CampaignSend).where(CampaignSend.campaign_id == camp.id)
    ).scalars().all()
    assert len(sends) == 1
    assert fake.call_count == 1


def test_cap_enforcement(db):
    tid = _tenant(db)
    for i in range(5):
        _customer(db, tid, line=f"U{i}", birthday=datetime.date(1990, 6, 15))
    db.commit()
    camp = Campaign(
        tenant_id=tid, type=CAMPAIGN_BIRTHDAY, name="bd",
        message_template="hi {name}",
    )
    db.add(camp)
    db.commit()
    fake = FakeLinePushClient()
    r = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=2, push_client=fake)
    assert r["sent"] == 2
    assert fake.call_count == 2


def test_rerun_with_many_existing_sent_skips_without_resend(db):
    """既有大量 sent 記錄時重跑：全部冪等跳過、不重推（批次預撈 map 路徑）。"""
    tid = _tenant(db)
    for i in range(8):
        _customer(db, tid, line=f"UB{i}", birthday=datetime.date(1990, 6, 15))
    db.commit()
    camp = Campaign(
        tenant_id=tid, type=CAMPAIGN_BIRTHDAY, name="bd",
        message_template="hi {name}",
    )
    db.add(camp)
    db.commit()
    fake = FakeLinePushClient()
    r1 = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=100, push_client=fake)
    assert r1["sent"] == 8
    r2 = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=100, push_client=fake)
    assert r2["sent"] == 0
    assert r2["skipped"] == 8
    assert fake.call_count == 8  # 未重推
    sends = db.execute(
        select(CampaignSend).where(CampaignSend.campaign_id == camp.id)
    ).scalars().all()
    assert len(sends) == 8  # 未新增列


def test_quota_recalibration_across_interval(db, monkeypatch):
    """額度大於校準週期（20）時，跨週期校準後仍精準停在額度上限。"""
    from saas_mvp.config import settings

    monkeypatch.setattr(settings, "push_allowance_base", 25)
    tid = _tenant(db)
    for i in range(30):
        _customer(db, tid, line=f"UQ{i}", birthday=datetime.date(1990, 6, 15))
    db.commit()
    camp = Campaign(
        tenant_id=tid, type=CAMPAIGN_BIRTHDAY, name="bd",
        message_template="hi {name}",
    )
    db.add(camp)
    db.commit()
    fake = FakeLinePushClient()
    r = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=100, push_client=fake)
    assert r["sent"] == 25
    assert fake.call_count == 25


def test_reward_points_atomic(db):
    tid = _tenant(db)
    c = _customer(db, tid, line="U1", birthday=datetime.date(1990, 6, 15))
    db.commit()
    camp = Campaign(
        tenant_id=tid, type=CAMPAIGN_BIRTHDAY, name="bd",
        message_template="hi", reward_type="points", reward_value=50,
    )
    db.add(camp)
    db.commit()
    fake = FakeLinePushClient()
    r = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=10, push_client=fake)
    assert r["sent"] == 1
    db.refresh(c)
    assert c.points_balance == 50
    send = db.execute(
        select(CampaignSend).where(CampaignSend.campaign_id == camp.id)
    ).scalar_one()
    assert send.status == CAMPAIGN_SEND_SENT
    assert send.reward_ref == "points:50"


def test_reward_coupon_atomic(db):
    tid = _tenant(db)
    c = _customer(db, tid, line="U1", birthday=datetime.date(1990, 6, 15))
    coupon = Coupon(
        tenant_id=tid, code="BD10", name="生日券",
        discount_type="amount", discount_value=100,
    )
    db.add(coupon)
    db.commit()
    camp = Campaign(
        tenant_id=tid, type=CAMPAIGN_BIRTHDAY, name="bd",
        message_template="hi", reward_type="coupon", reward_value=coupon.id,
    )
    db.add(camp)
    db.commit()
    fake = FakeLinePushClient()
    r = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=10, push_client=fake)
    assert r["sent"] == 1
    db.refresh(coupon)
    assert coupon.redeemed_count == 1
    send = db.execute(
        select(CampaignSend).where(CampaignSend.campaign_id == camp.id)
    ).scalar_one()
    assert send.reward_ref is not None and send.reward_ref.startswith("coupon:")


def test_segment_json_filter(db):
    tid = _tenant(db)
    gold = _customer(db, tid, line="Ugold")
    gold.tier = "gold"
    _customer(db, tid, line="Ureg")  # tier default regular
    db.commit()
    camp = Campaign(
        tenant_id=tid, type=CAMPAIGN_BROADCAST, name="bc",
        message_template="hi", segment_json=json.dumps({"tier": "gold"}),
    )
    db.add(camp)
    db.commit()
    elig = marketing_svc.eligible_customers(db, camp, _NOW)
    assert [c.line_user_id for c in elig] == ["Ugold"]


# ── REST + feature gating ────────────────────────────────────────────────────

@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_get_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    fake = FakeLinePushClient()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_push_client] = lambda: fake
    with TestClient(app, raise_server_exceptions=True) as c:
        c._fake_push = fake
        yield c


def _register(client) -> str:
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _enable(client, token, feature):
    client.post(f"/billing/features/{feature}/subscribe", headers=_auth(token))


def test_feature_gating_403_when_disabled(client):
    token = _register(client)
    client.post("/billing/features/MARKETING_AUTO/unsubscribe", headers=_auth(token))
    r = client.get("/booking/campaigns/", headers=_auth(token))
    assert r.status_code == 403


def test_campaign_crud_and_run(client):
    token = _register(client)
    _enable(client, token, "MARKETING_AUTO")
    r = client.post("/booking/campaigns/", headers=_auth(token), json={
        "name": "broadcast all", "type": "broadcast",
        "message_template": "hi {name}",
    })
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    # list
    assert any(c["id"] == cid for c in client.get("/booking/campaigns/", headers=_auth(token)).json())
    # run (no customers → sent 0)
    rr = client.post(f"/booking/campaigns/{cid}/run", headers=_auth(token))
    assert rr.status_code == 200
    assert rr.json() == {"sent": 0, "skipped": 0}
