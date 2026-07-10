"""R2-1 測試 — C1 期扣失敗通知 + C5 readiness 檢查。"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.ops.check_readiness import run_checks  # noqa: E402
from saas_mvp.services import billing as billing_svc  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import subscriptions as subs_svc  # noqa: E402
from saas_mvp.services.mailer import MailerError, StubMailer  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant_with_owner_and_staff(db) -> Tenant:
    t = Tenant(name=f"cn_{uuid.uuid4().hex[:8]}", plan="pro")
    db.add(t)
    db.flush()
    db.add(User(email=f"{t.name}-owner@x.tw", hashed_password="x",
                tenant_id=t.id, role="owner"))
    db.add(User(email=f"{t.name}-staff@x.tw", hashed_password="x",
                tenant_id=t.id, role="staff"))
    db.commit()
    db.refresh(t)
    return t


class _BoomMailer(StubMailer):
    def send(self, *, to, subject, body):
        raise MailerError("boom")


# ── C1 期扣失敗通知 ───────────────────────────────────────────────────────────

class TestChargeFailedNotify:
    def test_period_failure_emails_owner_only(self, db):
        t = _tenant_with_owner_and_staff(db)
        sub = subs_svc.create_subscription(
            db, tenant_id=t.id, feature=features_svc.BUNDLE_PRO, amount_cents=89900
        )
        subs_svc.activate(db, sub)
        mailer = StubMailer()
        # 直接呼叫失敗分支(端到端回調測試已由 test_billing_bundle 覆蓋)
        import saas_mvp.services.mailer as mailer_mod
        orig = mailer_mod._stub_singleton
        mailer_mod._stub_singleton = mailer
        try:
            billing_svc.apply_bundle_period(db, sub, success=False)
        finally:
            mailer_mod._stub_singleton = orig
        db.refresh(t)
        assert t.plan == "free"  # 降級不受通知影響
        assert len(mailer.sent) == 1
        assert "owner@" in mailer.sent[0].to  # 只寄 owner,不寄 staff
        assert "扣款失敗" in mailer.sent[0].subject
        assert "/ui/plan" in mailer.sent[0].body

    def test_mailer_error_does_not_break_downgrade(self, db):
        t = _tenant_with_owner_and_staff(db)
        sub = subs_svc.create_subscription(
            db, tenant_id=t.id, feature=features_svc.BUNDLE_STANDARD, amount_cents=39900
        )
        subs_svc.activate(db, sub)
        billing_svc.notify_charge_failed(
            db, t, plan_label="標準版", period_no=2, mailer=_BoomMailer()
        )  # 不拋
        billing_svc.apply_bundle_period(db, sub, success=False)  # 全流程不炸
        db.refresh(t)
        assert t.plan == "free"

    def test_no_owner_noop(self, db):
        t = Tenant(name=f"noowner_{uuid.uuid4().hex[:6]}", plan="pro")
        db.add(t)
        db.commit()
        billing_svc.notify_charge_failed(
            db, t, plan_label="專業版", period_no=1, mailer=_BoomMailer()
        )  # 無 owner → 靜默,不拋


# ── C5 readiness ─────────────────────────────────────────────────────────────

class TestReadiness:
    def _by_name(self, checks) -> dict:
        return {c.name: c for c in checks}

    def test_insecure_defaults_fail(self, monkeypatch):
        from saas_mvp.ops import check_readiness as cr

        monkeypatch.setattr(settings, "secret_key", cr._INSECURE_SECRET)
        monkeypatch.setattr(settings, "line_channel_encrypt_key", cr._DEV_LINE_KEY)
        monkeypatch.setattr(settings, "ui_csrf_enabled", False)
        by = self._by_name(run_checks(session_factory=_Session))
        assert by["secret_key"].status == "FAIL"
        assert by["line_channel_encrypt_key"].status == "FAIL"
        assert by["ui_csrf"].status == "FAIL"

    def test_prod_with_test_merchant_fails(self, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "ecpay")
        monkeypatch.setattr(settings, "ecpay_env", "prod")
        monkeypatch.setattr(settings, "ecpay_merchant_id", "2000132")
        by = self._by_name(run_checks(session_factory=_Session))
        assert by["payment"].status == "FAIL"
        assert "2000132" in by["payment"].detail

    def test_stub_everything_warns_not_fails(self, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "stub")
        monkeypatch.setattr(settings, "smtp_host", "")
        monkeypatch.setattr(settings, "sentry_dsn", "")
        monkeypatch.setattr(settings, "anthropic_api_key", "")
        by = self._by_name(run_checks(session_factory=_Session))
        for name in ("payment", "smtp", "sentry", "ai"):
            assert by[name].status == "WARN", name

    def test_ecpay_stage_warns_and_https_required(self, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "ecpay")
        monkeypatch.setattr(settings, "ecpay_env", "stage")
        monkeypatch.setattr(settings, "public_base_url", "http://insecure.example")
        by = self._by_name(run_checks(session_factory=_Session))
        assert by["payment"].status == "WARN"
        assert by["public_base_url"].status == "FAIL"
