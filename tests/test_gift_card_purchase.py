"""R11-A — 顧客線上購買禮物卡(公開頁+金流 callback 發卡)。"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db, import_all_models  # noqa: E402

import_all_models()

from saas_mvp.models.business_profile import BusinessProfile  # noqa: E402
from saas_mvp.models.gift_card import GiftCard, GiftCardLedger  # noqa: E402
from saas_mvp.models.order import ORDER_PAID, Order  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import gift_card_sales as sales_svc  # noqa: E402
from saas_mvp.services import shop as shop_svc  # noqa: E402


@pytest.fixture(autouse=True)
def _real_provider(monkeypatch):
    """sale_available 要求真實金流(stub 會 404 整條購卡流)。"""
    from saas_mvp.config import settings

    monkeypatch.setattr(settings, "payment_provider", "ecpay")
    monkeypatch.setattr(settings, "public_base_url", "https://shop.example")


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)
    app = create_app()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, session_factory


def _setup_tenant(
    session_factory,
    *,
    enabled: bool = True,
    feature_on: bool = True,
    slug: str | None = None,
) -> tuple[int, str]:
    slug = slug or f"shop-{uuid.uuid4().hex[:8]}"
    db = session_factory()
    try:
        tenant = Tenant(name=f"t-{slug}", plan="free")
        db.add(tenant)
        db.flush()
        db.add(BusinessProfile(tenant_id=tenant.id, slug=slug, is_published=True))
        features_svc.set_enabled(
            db, tenant.id, features_svc.GIFT_CARDS, feature_on,
            actor_user_id=None, source="admin",
        )
        if enabled:
            sales_svc.save_config(
                db,
                tenant_id=tenant.id,
                online_sale_enabled=True,
                denominations=[500, 1000],
                fulfillment_guarantee="本店禮物卡已依禮券定型化契約辦理履約保障。",
                updated_by_user_id=None,
            )
        db.commit()
        return tenant.id, slug
    finally:
        db.close()


_FORM = {
    "amount_twd": 1000,
    "purchaser_email": "buyer@example.com",
    "purchaser_name": "買家",
    "recipient_name": "收禮人",
    "message": "生日快樂",
    "agree": "true",
}


class TestPublicPages:
    def test_buy_form_renders(self, env):
        client, sf = env
        _, slug = _setup_tenant(sf)
        r = client.get(f"/p/{slug}/gift-cards")
        assert r.status_code == 200
        assert "NT$1,000" in r.text
        assert "履約保障" in r.text

    def test_disabled_or_feature_off_404(self, env):
        client, sf = env
        _, slug1 = _setup_tenant(sf, enabled=False)
        assert client.get(f"/p/{slug1}/gift-cards").status_code == 404
        _, slug2 = _setup_tenant(sf, feature_on=False)
        assert client.get(f"/p/{slug2}/gift-cards").status_code == 404

    def test_submit_requires_agree_and_valid_amount(self, env):
        client, sf = env
        _, slug = _setup_tenant(sf)
        r = client.post(
            f"/p/{slug}/gift-cards", data={**_FORM, "agree": ""},
            follow_redirects=False,
        )
        assert r.status_code == 400
        assert "履約保障" in r.text
        r2 = client.post(
            f"/p/{slug}/gift-cards", data={**_FORM, "amount_twd": 777},
            follow_redirects=False,
        )
        assert r2.status_code == 400
        assert "面額" in r2.text

    def test_submit_creates_order_and_redirects_to_checkout(self, env):
        client, sf = env
        tid, slug = _setup_tenant(sf)
        r = client.post(f"/p/{slug}/gift-cards", data=_FORM, follow_redirects=False)
        assert r.status_code == 303, r.text
        assert "/payments/ecpay/checkout/" in r.headers["location"]
        db = sf()
        try:
            order = db.query(Order).filter(Order.tenant_id == tid).one()
            assert order.total_cents == 100000
            purchase = sales_svc.purchase_for_order(db, order.id)
            assert purchase is not None
            assert purchase.purchaser_email == "buyer@example.com"
            assert purchase.status == "pending"
        finally:
            db.close()


class TestPaidCallbackIssuance:
    def _buy(self, client, sf, slug: str, tid: int) -> str:
        r = client.post(f"/p/{slug}/gift-cards", data=_FORM, follow_redirects=False)
        assert r.status_code == 303
        db = sf()
        try:
            order = (
                db.query(Order)
                .filter(Order.tenant_id == tid)
                .order_by(Order.id.desc())
                .first()
            )
            return order.merchant_trade_no
        finally:
            db.close()

    def test_paid_issues_card_atomically_and_replay_safe(self, env):
        client, sf = env
        tid, slug = _setup_tenant(sf)
        trade_no = self._buy(client, sf, slug, tid)
        db = sf()
        try:
            order = shop_svc.get_order_by_trade_no(db, trade_no)
            shop_svc.mark_order_paid(
                db, tenant_id=tid, order_id=order.id, provider="ecpay",
                provider_trade_no="ECP123",
            )
            # 重送 callback(冪等)
            shop_svc.mark_order_paid(
                db, tenant_id=tid, order_id=order.id, provider="ecpay",
            )
            cards = db.query(GiftCard).filter(GiftCard.tenant_id == tid).all()
            assert len(cards) == 1
            assert cards[0].initial_value_cents == 100000
            assert cards[0].purchaser_name == "買家"
            ledger = (
                db.query(GiftCardLedger)
                .filter(GiftCardLedger.gift_card_id == cards[0].id)
                .all()
            )
            assert [row.kind for row in ledger] == ["issue"]
            purchase = sales_svc.purchase_for_order(db, order.id)
            assert purchase.status == "issued"
            assert purchase.gift_card_id == cards[0].id
            assert purchase.plain_code  # 明碼可重覆取用
            assert order.status == ORDER_PAID
        finally:
            db.close()

    def test_status_page_shows_code_after_paid(self, env):
        client, sf = env
        tid, slug = _setup_tenant(sf)
        trade_no = self._buy(client, sf, slug, tid)
        # 未付款:pending 畫面
        r = client.get(f"/p/{slug}/gift-cards/{trade_no}")
        assert r.status_code == 200
        assert "付款確認中" in r.text
        db = sf()
        try:
            order = shop_svc.get_order_by_trade_no(db, trade_no)
            shop_svc.mark_order_paid(db, tenant_id=tid, order_id=order.id)
            purchase = sales_svc.purchase_for_order(db, order.id)
            code = purchase.plain_code
        finally:
            db.close()
        r2 = client.get(f"/p/{slug}/gift-cards/{trade_no}")
        assert r2.status_code == 200
        assert code in r2.text
        assert "buyer@example.com" in r2.text

    def test_status_page_cross_tenant_404(self, env):
        client, sf = env
        tid, slug = _setup_tenant(sf)
        trade_no = self._buy(client, sf, slug, tid)
        _, other_slug = _setup_tenant(sf)
        assert client.get(f"/p/{other_slug}/gift-cards/{trade_no}").status_code == 404

    def test_delivery_email_queued_once(self, env):
        client, sf = env
        tid, slug = _setup_tenant(sf)
        trade_no = self._buy(client, sf, slug, tid)
        db = sf()
        try:
            from saas_mvp.models.email_delivery import EmailDelivery

            order = shop_svc.get_order_by_trade_no(db, trade_no)
            shop_svc.mark_order_paid(db, tenant_id=tid, order_id=order.id)
            shop_svc.mark_order_paid(db, tenant_id=tid, order_id=order.id)  # 重送
            rows = (
                db.query(EmailDelivery)
                .filter(EmailDelivery.category == "gift_card_purchase")
                .all()
            )
            assert len(rows) == 1
            assert rows[0].recipient == "buyer@example.com"
            purchase = sales_svc.purchase_for_order(db, order.id)
            assert purchase.email_queued_at is not None
        finally:
            db.close()


class TestConfigService:
    def test_enable_requires_guarantee_and_denoms(self, env):
        _, sf = env
        tid, _ = _setup_tenant(sf, enabled=False)
        db = sf()
        try:
            with pytest.raises(sales_svc.GiftCardSaleError):
                sales_svc.save_config(
                    db, tenant_id=tid, online_sale_enabled=True,
                    denominations=[], fulfillment_guarantee="夠長的履約保障文案十字以上",
                    updated_by_user_id=None,
                )
            with pytest.raises(sales_svc.GiftCardSaleError):
                sales_svc.save_config(
                    db, tenant_id=tid, online_sale_enabled=True,
                    denominations=[500], fulfillment_guarantee="太短",
                    updated_by_user_id=None,
                )
            with pytest.raises(sales_svc.GiftCardSaleError):
                sales_svc.save_config(
                    db, tenant_id=tid, online_sale_enabled=True,
                    denominations=[50], fulfillment_guarantee="夠長的履約保障文案十字以上",
                    updated_by_user_id=None,
                )
        finally:
            db.close()


class TestAdversarialRegressions:
    """R11-A 對抗審查確認缺陷的回歸鎖。"""

    def _paid_purchase(self, client, sf, slug: str, tid: int):
        r = client.post(f"/p/{slug}/gift-cards", data=_FORM, follow_redirects=False)
        assert r.status_code == 303
        db = sf()
        try:
            order = (
                db.query(Order)
                .filter(Order.tenant_id == tid)
                .order_by(Order.id.desc())
                .first()
            )
            # stub provider 的退款路徑不需外部交易編號,便於驗證 void 鉤子
            shop_svc.mark_order_paid(
                db, tenant_id=tid, order_id=order.id,
                provider="stub", provider_trade_no="STUB1",
            )
            return order.id
        finally:
            db.close()

    def test_stub_provider_gates_public_page_404(self, env, monkeypatch):
        from saas_mvp.config import settings

        client, sf = env
        _, slug = _setup_tenant(sf)
        monkeypatch.setattr(settings, "payment_provider", "stub")
        assert client.get(f"/p/{slug}/gift-cards").status_code == 404

    def test_status_page_post_prg_303(self, env):
        client, sf = env
        tid, slug = _setup_tenant(sf)
        client.post(f"/p/{slug}/gift-cards", data=_FORM, follow_redirects=False)
        db = sf()
        try:
            order = db.query(Order).filter(Order.tenant_id == tid).one()
            trade_no = order.merchant_trade_no
        finally:
            db.close()
        # NewebPay ReturnURL form POST → PRG 303 到 GET
        r2 = client.post(
            f"/p/{slug}/gift-cards/{trade_no}",
            data={"TradeInfo": "xxx"},
            follow_redirects=False,
        )
        assert r2.status_code == 303
        assert r2.headers["location"].endswith(f"/gift-cards/{trade_no}")

    def test_cancel_order_blocked_for_purchases(self, env):
        client, sf = env
        tid, slug = _setup_tenant(sf)
        client.post(f"/p/{slug}/gift-cards", data=_FORM, follow_redirects=False)
        db = sf()
        try:
            from fastapi import HTTPException

            order = db.query(Order).filter(Order.tenant_id == tid).one()
            with pytest.raises(HTTPException) as exc:
                shop_svc.cancel_order(db, tenant_id=tid, order_id=order.id)
            assert exc.value.status_code == 409
        finally:
            db.close()

    def test_refund_voids_card_and_blocks_after_redeem(self, env):
        from saas_mvp.services import gift_cards as gift_cards_svc
        from saas_mvp.services import order_refund as refund_svc

        client, sf = env
        tid, slug = _setup_tenant(sf)
        order_id = self._paid_purchase(client, sf, slug, tid)
        db = sf()
        try:
            purchase = sales_svc.purchase_for_order(db, order_id)
            # 全額退款 → 卡同交易作廢、餘額歸零
            refund_svc.request_order_refund(
                db, tenant_id=tid, order_id=order_id, actor_user_id=1,
            )
            card = db.get(GiftCard, purchase.gift_card_id)
            assert card.status == "void"
            assert (
                gift_cards_svc.balance_cents(
                    db, tenant_id=tid, gift_card_id=card.id
                )
                == 0
            )
        finally:
            db.close()
        # 第二單:先折抵再退款 → 擋下
        order_id2 = self._paid_purchase(client, sf, slug, tid)
        db = sf()
        try:
            from saas_mvp.models.gift_card import GiftCardLedger

            purchase2 = sales_svc.purchase_for_order(db, order_id2)
            db.add(GiftCardLedger(
                tenant_id=tid, gift_card_id=purchase2.gift_card_id,
                delta_cents=-10000, kind="redeem", order_id=None,
            ))
            db.commit()
            with pytest.raises(refund_svc.OrderRefundError):
                refund_svc.request_order_refund(
                    db, tenant_id=tid, order_id=order_id2, actor_user_id=1,
                )
        finally:
            db.close()

    def test_replay_heals_lost_delivery_email(self, env):
        client, sf = env
        tid, slug = _setup_tenant(sf)
        order_id = self._paid_purchase(client, sf, slug, tid)
        db = sf()
        try:
            from saas_mvp.models.email_delivery import EmailDelivery

            # 模擬「commit 後、寄信前崩潰」:清掉 email 痕跡
            purchase = sales_svc.purchase_for_order(db, order_id)
            purchase.email_queued_at = None
            db.query(EmailDelivery).filter(
                EmailDelivery.category == "gift_card_purchase"
            ).delete()
            db.commit()
            # callback replay(ORDER_PAID 分支)須自癒補寄
            shop_svc.mark_order_paid(db, tenant_id=tid, order_id=order_id)
            rows = (
                db.query(EmailDelivery)
                .filter(EmailDelivery.category == "gift_card_purchase")
                .count()
            )
            assert rows == 1
        finally:
            db.close()

    def test_tenant_hourly_flood_gate(self, env):
        client, sf = env
        tid, slug = _setup_tenant(sf)
        db = sf()
        try:
            for _ in range(30):
                sales_svc.start_purchase(
                    db, tenant_id=tid, amount_twd=500,
                    purchaser_email="flood@example.com",
                )
            with pytest.raises(sales_svc.GiftCardSaleError):
                sales_svc.start_purchase(
                    db, tenant_id=tid, amount_twd=500,
                    purchaser_email="flood@example.com",
                )
        finally:
            db.close()
