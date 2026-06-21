"""金流 provider 抽象 — 比照 translator / line_client 的「先 stub、後接真實」。

本輪只實作 StubPaymentProvider（回傳假 checkout URL，供開發/測試）。
真實 provider（綠界 ECPay / Stripe / LINE Pay…）需使用者指定後，以同一介面接上，
不影響 services/shop 與 routers/orders。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class PaymentError(Exception):
    """金流呼叫失敗。"""


class PaymentProvider(ABC):
    @abstractmethod
    def create_checkout(self, *, order_id: int, amount_cents: int, currency: str) -> str:
        """建立結帳並回傳付款連結 URL。"""

    @abstractmethod
    def name(self) -> str:
        ...


class StubPaymentProvider(PaymentProvider):
    """離線假 provider：回傳可預期的假付款連結，不呼叫外部服務。"""

    def create_checkout(self, *, order_id: int, amount_cents: int, currency: str) -> str:
        return f"https://pay.example/stub/checkout?order={order_id}&amount={amount_cents}&cur={currency}"

    def name(self) -> str:
        return "stub"


def get_payment_provider() -> PaymentProvider:
    """FastAPI dependency / 服務用：依設定回傳 provider。

    目前僅 ``stub``；未知值一律回 StubPaymentProvider（安全預設）。
    """
    from saas_mvp.config import settings

    if settings.payment_provider == "stub":
        return StubPaymentProvider()
    # 未來：elif settings.payment_provider == "ecpay": return EcpayProvider(...)
    return StubPaymentProvider()
