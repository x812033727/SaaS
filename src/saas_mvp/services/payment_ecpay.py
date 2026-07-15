"""綠界 ECPay AIO 金流串接。

CheckMacValue 演算法**逐位元組對齊綠界官方 Python SDK**（ECPay/ECPayAIO_Python 的
generate_check_value）：排序鍵(case-insensitive) → HashKey=...&...&HashIV=... →
quote_plus(safe='-_.!*()').lower() → sha256 大寫。不引入 ECPay SDK 當 runtime 依賴。

流程：顧客下單 → checkout 頁自動 submit 表單到綠界付款頁 → 綠界 server 回調
ReturnURL（POST，含 CheckMacValue）→ 先驗簽再標記訂單已付 → 回純文字 "1|OK"。
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import hmac
import logging
import urllib.parse
import urllib.request
from typing import Callable

from saas_mvp.config import settings
from saas_mvp.services.payment import PaymentProvider

_log = logging.getLogger(__name__)

_AIO_STAGE = "https://payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5"
_AIO_PROD = "https://payment.ecpay.com.tw/Cashier/AioCheckOut/V5"
_PERIOD_ACTION_STAGE = "https://payment-stage.ecpay.com.tw/Cashier/CreditCardPeriodAction"
_PERIOD_ACTION_PROD = "https://payment.ecpay.com.tw/Cashier/CreditCardPeriodAction"

# 綠界官方 quote_plus 的 safe 字元集（與官方 SDK 相同）
_SAFE = "-_.!*()"


def _urllib_post(url: str, data: dict) -> str:
    """以 application/x-www-form-urlencoded POST，回應文字（綠界 S2S API 用）。

    抽成 module function 以便 EcpayClient 注入 fake，測試不打真實網路（仿 line_client/http.py）。
    """
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — 固定綠界網域
        return resp.read().decode("utf-8", errors="replace")


class EcpayClient:
    """綠界 CheckMacValue 產生/驗證 + AIO 訂單表單組裝（config 驅動）。"""

    def __init__(
        self,
        *,
        merchant_id: str | None = None,
        hash_key: str | None = None,
        hash_iv: str | None = None,
        env: str | None = None,
        http_post: Callable[[str, dict], str] | None = None,
    ) -> None:
        self.merchant_id = merchant_id if merchant_id is not None else settings.ecpay_merchant_id
        self.hash_key = hash_key if hash_key is not None else settings.ecpay_hash_key
        self.hash_iv = hash_iv if hash_iv is not None else settings.ecpay_hash_iv
        self.env = env if env is not None else settings.ecpay_env
        self._http_post = http_post or _urllib_post

    @property
    def aio_url(self) -> str:
        return _AIO_PROD if self.env == "prod" else _AIO_STAGE

    @property
    def period_action_url(self) -> str:
        return _PERIOD_ACTION_PROD if self.env == "prod" else _PERIOD_ACTION_STAGE

    # ── CheckMacValue（對齊官方 SDK generate_check_value） ────────────────────
    def check_mac_value(self, params: dict) -> str:
        p = copy.deepcopy(params)
        p.pop("CheckMacValue", None)  # 驗證時排除自身
        encrypt_type = int(p.get("EncryptType", 1))
        ordered = sorted(p.items(), key=lambda kv: kv[0].lower())
        raw = (
            "HashKey=%s&" % self.hash_key
            + "".join("{}={}&".format(k, v) for k, v in ordered)
            + "HashIV=%s" % self.hash_iv
        )
        enc = urllib.parse.quote_plus(str(raw), safe=_SAFE).lower()
        if encrypt_type == 0:
            return hashlib.md5(enc.encode("utf-8")).hexdigest().upper()
        return hashlib.sha256(enc.encode("utf-8")).hexdigest().upper()

    def verify(self, params: dict) -> bool:
        """驗證回傳參數的 CheckMacValue（等量時間比對）。"""
        received = params.get("CheckMacValue", "")
        expected = self.check_mac_value(params)
        return hmac.compare_digest(str(received).upper(), expected)

    # ── AIO 訂單表單 ──────────────────────────────────────────────────────────
    def build_order_form(
        self,
        *,
        merchant_trade_no: str,
        amount_twd: int,
        item_name: str,
        trade_desc: str,
        return_url: str,
        result_url: str | None = None,
        client_back_url: str | None = None,
        now: datetime.datetime | None = None,
    ) -> dict:
        """組綠界 AIO V5 必填參數 + CheckMacValue（值存原文，由表單/簽章各自處理編碼）。"""
        trade_date = (now or datetime.datetime.now()).strftime("%Y/%m/%d %H:%M:%S")
        params: dict[str, str] = {
            "MerchantID": self.merchant_id,
            "MerchantTradeNo": merchant_trade_no,
            "MerchantTradeDate": trade_date,
            "PaymentType": "aio",
            "TotalAmount": str(int(amount_twd)),
            "TradeDesc": trade_desc,
            "ItemName": item_name,
            "ReturnURL": return_url,
            "ChoosePayment": "ALL",
            "EncryptType": "1",
        }
        if result_url:
            params["OrderResultURL"] = result_url
        if client_back_url:
            params["ClientBackURL"] = client_back_url
        params["CheckMacValue"] = self.check_mac_value(params)
        return params

    # ── 信用卡定期定額（recurring） ─────────────────────────────────────────────
    def build_period_form(
        self,
        *,
        merchant_trade_no: str,
        period_amount_twd: int,
        item_name: str,
        trade_desc: str,
        return_url: str,
        period_return_url: str,
        exec_times: int = 99,
        frequency: int = 1,
        period_type: str = "M",
        client_back_url: str | None = None,
        now: datetime.datetime | None = None,
    ) -> dict:
        """組綠界信用卡定期定額 AIO 表單 + CheckMacValue。

        在一次性表單基礎上：ChoosePayment=Credit、TotalAmount=PeriodAmount、加 Period* 欄位
        與 PeriodReturnURL（每期授權結果回調）。
        """
        trade_date = (now or datetime.datetime.now()).strftime("%Y/%m/%d %H:%M:%S")
        params: dict[str, str] = {
            "MerchantID": self.merchant_id,
            "MerchantTradeNo": merchant_trade_no,
            "MerchantTradeDate": trade_date,
            "PaymentType": "aio",
            "TotalAmount": str(int(period_amount_twd)),
            "TradeDesc": trade_desc,
            "ItemName": item_name,
            "ReturnURL": return_url,
            "ChoosePayment": "Credit",  # 定期定額僅支援信用卡
            "EncryptType": "1",
            "PeriodAmount": str(int(period_amount_twd)),
            "PeriodType": period_type,
            "Frequency": str(int(frequency)),
            "ExecTimes": str(int(exec_times)),
            "PeriodReturnURL": period_return_url,
        }
        if client_back_url:
            params["ClientBackURL"] = client_back_url
        params["CheckMacValue"] = self.check_mac_value(params)
        return params

    def cancel_period(
        self, merchant_trade_no: str, *, now: datetime.datetime | None = None
    ) -> dict:
        """呼叫綠界 CreditCardPeriodAction 停止後續定期定額扣款（Action=Cancel）。

        回傳解析後的回應 dict（綠界以 query-string 回應）。網路/解析失敗會拋例外，
        由呼叫端決定如何處理（退訂時不可放任繼續扣款）。
        """
        ts = int((now or datetime.datetime.now()).timestamp())
        params: dict[str, str] = {
            "MerchantID": self.merchant_id,
            "MerchantTradeNo": merchant_trade_no,
            "Action": "Cancel",
            "TimeStamp": str(ts),
        }
        params["CheckMacValue"] = self.check_mac_value(params)
        raw = self._http_post(self.period_action_url, params)
        parsed = dict(urllib.parse.parse_qsl(raw))
        _log.info(
            "ecpay cancel_period trade_no=%s RtnCode=%s",
            merchant_trade_no, parsed.get("RtnCode"),
        )
        return parsed


class EcpayPaymentProvider(PaymentProvider):
    """綠界 provider：create_checkout 回我方 checkout 頁網址（瀏覽器到該頁自動 submit）。"""

    def __init__(self, *, public_base_url: str | None = None) -> None:
        self._public_base_url = (
            settings.public_base_url if public_base_url is None else public_base_url
        )

    def create_checkout(self, *, order_id: int, amount_cents: int, currency: str) -> str:
        base = self._public_base_url.rstrip("/")
        return f"{base}/payments/ecpay/checkout/{order_id}"

    def name(self) -> str:
        return "ecpay"


def get_ecpay_client(db=None) -> EcpayClient:
    """後台加密設定優先、環境變數備援。回調驗簽也必須使用同一來源。"""
    from saas_mvp.services.platform_payment_config import effective_payment_config

    config = effective_payment_config(db, settings)
    return EcpayClient(
        merchant_id=config.merchant_id,
        hash_key=config.hash_key,
        hash_iv=config.hash_iv,
        env=config.environment,
    )
