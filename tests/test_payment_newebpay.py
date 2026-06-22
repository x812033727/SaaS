"""藍新金流 NewebPay AES / TradeSha / 表單 / provider / 回調測試。"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.config import settings
from saas_mvp.services.payment import get_payment_provider
from saas_mvp.services.payment_newebpay import NewebPayClient, NewebPayProvider

# 藍新測試用金鑰（HashKey 32 bytes、HashIV 16 bytes，AES-256-CBC 要求）
_HK = "12345678901234567890123456789012"
_HIV = "1234567890123456"


def _client() -> NewebPayClient:
    return NewebPayClient(merchant_id="MS123456", hash_key=_HK, hash_iv=_HIV, env="stage")


class TestAES:
    def test_encrypt_decrypt_round_trip(self):
        c = _client()
        params = {"MerchantOrderNo": "OD1", "Amt": "100", "Status": "SUCCESS"}
        ti = c.encrypt_trade_info(params)
        # hex 輸出
        bytes.fromhex(ti)
        back = c.decrypt_trade_info(ti)
        assert back["MerchantOrderNo"] == "OD1"
        assert back["Amt"] == "100"
        assert back["Status"] == "SUCCESS"

    def test_encrypt_deterministic_same_key(self):
        # 相同明文 + 相同 key/iv → 相同密文（CBC 固定 iv，藍新即此設計）
        a = _client().encrypt_trade_info({"a": "1", "b": "2"})
        b = _client().encrypt_trade_info({"a": "1", "b": "2"})
        assert a == b


class TestTradeSha:
    def test_deterministic(self):
        c = _client()
        ti = c.encrypt_trade_info({"MerchantOrderNo": "OD9", "Amt": "250"})
        assert c.trade_sha(ti) == c.trade_sha(ti)
        v = c.trade_sha(ti)
        assert len(v) == 64 and v == v.upper()  # SHA256 64 hex 大寫

    def test_verify_accepts_and_rejects(self):
        c = _client()
        ti = c.encrypt_trade_info({"MerchantOrderNo": "OD9", "Amt": "250"})
        params = {"TradeInfo": ti, "TradeSha": c.trade_sha(ti)}
        assert c.verify(params) is True
        tampered = dict(params, TradeSha="DEADBEEF")
        assert c.verify(tampered) is False
        # TradeInfo 被改 → TradeSha 不符
        tampered2 = dict(params, TradeInfo=ti[:-2] + ("00" if ti[-2:] != "00" else "11"))
        assert c.verify(tampered2) is False


class TestBuildOrderForm:
    def test_required_fields_and_self_consistent(self):
        c = _client()
        form = c.build_order_form(
            merchant_trade_no="OD9Txyz", amount_twd=250, item_desc="訂單9",
            return_url="https://x/done", notify_url="https://x/notify",
            client_back_url="https://x/done",
        )
        for k in ("MerchantID", "TradeInfo", "TradeSha", "Version"):
            assert k in form
        assert c.verify(form) is True
        # 解回 trade-info 內含正確金額/單號
        info = c.decrypt_trade_info(form["TradeInfo"])
        assert info["Amt"] == "250" and info["MerchantOrderNo"] == "OD9Txyz"
        assert info["NotifyURL"] == "https://x/notify"

    def test_mpg_url_stage_vs_prod(self):
        assert "ccore.newebpay.com" in NewebPayClient(env="stage").mpg_url
        assert NewebPayClient(env="prod").mpg_url == "https://core.newebpay.com/MPG/mpg_gateway"


class TestProvider:
    def test_provider_checkout_url(self, monkeypatch):
        monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
        url = NewebPayProvider().create_checkout(order_id=42, amount_cents=10000, currency="TWD")
        assert url == "https://shop.example/payments/newebpay/checkout/42"

    def test_factory_returns_newebpay_when_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "newebpay")
        assert isinstance(get_payment_provider(), NewebPayProvider)


# ── 端點（checkout 頁 + notify 回調驗簽/標記/竄改/金額/冪等） ──────────────────

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t  # noqa: F401,E402
from saas_mvp.models import customer as _c  # noqa: F401,E402
from saas_mvp.models import product as _p, order as _o, order_item as _oi  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.order import Order  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def http(monkeypatch):
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    monkeypatch.setattr(settings, "payment_provider", "newebpay")
    monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
    monkeypatch.setattr(settings, "newebpay_merchant_id", "MS123456")
    monkeypatch.setattr(settings, "newebpay_hash_key", _HK)
    monkeypatch.setattr(settings, "newebpay_hash_iv", _HIV)
    monkeypatch.setattr(settings, "newebpay_env", "stage")
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
    db = _Session()
    try:
        t = Tenant(name="shop", plan="free")
        db.add(t)
        db.flush()
        o = Order(tenant_id=t.id, line_user_id="U1", status=status,
                  total_cents=total_cents, currency="TWD")
        db.add(o)
        db.commit()
        return o.id
    finally:
        db.close()


def _order(oid) -> Order:
    db = _Session()
    try:
        return db.get(Order, oid)
    finally:
        db.close()


def _notify_params(trade_no, amount_twd, status_code="SUCCESS"):
    c = NewebPayClient(merchant_id="MS123456", hash_key=_HK, hash_iv=_HIV, env="stage")
    info = {
        "Status": status_code,
        "MerchantOrderNo": trade_no,
        "Amt": str(amount_twd),
        "TradeNo": "24010112000012345",
        "PaymentType": "CREDIT",
    }
    ti = c.encrypt_trade_info(info)
    return {"TradeInfo": ti, "TradeSha": c.trade_sha(ti), "Status": status_code}


class TestCheckout:
    def test_renders_autosubmit_form_and_sets_trade_no(self, http):
        oid = _make_order(total_cents=10000)
        r = http.get(f"/payments/newebpay/checkout/{oid}")
        assert r.status_code == 200
        assert "ccore.newebpay.com" in r.text
        assert "TradeInfo" in r.text and "TradeSha" in r.text
        assert _order(oid).merchant_trade_no is not None

    def test_non_pending_blocked(self, http):
        oid = _make_order(status="paid")
        r = http.get(f"/payments/newebpay/checkout/{oid}")
        assert "無法付款" in r.text

    def test_missing_order_404(self, http):
        assert http.get("/payments/newebpay/checkout/999999").status_code == 404


class TestNotify:
    def _prepare(self, http):
        oid = _make_order(total_cents=10000)  # NT$100
        http.get(f"/payments/newebpay/checkout/{oid}")
        return oid, _order(oid).merchant_trade_no

    def test_valid_marks_paid_and_idempotent(self, http):
        oid, trade_no = self._prepare(http)
        r = http.post("/payments/newebpay/notify", data=_notify_params(trade_no, 100))
        assert r.status_code == 200 and r.text == "1|OK"
        assert _order(oid).status == "paid"
        r2 = http.post("/payments/newebpay/notify", data=_notify_params(trade_no, 100))
        assert r2.text == "1|OK" and _order(oid).status == "paid"

    def test_bad_signature_rejected(self, http):
        oid, trade_no = self._prepare(http)
        p = _notify_params(trade_no, 100)
        p["TradeSha"] = "DEADBEEF"
        r = http.post("/payments/newebpay/notify", data=p)
        assert r.text.startswith("0|") and _order(oid).status == "pending"

    def test_amount_mismatch_rejected(self, http):
        oid, trade_no = self._prepare(http)
        r = http.post("/payments/newebpay/notify", data=_notify_params(trade_no, 999))
        assert r.text == "0|amount mismatch" and _order(oid).status == "pending"

    def test_unknown_trade_no(self, http):
        r = http.post("/payments/newebpay/notify", data=_notify_params("NOSUCH", 100))
        assert r.text == "0|order not found"

    def test_failed_payment_acked_no_change(self, http):
        oid, trade_no = self._prepare(http)
        r = http.post("/payments/newebpay/notify", data=_notify_params(trade_no, 100, status_code="FAIL"))
        assert r.text == "1|OK" and _order(oid).status == "pending"
