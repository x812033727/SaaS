"""A3 行銷工具測試 — push flex/image、campaign 分流、滿意度調查、洞察。"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, campaign as _camp  # noqa: F401,E402
from saas_mvp.models import campaign_send as _cs  # noqa: F401,E402
from saas_mvp.models import flex_menu as _fm, flex_menu_card as _fmc  # noqa: F401,E402
from saas_mvp.models import reservation_feedback as _rf  # noqa: F401,E402
from saas_mvp.models import booking_slot as _bs, reservation as _r  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import (  # noqa: E402
    FakeLinePushClient,
    FakeLineReplyClient,
    get_line_client,
)
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.campaign import CAMPAIGN_BROADCAST, Campaign  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.flex_menu import FlexMenu  # noqa: E402
from saas_mvp.models.flex_menu_card import FlexMenuCard  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.reservation_feedback import ReservationFeedback  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.ops.send_feedback_requests import send_feedback_requests  # noqa: E402
from saas_mvp.services import feedback as feedback_svc  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import marketing as marketing_svc  # noqa: E402
from saas_mvp.translation import get_translator  # noqa: E402
from saas_mvp.translation.stub import StubTranslator  # noqa: E402

_CHANNEL_SECRET = "mkt_secret_value_0123456789abcdefgh"

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


def _tenant(db) -> int:
    t = Tenant(name=f"mk_{uuid.uuid4().hex[:8]}", plan="pro")
    db.add(t)
    db.flush()
    db.commit()
    return t.id


def _customer(db, tid, line="Umkt001") -> Customer:
    c = Customer(tenant_id=tid, line_user_id=line, display_name="客人")
    db.add(c)
    db.commit()
    return c


def _flex_menu(db, tid) -> int:
    m = FlexMenu(tenant_id=tid, title="活動選單", is_active=True)
    db.add(m)
    db.flush()
    db.add(FlexMenuCard(
        tenant_id=tid, menu_id=m.id, sort_order=0,
        title="夏季優惠", action_type="postback", action_data="action=coupons",
    ))
    db.commit()
    return m.id


def _campaign(db, tid, **kw) -> Campaign:
    camp = Campaign(
        tenant_id=tid, type=CAMPAIGN_BROADCAST, name="c",
        message_template="hi {name}", **kw,
    )
    db.add(camp)
    db.commit()
    return camp


# ── A3.1/A3.2 campaign 分流 ──────────────────────────────────────────────────

class TestCampaignMessageTypes:
    def test_flex_campaign_pushes_flex(self, db):
        tid = _tenant(db)
        _customer(db, tid)
        menu_id = _flex_menu(db, tid)
        camp = _campaign(db, tid, message_type="flex", flex_menu_id=menu_id)
        fake = FakeLinePushClient()
        r = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=10, push_client=fake)
        assert r["sent"] == 1
        assert len(fake.flex) == 1 and len(fake.sent) == 0
        assert fake.flex[0].contents  # carousel payload
        assert fake.call_count == 1  # 計量 1 則

    def test_flex_menu_deleted_falls_back_to_text(self, db):
        tid = _tenant(db)
        _customer(db, tid)
        camp = _campaign(db, tid, message_type="flex", flex_menu_id=99999)
        fake = FakeLinePushClient()
        r = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=10, push_client=fake)
        assert r["sent"] == 1
        assert len(fake.sent) == 1 and len(fake.flex) == 0  # 降級純文字

    def test_image_campaign_pushes_image(self, db):
        tid = _tenant(db)
        _customer(db, tid)
        camp = _campaign(
            db, tid, message_type="image", image_url="https://cdn.example/x.jpg"
        )
        fake = FakeLinePushClient()
        r = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=10, push_client=fake)
        assert r["sent"] == 1
        assert len(fake.images) == 1
        assert fake.images[0].preview_url == "https://cdn.example/x.jpg"

    def test_non_https_image_falls_back_to_text(self, db):
        tid = _tenant(db)
        _customer(db, tid)
        camp = _campaign(
            db, tid, message_type="image", image_url="http://insecure.example/x.jpg"
        )
        fake = FakeLinePushClient()
        marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=10, push_client=fake)
        assert len(fake.sent) == 1 and len(fake.images) == 0

    def test_default_text_unchanged(self, db):
        tid = _tenant(db)
        _customer(db, tid)
        camp = _campaign(db, tid)
        fake = FakeLinePushClient()
        marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=10, push_client=fake)
        assert len(fake.sent) == 1
        assert "hi 客人" in fake.sent[0].text


# ── A3.3 滿意度調查 ──────────────────────────────────────────────────────────

def _seed_finished_reservation(db, tid, *, line="Ufb001") -> int:
    slot = BookingSlot(
        tenant_id=tid,
        slot_start=_NOW - datetime.timedelta(hours=3),
        slot_end=_NOW - datetime.timedelta(hours=2),
        max_capacity=4,
    )
    db.add(slot)
    db.flush()
    resv = Reservation(
        tenant_id=tid, slot_id=slot.id, party_size=1, line_user_id=line,
    )
    db.add(resv)
    db.commit()
    return resv.id


def _line_cfg(db, tid) -> None:
    cfg = LineChannelConfig(tenant_id=tid, default_target_lang="zh-TW")
    cfg.channel_secret = _CHANNEL_SECRET
    cfg.access_token = "tok"
    cfg.bot_mode = "booking"
    db.add(cfg)
    db.commit()


class TestFeedbackSurvey:
    def test_cron_sends_score_buttons_once(self, db):
        tid = _tenant(db)
        _line_cfg(db, tid)
        rid = _seed_finished_reservation(db, tid)
        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        fake = FakeLinePushClient()

        r1 = send_feedback_requests(
            session_factory=factory, push_client=fake, apply=True, now=_NOW
        )
        assert [x.status for x in r1] == ["sent"]
        assert len(fake.sent) == 1
        labels = [i[0] for i in (fake.sent[0].quick_reply or [])]
        assert len(labels) == 5 and "1 分" in labels[0]

        # 冪等：再跑不重發
        r2 = send_feedback_requests(
            session_factory=factory, push_client=fake, apply=True, now=_NOW
        )
        assert r2 == []
        db.expire_all()
        row = db.execute(
            select(ReservationFeedback).where(
                ReservationFeedback.reservation_id == rid
            )
        ).scalar_one()
        assert row.score is None  # 已發卷未回

    def test_feature_disabled_not_sent(self, db, monkeypatch):
        from saas_mvp.config import settings

        monkeypatch.setattr(settings, "features_default_enabled", False)
        tid = _tenant(db)
        db.get(Tenant, tid).plan = "standard"  # standard 無 FEEDBACK_SURVEY
        db.commit()
        _line_cfg(db, tid)
        _seed_finished_reservation(db, tid)
        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        fake = FakeLinePushClient()
        r = send_feedback_requests(
            session_factory=factory, push_client=fake, apply=True, now=_NOW
        )
        assert r == [] and fake.call_count == 0

    def test_record_score_and_summary(self, db):
        tid = _tenant(db)
        rid = _seed_finished_reservation(db, tid, line="Uscore")
        db.add(ReservationFeedback(
            tenant_id=tid, reservation_id=rid, line_user_id="Uscore",
        ))
        db.commit()
        row = feedback_svc.record_score(
            db, tenant_id=tid, reservation_id=rid, line_user_id="Uscore", score=5
        )
        assert row is not None and row.score == 5
        s = feedback_svc.summary(db, tid)
        assert s["avg_score"] == 5.0 and s["response_rate"] == 1.0

    def test_record_score_wrong_owner_none(self, db):
        tid = _tenant(db)
        rid = _seed_finished_reservation(db, tid, line="Uowner")
        db.add(ReservationFeedback(
            tenant_id=tid, reservation_id=rid, line_user_id="Uowner",
        ))
        db.commit()
        assert feedback_svc.record_score(
            db, tenant_id=tid, reservation_id=rid, line_user_id="Uhacker", score=1
        ) is None


# ── webhook rate action（端到端）─────────────────────────────────────────────

class TestRateWebhook:
    @pytest.fixture()
    def client(self):
        Base.metadata.drop_all(bind=_engine)
        Base.metadata.create_all(bind=_engine)
        line_client = FakeLineReplyClient()
        app = create_app()

        def override_db():
            s = _Session()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_line_client] = lambda: line_client
        app.dependency_overrides[get_translator] = lambda: StubTranslator()
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c, line_client

    def _post(self, c, tid, data, user):
        body = json.dumps({
            "destination": "x",
            "events": [{
                "type": "postback", "replyToken": "rt",
                "webhookEventId": f"e{uuid.uuid4().hex[:6]}",
                "source": {"type": "user", "userId": user},
                "postback": {"data": data},
            }],
        }).encode()
        mac = hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256)
        sig = base64.b64encode(mac.digest()).decode()
        r = c.post(
            f"/line/webhook/{tid}", content=body,
            headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
        )
        assert r.status_code == 200

    def test_high_score_thanks(self, client):
        c, line = client
        db = _Session()
        tid = _tenant(db)
        _line_cfg(db, tid)
        rid = _seed_finished_reservation(db, tid, line="Urate5")
        db.add(ReservationFeedback(
            tenant_id=tid, reservation_id=rid, line_user_id="Urate5",
        ))
        db.commit()
        db.close()

        self._post(c, tid, f"action=rate&reservation_id={rid}&score=5", "Urate5")
        assert "好評" in line.sent[-1].text

    def test_low_score_apology(self, client):
        c, line = client
        db = _Session()
        tid = _tenant(db)
        _line_cfg(db, tid)
        rid = _seed_finished_reservation(db, tid, line="Urate1")
        db.add(ReservationFeedback(
            tenant_id=tid, reservation_id=rid, line_user_id="Urate1",
        ))
        db.commit()
        db.close()

        self._post(c, tid, f"action=rate&reservation_id={rid}&score=1", "Urate1")
        assert "抱歉" in line.sent[-1].text
        db = _Session()
        try:
            row = db.execute(
                select(ReservationFeedback).where(
                    ReservationFeedback.reservation_id == rid
                )
            ).scalar_one()
            assert row.score == 1
        finally:
            db.close()
