"""藍新金流 NewebPay MPG（幕前）串接。

加解密**對齊藍新官方文件**：
* TradeInfo = AES-256-CBC( query_string, key=HashKey, iv=HashIV, PKCS7 padding )，輸出 hex。
* TradeSha  = SHA256( "HashKey={key}&{TradeInfo}&HashIV={iv}" ) 大寫。
不引入藍新 SDK 當 runtime 依賴；AES 用既有依賴 cryptography。

流程：顧客下單 → checkout 頁自動 submit 表單到藍新 MPG 付款頁 → 藍新 server 回調
NotifyURL（POST，含 TradeInfo + TradeSha）→ 先驗 TradeSha + 解密 TradeInfo 取狀態
再標記訂單已付 → 回 200。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from saas_mvp.config import settings
from saas_mvp.services.payment import PaymentProvider

# MPG 付款閘道（V1.6）
_MPG_STAGE = "https://ccore.newebpay.com/MPG/mpg_gateway"
_MPG_PROD = "https://core.newebpay.com/MPG/mpg_gateway"

_VERSION = "2.0"


class NewebPayClient:
    """藍新 MPG TradeInfo/TradeSha 產生/驗證 + 訂單表單組裝（config 驅動）。"""

    def __init__(
        self,
        *,
        merchant_id: str | None = None,
        hash_key: str | None = None,
        hash_iv: str | None = None,
        env: str | None = None,
    ) -> None:
        self.merchant_id = (
            merchant_id if merchant_id is not None else settings.newebpay_merchant_id
        )
        self.hash_key = hash_key if hash_key is not None else settings.newebpay_hash_key
        self.hash_iv = hash_iv if hash_iv is not None else settings.newebpay_hash_iv
        self.env = env if env is not None else settings.newebpay_env

    @property
    def mpg_url(self) -> str:
        return _MPG_PROD if self.env == "prod" else _MPG_STAGE

    # ── AES-256-CBC（key=HashKey, iv=HashIV, PKCS7） ──────────────────────────
    def _key_iv(self) -> tuple[bytes, bytes]:
        return self.hash_key.encode("utf-8"), self.hash_iv.encode("utf-8")

    def encrypt_trade_info(self, params: dict) -> str:
        """把 params 編成 query string 後 AES 加密，回 hex（即 TradeInfo）。"""
        plain = urllib.parse.urlencode(params).encode("utf-8")
        key, iv = self._key_iv()
        padder = padding.PKCS7(algorithms.AES.block_size).padder()
        padded = padder.update(plain) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        ct = encryptor.update(padded) + encryptor.finalize()
        return ct.hex()

    def decrypt_trade_info(self, trade_info: str) -> dict:
        """AES 解密 hex TradeInfo，回 dict。

        RespondType=JSON（本服務下單一律用 JSON）時，藍新回傳的解密明文為
        JSON 物件，欄位包在 ``Result`` 內，例如::

            {"Status": "SUCCESS",
             "Result": {"MerchantOrderNo": "...", "Amt": 100, ...}}

        故 payload 看起來像 JSON（以 ``{`` 開頭）時以 JSON 解析、回傳巢狀 dict；
        否則回退到舊版 query-string（``a=b&c=d``）解析以維持向後相容。
        """
        key, iv = self._key_iv()
        ct = bytes.fromhex(trade_info)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ct) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        plain = (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
        stripped = plain.lstrip()
        if stripped.startswith("{"):
            # 真實藍新 JSON 回應：回傳巢狀 dict（含 Status / Result）。
            return json.loads(stripped)
        # 向後相容：舊版 query string 形式。
        return dict(urllib.parse.parse_qsl(plain, keep_blank_values=True))

    # ── TradeSha（SHA256 大寫） ───────────────────────────────────────────────
    def trade_sha(self, trade_info: str) -> str:
        raw = "HashKey=%s&%s&HashIV=%s" % (self.hash_key, trade_info, self.hash_iv)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()

    def verify(self, params: dict) -> bool:
        """驗證回傳的 TradeSha（等量時間比對）。"""
        trade_info = params.get("TradeInfo", "")
        received = params.get("TradeSha", "")
        expected = self.trade_sha(trade_info)
        return hmac.compare_digest(str(received).upper(), expected)

    # ── MPG 訂單表單 ──────────────────────────────────────────────────────────
    def build_order_form(
        self,
        *,
        merchant_trade_no: str,
        amount_twd: int,
        item_desc: str,
        return_url: str,
        notify_url: str,
        client_back_url: str | None = None,
        email: str | None = None,
    ) -> dict:
        """組藍新 MPG 必填 trade-info → 加密 → 回前端 form 參數（含 TradeSha）。"""
        trade_info_params: dict[str, str] = {
            "MerchantID": self.merchant_id,
            "RespondType": "JSON",
            "TimeStamp": str(int(time.time())),
            "Version": "1.6",
            "MerchantOrderNo": merchant_trade_no,
            "Amt": str(int(amount_twd)),
            "ItemDesc": item_desc,
            "ReturnURL": return_url,
            "NotifyURL": notify_url,
        }
        if client_back_url:
            trade_info_params["ClientBackURL"] = client_back_url
        if email:
            trade_info_params["Email"] = email
        trade_info = self.encrypt_trade_info(trade_info_params)
        return {
            "MerchantID": self.merchant_id,
            "TradeInfo": trade_info,
            "TradeSha": self.trade_sha(trade_info),
            "Version": _VERSION,
        }


class NewebPayProvider(PaymentProvider):
    """藍新 provider：create_checkout 回我方 checkout 頁網址（瀏覽器到該頁自動 submit）。"""

    def create_checkout(self, *, order_id: int, amount_cents: int, currency: str) -> str:
        base = settings.public_base_url.rstrip("/")
        return f"{base}/payments/newebpay/checkout/{order_id}"

    def name(self) -> str:
        return "newebpay"
