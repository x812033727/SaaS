"""R6-B1 — 行銷退訂/同意(PDPA):eligible 排除、run_campaign 防禦、退訂連結、公開頁。"""

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

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import FakeLinePushClient  # noqa: E402
from saas_mvp.models.campaign import (  # noqa: E402
    CAMPAIGN_BIRTHDAY,
    CAMPAIGN_BROADCAST,
    Campaign,
)
from saas_mvp.models.campaign_send import CampaignSend  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import customer_marketing  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import marketing as marketing_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
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


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _tenant(db) -> int:
    t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
    db.add(t)
    db.flush()
    features_svc.set_enabled(
        db, t.id, features_svc.MARKETING_AUTO, True, actor_user_id=None, source="admin"
    )
    return t.id


def _customer(db, tid, *, line, name="A", opted_out=False, birthday=None) -> Customer:
    c = Customer(
        tenant_id=tid, line_user_id=line, display_name=name, birthday=birthday,
        marketing_opt_out_at=(_NOW if opted_out else None),
    )
    db.add(c)
    db.flush()
    return c


class TestEligibilityExcludesOptedOut:
    def test_broadcast_excludes_opted_out(self, db):
        tid = _tenant(db)
        keep = _customer(db, tid, line="Ukeep")
        _customer(db, tid, line="Uout", opted_out=True)
        db.commit()
        camp = Campaign(tenant_id=tid, type=CAMPAIGN_BROADCAST, name="b",
                        message_template="hi {name}")
        db.add(camp)
        db.commit()
        elig = marketing_svc.eligible_customers(db, camp, _NOW)
        assert [c.id for c in elig] == [keep.id]

    def test_birthday_excludes_opted_out(self, db):
        tid = _tenant(db)
        _customer(db, tid, line="Ubd", birthday=datetime.date(1990, 6, 15), opted_out=True)
        db.commit()
        camp = Campaign(tenant_id=tid, type=CAMPAIGN_BIRTHDAY, name="bd",
                        message_template="生日快樂 {name}")
        db.add(camp)
        db.commit()
        assert marketing_svc.eligible_customers(db, camp, _NOW) == []


class TestRunCampaignOptOut:
    def test_sent_message_has_unsubscribe_link_and_token_assigned(self, db):
        tid = _tenant(db)
        c = _customer(db, tid, line="Usend")
        db.commit()
        settings_base = settings.public_base_url
        settings.public_base_url = "https://shop.example"
        try:
            camp = Campaign(tenant_id=tid, type=CAMPAIGN_BROADCAST, name="b",
                            message_template="優惠 {name}")
            db.add(camp)
            db.commit()
            fake = FakeLinePushClient()
            r = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=10, push_client=fake)
            assert r["sent"] == 1
            db.refresh(c)
            assert c.unsubscribe_token  # 惰性簽發
            # 推播訊息含退訂連結
            assert fake.sent, "expected a push"
            assert "退訂" in fake.sent[-1].text
            assert c.unsubscribe_token in fake.sent[-1].text
        finally:
            settings.public_base_url = settings_base

    def test_opted_out_pending_welcome_never_sends(self, db):
        """退訂者的 pending welcome 列不會被送出(eligible_customers 排除→迴圈不迭代)。"""
        tid = _tenant(db)
        c = _customer(db, tid, line="Uwel", opted_out=True)
        db.commit()
        camp = Campaign(tenant_id=tid, type=CAMPAIGN_BROADCAST, name="b",
                        message_template="hi {name}")
        db.add(camp)
        db.flush()
        period = marketing_svc.period_key_for(camp, _NOW)
        db.add(CampaignSend(campaign_id=camp.id, tenant_id=tid, customer_id=c.id,
                            period_key=period, status="pending"))
        db.commit()
        fake = FakeLinePushClient()
        r = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=10, push_client=fake)
        assert r["sent"] == 0
        assert fake.sent == []  # 退訂者絕不收到


class TestPublicUnsubscribe:
    def _seed_token(self) -> str:
        db = _Session()
        try:
            t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
            db.add(t)
            db.flush()
            c = Customer(tenant_id=t.id, line_user_id="Uunsub", display_name="小明")
            customer_marketing.assign_unsubscribe_token_if_missing(c)
            db.add(c)
            db.commit()
            return c.unsubscribe_token
        finally:
            db.close()

    def test_get_shows_page(self, client):
        token = self._seed_token()
        r = client.get(f"/unsubscribe/{token}")
        assert r.status_code == 200 and "退訂行銷訊息" in r.text

    def test_opt_out_then_opt_in(self, client):
        token = self._seed_token()
        r = client.post(f"/unsubscribe/{token}", data={"action": "out"})
        assert r.status_code == 303
        r = client.get(f"/unsubscribe/{token}")
        assert "已退訂行銷訊息" in r.text
        # 交易性通知不受影響:opt-out 只設 marketing_opt_out_at,不動 line_followed
        db = _Session()
        try:
            c = db.query(Customer).filter_by(unsubscribe_token=token).one()
            assert c.marketing_opt_out_at is not None
            assert c.line_followed is True
        finally:
            db.close()
        # 復訂
        r = client.post(f"/unsubscribe/{token}", data={"action": "in"})
        assert r.status_code == 303
        r = client.get(f"/unsubscribe/{token}")
        assert "退訂行銷訊息" in r.text  # 回到可退訂狀態

    def test_unknown_token_404_no_enumeration(self, client):
        assert client.get("/unsubscribe/nope-nope-nope").status_code == 404
        assert client.post("/unsubscribe/nope", data={"action": "out"}).status_code == 404
