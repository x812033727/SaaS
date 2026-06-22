"""綠界信用卡定期定額：表單組裝 + 停扣 API。"""

from __future__ import annotations

from saas_mvp.services.payment_ecpay import EcpayClient

_HK = "5294y06JbISpM5x9"
_HIV = "v77hoKGq4kWxNNIS"


def _client(http_post=None) -> EcpayClient:
    return EcpayClient(
        merchant_id="2000132", hash_key=_HK, hash_iv=_HIV, env="stage", http_post=http_post
    )


class TestPeriodForm:
    def test_required_period_fields_and_self_consistent(self):
        c = _client()
        form = c.build_period_form(
            merchant_trade_no="SB0001", period_amount_twd=200,
            item_name="訂閱-COUPON_SYSTEM", trade_desc="月費",
            return_url="https://x/sub-cb", period_return_url="https://x/period-cb",
        )
        assert form["ChoosePayment"] == "Credit"  # 定期定額僅信用卡
        assert form["PeriodAmount"] == "200" and form["TotalAmount"] == "200"
        assert form["PeriodType"] == "M" and form["Frequency"] == "1"
        assert form["ExecTimes"] == "99"
        assert form["PeriodReturnURL"] == "https://x/period-cb"
        assert c.verify(form) is True

    def test_custom_exec_times_and_frequency(self):
        c = _client()
        form = c.build_period_form(
            merchant_trade_no="SB0002", period_amount_twd=100,
            item_name="x", trade_desc="y",
            return_url="https://x/a", period_return_url="https://x/b",
            exec_times=12, frequency=1, period_type="M",
        )
        assert form["ExecTimes"] == "12"
        assert c.verify(form) is True


class TestCancelPeriod:
    def test_posts_signed_cancel_request(self):
        captured = {}

        def fake_post(url, data):
            captured["url"] = url
            captured["data"] = data
            return "RtnCode=1&RtnMsg=Success"

        c = _client(http_post=fake_post)
        resp = c.cancel_period("SB0001")

        assert captured["url"].endswith("/Cashier/CreditCardPeriodAction")
        assert captured["data"]["Action"] == "Cancel"
        assert captured["data"]["MerchantTradeNo"] == "SB0001"
        assert "TimeStamp" in captured["data"]
        # 送出的請求 CheckMacValue 正確（可被自身驗回）
        assert c.verify(captured["data"]) is True
        assert resp.get("RtnCode") == "1"

    def test_parses_failure_response(self):
        c = _client(http_post=lambda url, data: "RtnCode=2&RtnMsg=Order%20not%20found")
        resp = c.cancel_period("NOPE")
        assert resp.get("RtnCode") == "2"

    def test_period_action_url_stage_vs_prod(self):
        assert "payment-stage.ecpay.com.tw" in EcpayClient(env="stage").period_action_url
        assert EcpayClient(env="prod").period_action_url == (
            "https://payment.ecpay.com.tw/Cashier/CreditCardPeriodAction"
        )
