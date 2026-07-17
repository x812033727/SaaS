"""R6-B2 — 建檔週年行銷觸發(anniversary)。"""

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
from saas_mvp.models.campaign import CAMPAIGN_ANNIVERSARY, Campaign  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.ops import run_anniversary_campaigns as cron  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import marketing as marketing_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
_NOW = datetime.datetime(2030, 6, 15, 9, 0, tzinfo=datetime.timezone.utc)


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


def _customer(db, tid, *, line, created_at, opted_out=False):
    c = Customer(tenant_id=tid, line_user_id=line, display_name="A",
                 created_at=created_at,
                 marketing_opt_out_at=(_NOW if opted_out else None))
    db.add(c)
    db.flush()
    return c


class TestAnniversaryEligibility:
    def test_matches_created_at_month_day(self):
        db = _Session()
        try:
            tid = _tenant(db)
            match = _customer(db, tid, line="Um", created_at=datetime.datetime(2028, 6, 15, 3, 0))
            _customer(db, tid, line="Uo", created_at=datetime.datetime(2028, 6, 16, 3, 0))
            db.commit()
            camp = Campaign(tenant_id=tid, type=CAMPAIGN_ANNIVERSARY, name="a",
                            message_template="週年快樂 {name}")
            db.add(camp)
            db.commit()
            elig = marketing_svc.eligible_customers(db, camp, _NOW)
            assert [c.id for c in elig] == [match.id]
        finally:
            db.close()

    def test_opted_out_excluded(self):
        db = _Session()
        try:
            tid = _tenant(db)
            _customer(db, tid, line="Ux", created_at=datetime.datetime(2028, 6, 15, 3, 0), opted_out=True)
            db.commit()
            camp = Campaign(tenant_id=tid, type=CAMPAIGN_ANNIVERSARY, name="a", message_template="hi {name}")
            db.add(camp)
            db.commit()
            assert marketing_svc.eligible_customers(db, camp, _NOW) == []
        finally:
            db.close()


class TestAnniversaryCron:
    # cron 用真實 _utcnow();顧客 created_at 取「今天的月/日」(年份設過去)→
    # 無論實際跑測試的日期為何,該顧客永遠是週年當日(deterministic)。
    def _anniv_today(self):
        today = datetime.datetime.now(datetime.timezone.utc)
        return today.replace(year=2028, hour=3, minute=0, second=0, microsecond=0)

    def test_apply_runs_and_reports(self):
        db = _Session()
        try:
            tid = _tenant(db)
            _customer(db, tid, line="Ucron", created_at=self._anniv_today())
            db.add(Campaign(tenant_id=tid, type=CAMPAIGN_ANNIVERSARY, name="a", message_template="週年快樂 {name}"))
            db.commit()
        finally:
            db.close()
        fake = FakeLinePushClient()
        out = io.StringIO()
        rc = cron.main(["--apply"], session_factory=_Session, push_client=fake, stdout=out)
        assert rc == 0
        assert "mode=apply" in out.getvalue()
        # 週年顧客收到推播
        assert any("週年" in s.text for s in fake.sent)

    def test_dry_run_no_push(self):
        db = _Session()
        try:
            tid = _tenant(db)
            _customer(db, tid, line="Udry", created_at=self._anniv_today())
            db.add(Campaign(tenant_id=tid, type=CAMPAIGN_ANNIVERSARY, name="a", message_template="hi {name}"))
            db.commit()
        finally:
            db.close()
        fake = FakeLinePushClient()
        out = io.StringIO()
        cron.main([], session_factory=_Session, push_client=fake, stdout=out)
        assert "mode=dry_run" in out.getvalue()
        assert fake.sent == []
