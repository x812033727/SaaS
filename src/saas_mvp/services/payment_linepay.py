"""LINE Pay v3（E2）— 一次性訂單結帳（orders/POS 流）。

範圍取捨:**只做一次性付款**;訂閱續留 ECPay 定期定額(LINE Pay 預授權
RegKey 申請門檻高),列 KNOWN_LIMITATIONS。

簽章(v3):``HMAC-SHA256(channel_secret, channel_secret + uri + body + nonce)``
base64,headers X-LINE-ChannelId / X-LINE-Authorization-Nonce / X-LINE-Authorization。
比照 payment_ecpay 慣例:stdlib urllib、http_post 可注入、不引 SDK。
sandbox:https://sandbox-api-pay.line.me;prod:https://api-pay.line.me。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.request
import uuid

from saas_mvp.config import settings
from saas_mvp.services.payment import PaymentProvider

_log = logging.getLogger(__name__)

_SANDBOX_BASE = "https://sandbox-api-pay.line.me"
_PROD_BASE = "https://api-pay.line.me"

# Confirm API「交易已處理過」錯誤碼:重放時視為成功(冪等)。
_ALREADY_CONFIRMED_CODES = {"1169", "1172"}


class LinePayError(Exception):
    """LINE Pay API 失敗(網路/簽章/回應碼統一包裝)。"""


def sign(channel_secret: str, uri: str, body: str, nonce: str) -> str:
    """v3 簽章;獨立函式供已知向量單測。"""
    message = (channel_secret + uri + body + nonce).encode()
    digest = hmac.new(channel_secret.encode(), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _urllib_post(url: str, body: bytes, headers: dict) -> str:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode()


class LinePayClient:
    """v3 API client(request/confirm);http_post 可注入供離線測試。"""

    def __init__(self, *, http_post=None) -> None:
        self._channel_id = settings.line_pay_channel_id
        self._channel_secret = settings.line_pay_channel_secret
        self._base = _PROD_BASE if settings.line_pay_env == "prod" else _SANDBOX_BASE
        self._http_post = http_post or _urllib_post

    def _call(self, uri: str, payload: dict) -> dict:
        if not (self._channel_id and self._channel_secret):
            raise LinePayError("line pay credentials not configured")
        body = json.dumps(payload, separators=(",", ":"))
        nonce = str(uuid.uuid4())
        headers = {
            "Content-Type": "application/json",
            "X-LINE-ChannelId": self._channel_id,
            "X-LINE-Authorization-Nonce": nonce,
            "X-LINE-Authorization": sign(self._channel_secret, uri, body, nonce),
        }
        try:
            raw = self._http_post(self._base + uri, body.encode(), headers)
            return json.loads(raw)
        except LinePayError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LinePayError(f"line pay request failed: {exc}") from exc

    def request_payment(
        self, *, order_id: int, amount_twd: int, currency: str,
        confirm_url: str, cancel_url: str, item_name: str,
    ) -> dict:
        """POST /v3/payments/request → {transaction_id, payment_url}。"""
        resp = self._call("/v3/payments/request", {
            "amount": amount_twd,
            "currency": currency,
            "orderId": str(order_id),
            "packages": [{
                "id": f"pkg{order_id}",
                "amount": amount_twd,
                "products": [{
                    "name": item_name[:100],
                    "quantity": 1,
                    "price": amount_twd,
                }],
            }],
            "redirectUrls": {
                "confirmUrl": confirm_url,
                "cancelUrl": cancel_url,
            },
        })
        if str(resp.get("returnCode")) != "0000":
            raise LinePayError(
                f"request rejected: {resp.get('returnCode')} {resp.get('returnMessage')}"
            )
        info = resp.get("info") or {}
        return {
            "transaction_id": str(info.get("transactionId") or ""),
            "payment_url": ((info.get("paymentUrl") or {}).get("web")) or "",
        }

    def confirm_payment(
        self, *, transaction_id: str, amount_twd: int, currency: str
    ) -> dict:
        """POST /v3/payments/{txid}/confirm。已 confirm 的錯誤碼視為成功(冪等)。"""
        resp = self._call(f"/v3/payments/{transaction_id}/confirm", {
            "amount": amount_twd,
            "currency": currency,
        })
        code = str(resp.get("returnCode"))
        if code == "0000" or code in _ALREADY_CONFIRMED_CODES:
            return resp
        raise LinePayError(
            f"confirm rejected: {code} {resp.get('returnMessage')}"
        )


class LinePayPaymentProvider(PaymentProvider):
    """orders/POS 用 provider:create_checkout 打 Request API 回 LINE Pay 付款頁。"""

    def __init__(self, *, client: LinePayClient | None = None) -> None:
        self._client = client or LinePayClient()

    def create_checkout(self, db, *, order) -> str:
        from saas_mvp.services import shop as shop_svc

        trade_no = shop_svc.ensure_order_trade_no(db, order)
        base = settings.public_base_url.rstrip("/")
        result = self._client.request_payment(
            order_id=trade_no,
            amount_twd=order.total_cents // 100,
            currency=(order.currency or "TWD"),
            confirm_url=f"{base}/payments/linepay/confirm?orderId={trade_no}",
            cancel_url=f"{base}/payments/linepay/cancel?orderId={trade_no}",
            item_name=f"訂單 {order.id}",
        )
        # txid↔order 綁定:confirm 時必須與 query string 比對(不可信任外部值)。
        # 重新產生付款連結會覆寫為最新 txid —— 舊連結的 confirm 自然失效。
        order.payment_txn_id = result["transaction_id"] or None
        db.commit()
        return result["payment_url"]

    def name(self) -> str:
        return "linepay"
