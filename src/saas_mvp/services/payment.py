"""金流 provider 抽象 — 比照 translator / line_client 的「先 stub、後接真實」。

支援 Stub、綠界 ECPay、藍新與 LINE Pay；平台後台設定優先於環境備援值，
呼叫端只依賴同一介面。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class PaymentError(Exception):
    """金流呼叫失敗。"""


class PaymentProvider(ABC):
    @abstractmethod
    def create_checkout(self, db, *, order) -> str:
        """建立結帳並回傳付款連結 URL。

        收整個 ``Order``(而非裸 order_id):結帳 URL 以不可猜的
        ``merchant_trade_no`` 為鍵(PEA-3),且 LINE Pay 需把 transactionId
        寫回 order(txid↔order 綁定),兩者都需要 db + order。
        """

    @abstractmethod
    def name(self) -> str:
        ...


class StubPaymentProvider(PaymentProvider):
    """離線假 provider：回傳可預期的假付款連結，不呼叫外部服務。"""

    def create_checkout(self, db, *, order) -> str:
        return (
            f"https://pay.example/stub/checkout?order={order.id}"
            f"&amount={order.total_cents}&cur={order.currency}"
        )

    def name(self) -> str:
        return "stub"


def get_payment_provider(db=None) -> PaymentProvider:
    """FastAPI dependency / 服務用：依設定回傳 provider。

    未知值一律回 StubPaymentProvider（安全預設）。
    """
    from saas_mvp.config import settings
    from saas_mvp.services.platform_payment_config import effective_payment_config

    config = effective_payment_config(db, settings)
    if config.provider == "ecpay":
        from saas_mvp.services.payment_ecpay import EcpayPaymentProvider
        return EcpayPaymentProvider(public_base_url=settings.public_base_url)
    if config.provider == "newebpay":
        from saas_mvp.services.payment_newebpay import NewebPayProvider
        return NewebPayProvider()
    if config.provider == "linepay":
        from saas_mvp.services.payment_linepay import LinePayPaymentProvider
        return LinePayPaymentProvider()
    return StubPaymentProvider()
