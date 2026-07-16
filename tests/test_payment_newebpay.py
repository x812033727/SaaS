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
        from types import SimpleNamespace

        monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
        # trade_no 已存在 → ensure_order_trade_no 直接沿用,不需 db
        order = SimpleNamespace(id=42, merchant_trade_no="OD16ABCDEF0123456789")
        url = NewebPayProvider().create_checkout(None, order=order)
        assert url == "https://shop.example/payments/newebpay/checkout/OD16ABCDEF0123456789"

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
    from saas_mvp.services.shop import gen_order_trade_no

    db = _Session()
    try:
        t = Tenant(name="shop", plan="free")
        db.add(t)
        db.flush()
        o = Order(tenant_id=t.id, line_user_id="U1", status=status,
                  total_cents=total_cents, currency="TWD")
        db.add(o)
        db.flush()
        o.merchant_trade_no = gen_order_trade_no(o.id)  # 建單即產生(PEA-3)
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


def _notify_params(trade_no, amount_twd, status_code="SUCCESS"):
    """組真實藍新 JSON-with-nested-Result 回調 wire format。

    RespondType=JSON 時藍新解密後的明文為 JSON，欄位巢狀於 Result 內、
    Status 在頂層；Amt 為整數。直接 encrypt 此 JSON 字串模擬真實回調。
    """
    c = NewebPayClient(merchant_id="MS123456", hash_key=_HK, hash_iv=_HIV, env="stage")
    payload = {
        "Status": status_code,
        "Message": "授權成功" if status_code == "SUCCESS" else "授權失敗",
        "Result": {
            "MerchantID": "MS123456",
            "MerchantOrderNo": trade_no,
            "Amt": int(amount_twd),
            "TradeNo": "24010112000012345",
            "PaymentType": "CREDIT",
        },
    }
    ti = _encrypt_json(c, payload)
    return {"TradeInfo": ti, "TradeSha": c.trade_sha(ti), "Status": status_code}


def _encrypt_json(c: NewebPayClient, payload: dict) -> str:
    """以 client 的 key/iv 將 JSON 明文 AES 加密成 hex TradeInfo（模擬藍新閘道）。"""
    from cryptography.hazmat.primitives import padding as _pad
    from cryptography.hazmat.primitives.ciphers import (
        Cipher as _Cipher,
        algorithms as _alg,
        modes as _modes,
    )

    plain = json.dumps(payload).encode("utf-8")
    key, iv = c.hash_key.encode(), c.hash_iv.encode()
    padder = _pad.PKCS7(_alg.AES.block_size).padder()
    padded = padder.update(plain) + padder.finalize()
    enc = _Cipher(_alg.AES(key), _modes.CBC(iv)).encryptor()
    return (enc.update(padded) + enc.finalize()).hex()


class TestCheckout:
    def test_renders_autosubmit_form(self, http):
        oid = _make_order(total_cents=10000)
        r = http.get(f"/payments/newebpay/checkout/{_trade_no(oid)}")
        assert r.status_code == 200
        assert "ccore.newebpay.com" in r.text
        assert "TradeInfo" in r.text and "TradeSha" in r.text

    def test_non_pending_blocked(self, http):
        oid = _make_order(status="paid")
        r = http.get(f"/payments/newebpay/checkout/{_trade_no(oid)}")
        assert "無法付款" in r.text

    def test_missing_order_404(self, http):
        assert http.get("/payments/newebpay/checkout/ODUNKNOWNTRADE").status_code == 404

    def test_integer_order_id_enumeration_404(self, http):
        """PEA-3:改以不可猜 trade_no 為鍵,可枚舉的整數 id 一律 404。"""
        oid = _make_order(total_cents=10000)
        assert http.get(f"/payments/newebpay/checkout/{oid}").status_code == 404

    def test_provider_gate_503(self, http, monkeypatch):
        """payment_provider 非 newebpay 時結帳頁 503(補上與 ecpay 對等的 gate)。"""
        monkeypatch.setattr(settings, "payment_provider", "stub")
        oid = _make_order(total_cents=10000)
        assert http.get(f"/payments/newebpay/checkout/{_trade_no(oid)}").status_code == 503


class TestNotify:
    def _prepare(self, http):
        oid = _make_order(total_cents=10000)  # NT$100
        return oid, _trade_no(oid)

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

    def test_json_nested_result_parsed_and_marks_paid(self, http):
        """H1 回歸：decrypt_trade_info 直接解析真實藍新 JSON-with-nested-Result
        wire format（Status 頂層、MerchantOrderNo/Amt 巢狀於 Result），
        並正確標記訂單已付。"""
        oid, trade_no = self._prepare(http)
        c = NewebPayClient(merchant_id="MS123456", hash_key=_HK, hash_iv=_HIV, env="stage")
        ti = _encrypt_json(c, {
            "Status": "SUCCESS",
            "Result": {"MerchantOrderNo": trade_no, "Amt": 100},
        })
        # service 層直接解出巢狀 dict（不再是攤平 query-string）。
        parsed = c.decrypt_trade_info(ti)
        assert parsed["Status"] == "SUCCESS"
        assert parsed["Result"]["MerchantOrderNo"] == trade_no
        assert parsed["Result"]["Amt"] == 100
        r = http.post(
            "/payments/newebpay/notify",
            data={"TradeInfo": ti, "TradeSha": c.trade_sha(ti)},
        )
        assert r.status_code == 200 and r.text == "1|OK"
        assert _order(oid).status == "paid"

    def test_malformed_result_rejected_not_500(self, http):
        """H1 回歸：Result 型別異常（非 dict）的回調被拒（不是 500）。"""
        oid, trade_no = self._prepare(http)
        c = NewebPayClient(merchant_id="MS123456", hash_key=_HK, hash_iv=_HIV, env="stage")
        # Result 是字串而非物件 → 取 MerchantOrderNo 不可 .get()，須被攔成拒絕。
        ti = _encrypt_json(c, {"Status": "SUCCESS", "Result": "not-an-object"})
        r = http.post(
            "/payments/newebpay/notify",
            data={"TradeInfo": ti, "TradeSha": c.trade_sha(ti)},
        )
        # 不應 500；訂單維持 pending。
        assert r.status_code == 200
        assert _order(oid).status == "pending"
