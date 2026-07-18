"""R7-B — 到訪後感謝行銷觸發(post_visit)。"""

from __future__ import annotations

import datetime
import io
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.db import Base, import_all_models  # noqa: E402

import_all_models()

from saas_mvp.line_client import FakeLinePushClient  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.campaign import CAMPAIGN_POST_VISIT, Campaign  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.ops import run_post_visit_campaigns as cron  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import marketing as marketing_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
_NOW = datetime.datetime(2030, 6, 15, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def _fresh():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


def _tenant(db) -> int:
    t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
    db.add(t)
    db.flush()
    features_svc.set_enabled(db, t.id, features_svc.MARKETING_AUTO, True, actor_user_id=None, source="admin")
    return t.id


_SLOT_SEQ = iter(range(1, 10_000))


def _visited(db, tid, *, line, hours_ago, attended=True, opted_out=False) -> Customer:
    """建一位在 hours_ago 小時前有(已到訪)預約的顧客。

    slot_start 以序號錯開秒數,避開 uq_booking_slot_start(tenant_id, slot_start)。
    """
    c = Customer(tenant_id=tid, line_user_id=line, display_name="A",
                 marketing_opt_out_at=(_NOW if opted_out else None))
    db.add(c)
    db.flush()
    slot = BookingSlot(
        tenant_id=tid,
        slot_start=(
            _NOW - datetime.timedelta(hours=hours_ago, seconds=next(_SLOT_SEQ))
        ).replace(tzinfo=None),
        max_capacity=5,
    )
    db.add(slot)
    db.flush()
    db.add(Reservation(
        tenant_id=tid, slot_id=slot.id, party_size=1, status="confirmed",
        customer_id=c.id, line_user_id=line, attended=attended,
    ))
    db.flush()
    return c


def _campaign(db, tid) -> Campaign:
    camp = Campaign(tenant_id=tid, type=CAMPAIGN_POST_VISIT, name="thanks",
                    message_template="感謝光臨 {name}")
    db.add(camp)
    db.commit()
    return camp


class TestEligibility:
    def test_recent_attended_visit_matches(self):
        db = _Session()
        try:
            tid = _tenant(db)
            hit = _visited(db, tid, line="Uhit", hours_ago=3)
            _visited(db, tid, line="Uold", hours_ago=48)      # 視窗外
            _visited(db, tid, line="Unoshow", hours_ago=3, attended=False)  # 未到訪
            db.commit()
            camp = _campaign(db, tid)
            elig = marketing_svc.eligible_customers(db, camp, _NOW)
            assert [c.id for c in elig] == [hit.id]
        finally:
            db.close()

    def test_opted_out_excluded(self):
        db = _Session()
        try:
            tid = _tenant(db)
            _visited(db, tid, line="Uout", hours_ago=3, opted_out=True)
            db.commit()
            camp = _campaign(db, tid)
            assert marketing_svc.eligible_customers(db, camp, _NOW) == []
        finally:
            db.close()

    def test_no_visits_empty(self):
        db = _Session()
        try:
            tid = _tenant(db)
            camp = _campaign(db, tid)
            assert marketing_svc.eligible_customers(db, camp, _NOW) == []
        finally:
            db.close()


class TestRunAndIdempotency:
    def test_same_day_only_once(self):
        db = _Session()
        try:
            tid = _tenant(db)
            _visited(db, tid, line="Uonce", hours_ago=3)
            db.commit()
            camp = _campaign(db, tid)
            fake = FakeLinePushClient()
            r1 = marketing_svc.run_campaign(db, campaign=camp, now=_NOW, cap=10, push_client=fake)
            assert r1["sent"] == 1
            # 同日再跑(cron 每小時):period_key=%Y%m%d 冪等,不重送
            r2 = marketing_svc.run_campaign(
                db, campaign=camp, now=_NOW + datetime.timedelta(hours=2),
                cap=10, push_client=fake,
            )
            assert r2["sent"] == 0
            assert len(fake.sent) == 1
        finally:
            db.close()


class TestCron:
    def test_dry_run_no_push(self):
        db = _Session()
        try:
            tid = _tenant(db)
            # cron 用真實 now → 種「1 小時前到訪」相對真實時間
            real_now = datetime.datetime.now(datetime.timezone.utc)
            c = Customer(tenant_id=tid, line_user_id="Udry", display_name="A")
            db.add(c)
            db.flush()
            slot = BookingSlot(
                tenant_id=tid,
                slot_start=(real_now - datetime.timedelta(hours=1)).replace(tzinfo=None),
                max_capacity=5,
            )
            db.add(slot)
            db.flush()
            db.add(Reservation(tenant_id=tid, slot_id=slot.id, party_size=1,
                               status="confirmed", customer_id=c.id,
                               line_user_id="Udry", attended=True))
            db.add(Campaign(tenant_id=tid, type=CAMPAIGN_POST_VISIT, name="t",
                            message_template="hi {name}"))
            db.commit()
        finally:
            db.close()
        fake = FakeLinePushClient()
        out = io.StringIO()
        cron.main([], session_factory=_Session, push_client=fake, stdout=out)
        assert "mode=dry_run" in out.getvalue()
        assert fake.sent == []

    def test_apply_pushes(self):
        db = _Session()
        try:
            tid = _tenant(db)
            real_now = datetime.datetime.now(datetime.timezone.utc)
            c = Customer(tenant_id=tid, line_user_id="Uap", display_name="A")
            db.add(c)
            db.flush()
            slot = BookingSlot(
                tenant_id=tid,
                slot_start=(real_now - datetime.timedelta(hours=1)).replace(tzinfo=None),
                max_capacity=5,
            )
            db.add(slot)
            db.flush()
            db.add(Reservation(tenant_id=tid, slot_id=slot.id, party_size=1,
                               status="confirmed", customer_id=c.id,
                               line_user_id="Uap", attended=True))
            db.add(Campaign(tenant_id=tid, type=CAMPAIGN_POST_VISIT, name="t",
                            message_template="感謝光臨 {name}"))
            db.commit()
        finally:
            db.close()
        fake = FakeLinePushClient()
        out = io.StringIO()
        rc = cron.main(["--apply"], session_factory=_Session, push_client=fake, stdout=out)
        assert rc == 0
        assert any("感謝光臨" in s.text for s in fake.sent)
