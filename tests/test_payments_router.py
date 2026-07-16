"""綠界金流端點測試（checkout 頁 + 回調驗簽/標記/竄改/金額/冪等）。"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t  # noqa: F401,E402
from saas_mvp.models import customer as _c  # noqa: F401,E402
from saas_mvp.models import product as _p, order as _o, order_item as _oi  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.order import Order  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services.payment_ecpay import EcpayClient  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    monkeypatch.setattr(settings, "payment_provider", "ecpay")
    monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
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


def _make_order(total_cents=10000, status="pending") -> int:
    from saas_mvp.services.shop import gen_order_trade_no

    import uuid as _uuid

    db = _Session()
    try:
        t = Tenant(name=f"shop_{_uuid.uuid4().hex[:8]}", plan="free")
        db.add(t)
        db.flush()
        o = Order(tenant_id=t.id, line_user_id="U1", status=status,
                  total_cents=total_cents, currency="TWD")
        db.add(o)
        db.flush()
        # 比照 create_order:建單即產生不可猜 trade_no(PEA-3 的結帳 URL 鍵)
        o.merchant_trade_no = gen_order_trade_no(o.id)
        db.commit()
        return o.id
    finally:
        db.close()


def _trade_no(oid: int) -> str:
    db = _Session()
    try:
        return db.get(Order, oid).merchant_trade_no
    finally:
        db.close()


def _order(oid) -> Order:
    db = _Session()
    try:
        return db.get(Order, oid)
    finally:
        db.close()


def _callback_params(trade_no, amount_twd, rtn_code="1"):
    p = {
        "MerchantID": "2000132",
        "MerchantTradeNo": trade_no,
        "RtnCode": rtn_code,
        "RtnMsg": "Succeeded" if rtn_code == "1" else "Failed",
        "TradeNo": "2401011200001234",
        "TradeAmt": str(amount_twd),
        "PaymentDate": "2024/01/01 12:05:00",
        "PaymentType": "Credit_CreditCard",
        "TradeDate": "2024/01/01 12:00:00",
        "SimulatePaid": "0",
    }
    p["CheckMacValue"] = EcpayClient().check_mac_value(p)
    return p


class TestCheckout:
    def test_renders_autosubmit_form(self, client):
        oid = _make_order(total_cents=10000)
        r = client.get(f"/payments/ecpay/checkout/{_trade_no(oid)}")
        assert r.status_code == 200
        assert "payment-stage.ecpay.com.tw" in r.text
        assert "CheckMacValue" in r.text

    def test_non_pending_blocked(self, client):
        oid = _make_order(status="paid")
        r = client.get(f"/payments/ecpay/checkout/{_trade_no(oid)}")
        assert "無法付款" in r.text

    def test_missing_order_404(self, client):
        r = client.get("/payments/ecpay/checkout/ODUNKNOWNTRADE")
        assert r.status_code == 404

    def test_integer_order_id_enumeration_404(self, client):
        """PEA-3:checkout 改以不可猜 trade_no 為鍵,可枚舉的整數 id 一律 404。"""
        oid = _make_order(total_cents=10000)
        assert client.get(f"/payments/ecpay/checkout/{oid}").status_code == 404

    def test_trade_no_is_unguessable(self, client):
        """trade_no = OD + id36 + 隨機 hex 填滿 20 字(≥32-bit 隨機段)。"""
        a, b = _trade_no(_make_order()), _trade_no(_make_order())
        assert a.startswith("OD") and len(a) == 20
        assert a != b


class TestCallback:
    def _prepare(self, client):
        oid = _make_order(total_cents=10000)  # NT$100
        return oid, _trade_no(oid)

    def test_valid_marks_paid_and_idempotent(self, client):
        oid, trade_no = self._prepare(client)
        r = client.post("/payments/ecpay/callback", data=_callback_params(trade_no, 100))
        assert r.status_code == 200 and r.text == "1|OK"
        assert _order(oid).status == "paid"
        # 重送（冪等）仍 1|OK、仍 paid
        r2 = client.post("/payments/ecpay/callback", data=_callback_params(trade_no, 100))
        assert r2.text == "1|OK" and _order(oid).status == "paid"

    def test_bad_signature_rejected(self, client):
        oid, trade_no = self._prepare(client)
        p = _callback_params(trade_no, 100)
        p["CheckMacValue"] = "DEADBEEF"
        r = client.post("/payments/ecpay/callback", data=p)
        assert r.text.startswith("0|") and _order(oid).status == "pending"

    def test_amount_mismatch_rejected(self, client):
        oid, trade_no = self._prepare(client)
        r = client.post("/payments/ecpay/callback", data=_callback_params(trade_no, 999))
        assert r.text == "0|amount mismatch" and _order(oid).status == "pending"

    def test_unknown_trade_no(self, client):
        r = client.post("/payments/ecpay/callback", data=_callback_params("NOSUCH", 100))
        assert r.text == "0|order not found"

    def test_failed_payment_acked_no_change(self, client):
        oid, trade_no = self._prepare(client)
        r = client.post("/payments/ecpay/callback", data=_callback_params(trade_no, 100, rtn_code="0"))
        assert r.text == "1|OK" and _order(oid).status == "pending"
