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
import urllib.parse

from saas_mvp.config import settings
from saas_mvp.services.payment import PaymentProvider

_AIO_STAGE = "https://payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5"
_AIO_PROD = "https://payment.ecpay.com.tw/Cashier/AioCheckOut/V5"

# 綠界官方 quote_plus 的 safe 字元集（與官方 SDK 相同）
_SAFE = "-_.!*()"


class EcpayClient:
    """綠界 CheckMacValue 產生/驗證 + AIO 訂單表單組裝（config 驅動）。"""

    def __init__(
        self,
        *,
        merchant_id: str | None = None,
        hash_key: str | None = None,
        hash_iv: str | None = None,
        env: str | None = None,
    ) -> None:
        self.merchant_id = merchant_id if merchant_id is not None else settings.ecpay_merchant_id
        self.hash_key = hash_key if hash_key is not None else settings.ecpay_hash_key
        self.hash_iv = hash_iv if hash_iv is not None else settings.ecpay_hash_iv
        self.env = env if env is not None else settings.ecpay_env

    @property
    def aio_url(self) -> str:
        return _AIO_PROD if self.env == "prod" else _AIO_STAGE

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


class EcpayPaymentProvider(PaymentProvider):
    """綠界 provider：create_checkout 回我方 checkout 頁網址（瀏覽器到該頁自動 submit）。"""

    def create_checkout(self, *, order_id: int, amount_cents: int, currency: str) -> str:
        base = settings.public_base_url.rstrip("/")
        return f"{base}/payments/ecpay/checkout/{order_id}"

    def name(self) -> str:
        return "ecpay"
