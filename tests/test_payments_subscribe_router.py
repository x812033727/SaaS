"""綠界定期定額端點：訂閱頁 + 首期/每期回調（驗簽/開通/關閉/冪等）。"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf, feature_change_history as _fch  # noqa: F401,E402
from saas_mvp.models import feature_subscription as _fs  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import subscriptions as subs_svc  # noqa: E402
from saas_mvp.services.payment_ecpay import EcpayClient  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

FEAT = "COUPON_SYSTEM"


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    monkeypatch.setattr(settings, "payment_provider", "ecpay")
    monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
    monkeypatch.setattr(settings, "features_default_enabled", False)  # 顯示明確開通狀態
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


def _make_pending_sub(amount_cents=20000) -> tuple[int, str, int]:
    db = _Session()
    try:
        t = Tenant(name="shop", plan="free")
        db.add(t)
        db.flush()
        sub = subs_svc.create_subscription(
            db, tenant_id=t.id, feature=FEAT, amount_cents=amount_cents
        )
        return sub.id, sub.merchant_trade_no, t.id
    finally:
        db.close()


def _enabled(tenant_id) -> bool:
    db = _Session()
    try:
        return features_svc.is_enabled(db, tenant_id, FEAT)
    finally:
        db.close()


def _sub_status(trade_no) -> str:
    db = _Session()
    try:
        return subs_svc.get_subscription_by_trade_no(db, trade_no).status
    finally:
        db.close()


def _first_auth_params(trade_no, rtn_code="1"):
    p = {
        "MerchantID": "2000132",
        "MerchantTradeNo": trade_no,
        "RtnCode": rtn_code,
        "RtnMsg": "Succeeded" if rtn_code == "1" else "Failed",
        "Gwsr": "11122233",
        "AuthCode": "777777",
        "Amount": "200",
        "PeriodType": "M",
        "Frequency": "1",
        "ExecTimes": "99",
        "ProcessDate": "2024/01/01 12:00:00",
        "TotalSuccessTimes": "1",
    }
    p["CheckMacValue"] = EcpayClient().check_mac_value(p)
    return p


def _period_params(trade_no, rtn_code="1", total="2"):
    p = {
        "MerchantID": "2000132",
        "MerchantTradeNo": trade_no,
        "RtnCode": rtn_code,
        "RtnMsg": "Succeeded" if rtn_code == "1" else "Failed",
        "Gwsr": "11122244",
        "Amount": "200",
        "ProcessDate": "2024/02/01 12:00:00",
        "TotalSuccessTimes": total,
    }
    p["CheckMacValue"] = EcpayClient().check_mac_value(p)
    return p


class TestSubscribePage:
    def test_renders_period_autosubmit_form(self, client):
        sid, _, _ = _make_pending_sub()
        r = client.get(f"/payments/ecpay/subscribe/{sid}")
        assert r.status_code == 200
        assert "payment-stage.ecpay.com.tw" in r.text
        assert "Credit" in r.text and "PeriodReturnURL" in r.text

    def test_missing_subscription_404(self, client):
        assert client.get("/payments/ecpay/subscribe/999999").status_code == 404


class TestSubscribeCallback:
    def test_first_auth_success_enables(self, client):
        sid, trade_no, tid = _make_pending_sub()
        r = client.post("/payments/ecpay/subscribe-callback", data=_first_auth_params(trade_no))
        assert r.status_code == 200 and r.text == "1|OK"
        assert _sub_status(trade_no) == "active"
        assert _enabled(tid) is True
        # 冪等重送仍 1|OK、仍開通
        r2 = client.post("/payments/ecpay/subscribe-callback", data=_first_auth_params(trade_no))
        assert r2.text == "1|OK" and _enabled(tid) is True

    def test_bad_signature_rejected(self, client):
        sid, trade_no, tid = _make_pending_sub()
        p = _first_auth_params(trade_no)
        p["CheckMacValue"] = "DEADBEEF"
        r = client.post("/payments/ecpay/subscribe-callback", data=p)
        assert r.text.startswith("0|") and _enabled(tid) is False

    def test_failed_auth_marks_failed_not_enabled(self, client):
        sid, trade_no, tid = _make_pending_sub()
        r = client.post(
            "/payments/ecpay/subscribe-callback", data=_first_auth_params(trade_no, rtn_code="10100058")
        )
        assert r.text == "1|OK"
        assert _sub_status(trade_no) == "failed" and _enabled(tid) is False

    def test_success_after_pending_was_cancelled_never_reactivates(self, client):
        _, trade_no, tid = _make_pending_sub()
        with _Session() as db:
            sub = subs_svc.get_subscription_by_trade_no(db, trade_no)
            subs_svc.mark_cancelled(db, sub, ok=True)

        r = client.post(
            "/payments/ecpay/subscribe-callback",
            data=_first_auth_params(trade_no),
        )

        assert r.text == "1|OK"
        assert _sub_status(trade_no) == "cancelled"
        assert _enabled(tid) is False

    def test_unknown_trade_no(self, client):
        r = client.post("/payments/ecpay/subscribe-callback", data=_first_auth_params("NOPE"))
        assert r.text == "0|subscription not found"


class TestPeriodCallback:
    def _activate(self, client, trade_no):
        client.post("/payments/ecpay/subscribe-callback", data=_first_auth_params(trade_no))

    def test_period_success_keeps_enabled(self, client):
        sid, trade_no, tid = _make_pending_sub()
        self._activate(client, trade_no)
        r = client.post("/payments/ecpay/period-callback", data=_period_params(trade_no))
        assert r.text == "1|OK" and _enabled(tid) is True

    def test_period_failure_disables(self, client):
        sid, trade_no, tid = _make_pending_sub()
        self._activate(client, trade_no)
        r = client.post(
            "/payments/ecpay/period-callback", data=_period_params(trade_no, rtn_code="10100058")
        )
        assert r.text == "1|OK"
        assert _enabled(tid) is False and _sub_status(trade_no) == "failed"

    def test_period_bad_signature_rejected(self, client):
        sid, trade_no, tid = _make_pending_sub()
        self._activate(client, trade_no)
        p = _period_params(trade_no)
        p["CheckMacValue"] = "BAD"
        r = client.post("/payments/ecpay/period-callback", data=p)
        assert r.text.startswith("0|") and _enabled(tid) is True  # 維持原狀
