"""綠界 ECPay CheckMacValue / 表單 / provider 測試。"""

from __future__ import annotations

import pytest

from saas_mvp.config import settings
from saas_mvp.services.payment import get_payment_provider
from saas_mvp.services.payment_ecpay import EcpayClient, EcpayPaymentProvider

# 綠界公開測試金鑰
_HK = "5294y06JbISpM5x9"
_HIV = "v77hoKGq4kWxNNIS"

# 對固定輸入的 golden CheckMacValue（以綠界官方 generate_check_value 演算法計算）。
# 鎖定演算法不被誤改；演算法逐位元組對齊 ECPay/ECPayAIO_Python 官方 SDK。
_GOLDEN_INPUT = {
    "MerchantID": "2000132",
    "MerchantTradeNo": "TEST0001",
    "MerchantTradeDate": "2024/01/01 12:00:00",
    "PaymentType": "aio",
    "TotalAmount": "100",
    "TradeDesc": "test",
    "ItemName": "coffee",
    "ReturnURL": "https://example.com/cb",
    "ChoosePayment": "ALL",
    "EncryptType": "1",
}
_GOLDEN = "6480269AF547D917A6FE4FEF6299E257E8393376D0DA9E4CBAC50D9C2A5B3511"


def _client() -> EcpayClient:
    return EcpayClient(merchant_id="2000132", hash_key=_HK, hash_iv=_HIV, env="stage")


class TestCheckMacValue:
    def test_golden_vector(self):
        assert _client().check_mac_value(_GOLDEN_INPUT) == _GOLDEN

    def test_excludes_checkmacvalue_key(self):
        c = _client()
        with_cmv = dict(_GOLDEN_INPUT, CheckMacValue="whatever")
        assert c.check_mac_value(with_cmv) == _GOLDEN  # 自身被排除

    def test_md5_when_encrypt_type_0(self):
        c = _client()
        p = dict(_GOLDEN_INPUT, EncryptType="0")
        v = c.check_mac_value(p)
        assert len(v) == 32 and v == v.upper()  # MD5 32 hex 大寫

    def test_verify_accepts_and_rejects(self):
        c = _client()
        p = dict(_GOLDEN_INPUT)
        p["CheckMacValue"] = c.check_mac_value(p)
        assert c.verify(p) is True
        tampered = dict(p, TotalAmount="1")
        assert c.verify(tampered) is False


class TestBuildOrderForm:
    def test_required_fields_and_self_consistent(self):
        c = _client()
        form = c.build_order_form(
            merchant_trade_no="OD9Txyz", amount_twd=250, item_name="訂單9",
            trade_desc="LINE 商城訂單", return_url="https://x/cb",
            client_back_url="https://x/done",
        )
        for k in ("MerchantID", "MerchantTradeNo", "MerchantTradeDate", "PaymentType",
                  "TotalAmount", "TradeDesc", "ItemName", "ReturnURL", "ChoosePayment",
                  "EncryptType", "CheckMacValue"):
            assert k in form
        assert form["TotalAmount"] == "250" and form["PaymentType"] == "aio"
        assert c.verify(form) is True

    def test_aio_url_stage_vs_prod(self):
        assert "stage" in EcpayClient(env="stage").aio_url
        assert EcpayClient(env="prod").aio_url == "https://payment.ecpay.com.tw/Cashier/AioCheckOut/V5"


class TestProvider:
    def test_ecpay_provider_checkout_url(self, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
        # trade_no 已存在 → ensure_order_trade_no 直接沿用,不需 db
        order = SimpleNamespace(id=42, merchant_trade_no="OD16ABCDEF0123456789")
        url = EcpayPaymentProvider().create_checkout(None, order=order)
        assert url == "https://shop.example/payments/ecpay/checkout/OD16ABCDEF0123456789"

    def test_factory_returns_ecpay_when_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "ecpay")
        assert isinstance(get_payment_provider(), EcpayPaymentProvider)

    def test_factory_returns_stub_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "stub")
        from saas_mvp.services.payment import StubPaymentProvider
        assert isinstance(get_payment_provider(), StubPaymentProvider)
