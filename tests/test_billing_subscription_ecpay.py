"""進階功能訂閱：ecpay 定期定額 vs stub（service 級，fake http 不打真實網路）。"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf, feature_change_history as _fch  # noqa: F401,E402
from saas_mvp.models import feature_subscription as _fs  # noqa: F401,E402

import saas_mvp.services.payment_ecpay as pe  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base  # noqa: E402
from saas_mvp.models.feature_subscription import (  # noqa: E402
    SUB_ACTIVE,
    SUB_CANCEL_FAILED,
    SUB_CANCELLED,
)
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import billing as billing_svc  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import subscriptions as subs_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

FEAT = "COUPON_SYSTEM"


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db) -> Tenant:
    t = Tenant(name="t", plan="free")
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


class TestStubMode:
    def test_subscribe_enables_immediately(self, db, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "stub")
        t = _tenant(db)
        res = billing_svc.subscribe_feature(db, t, FEAT, actor_user_id=None)
        assert res.mode == "stub" and res.enabled is True
        assert res.payment_id.startswith("simulated_")
        assert features_svc.is_enabled(db, t.id, FEAT) is True

    def test_unsubscribe_disables(self, db, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "stub")
        t = _tenant(db)
        billing_svc.subscribe_feature(db, t, FEAT, actor_user_id=None)
        billing_svc.unsubscribe_feature(db, t, FEAT, actor_user_id=None)
        assert features_svc.is_enabled(db, t.id, FEAT) is False


class TestEcpaySubscribe:
    def test_creates_pending_and_not_enabled(self, db, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "ecpay")
        monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
        monkeypatch.setattr(settings, "features_default_enabled", False)  # 嚴格：顯示未開通
        t = _tenant(db)

        res = billing_svc.subscribe_feature(db, t, FEAT, actor_user_id=None)

        assert res.mode == "ecpay" and res.enabled is False and res.payment_id is None
        assert res.checkout_url.startswith("https://shop.example/payments/ecpay/subscribe/")
        # 尚未開通（待首期授權回調）
        assert features_svc.is_enabled(db, t.id, FEAT) is False
        # 已建 pending 訂閱
        sub = subs_svc.latest_active_for(db, t.id, FEAT)
        assert sub is not None and sub.status == "pending"
        assert sub.period_amount_cents == settings.feature_monthly_price_cents


class TestEcpayUnsubscribe:
    def _active_sub(self, db, t):
        sub = subs_svc.create_subscription(
            db, tenant_id=t.id, feature=FEAT, amount_cents=20000
        )
        subs_svc.activate(db, sub, gwsr="GW1", auth_code="AB12")
        features_svc.set_enabled(
            db, t.id, FEAT, True, actor_user_id=None, source="subscribe"
        )
        return sub

    def test_cancel_success_stops_charge_and_disables(self, db, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "ecpay")
        calls = {}

        def fake_post(url, data):
            calls["data"] = data
            return "RtnCode=1&RtnMsg=OK"

        monkeypatch.setattr(pe, "_urllib_post", fake_post)
        t = _tenant(db)
        sub = self._active_sub(db, t)

        billing_svc.unsubscribe_feature(db, t, FEAT, actor_user_id=None)

        db.refresh(sub)
        assert calls["data"]["Action"] == "Cancel"  # 確實呼叫綠界停扣
        assert sub.status == SUB_CANCELLED and sub.cancelled_at is not None
        assert features_svc.is_enabled(db, t.id, FEAT) is False

    def test_cancel_api_failure_still_disables_but_flags(self, db, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "ecpay")

        def boom(url, data):
            raise OSError("network down")

        monkeypatch.setattr(pe, "_urllib_post", boom)
        t = _tenant(db)
        sub = self._active_sub(db, t)

        billing_svc.unsubscribe_feature(db, t, FEAT, actor_user_id=None)

        db.refresh(sub)
        # 停扣未確認 → 標 cancel_failed（待 ops 重試），但功能仍關閉
        assert sub.status == SUB_CANCEL_FAILED
        assert features_svc.is_enabled(db, t.id, FEAT) is False

    def test_cancel_rtncode_not_1_flags_failed(self, db, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "ecpay")
        monkeypatch.setattr(pe, "_urllib_post", lambda url, data: "RtnCode=2&RtnMsg=err")
        t = _tenant(db)
        sub = self._active_sub(db, t)

        billing_svc.unsubscribe_feature(db, t, FEAT, actor_user_id=None)

        db.refresh(sub)
        assert sub.status == SUB_CANCEL_FAILED
        assert features_svc.is_enabled(db, t.id, FEAT) is False

    def test_active_sub_helper_status(self, db, monkeypatch):
        # 控制組：activate 後狀態確為 active
        t = _tenant(db)
        sub = self._active_sub(db, t)
        assert sub.status == SUB_ACTIVE and sub.total_success_times == 1
