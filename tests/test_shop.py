"""商品銷售 service + 金流 stub 測試（DB 直連）。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t  # noqa: F401
from saas_mvp.models import customer as _c  # noqa: F401
from saas_mvp.models import product as _p, order as _o, order_item as _oi  # noqa: F401

from saas_mvp.db import Base
from saas_mvp.models.product import Product
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import shop as shop_svc
from saas_mvp.services.payment import StubPaymentProvider, get_payment_provider

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db, name="shop") -> int:
    t = Tenant(name=name, plan="free")
    db.add(t)
    db.commit()
    return t.id


def _product(db, tid, *, price=100, stock=None, active=True) -> int:
    p = shop_svc.create_product(db, tenant_id=tid, name="珍奶", price_cents=price, stock=stock)
    if not active:
        p.is_active = False
        db.commit()
    return p.id


class TestOrder:
    def test_create_order_total_and_stock(self, db):
        tid = _tenant(db)
        pid = _product(db, tid, price=50, stock=10)
        order = shop_svc.create_order(db, tenant_id=tid, items=[(pid, 3)], line_user_id="U1")
        assert order.total_cents == 150
        assert db.get(Product, pid).stock == 7
        items = shop_svc.list_order_items(db, tenant_id=tid, order_id=order.id)
        assert items[0].unit_price_cents == 50 and items[0].line_total_cents == 150

    def test_oversell_rejected(self, db):
        tid = _tenant(db)
        pid = _product(db, tid, stock=2)
        with pytest.raises(shop_svc.OutOfStock):
            shop_svc.create_order(db, tenant_id=tid, items=[(pid, 5)], line_user_id="U1")
        assert db.get(Product, pid).stock == 2  # 未扣

    def test_unlimited_stock(self, db):
        tid = _tenant(db)
        pid = _product(db, tid, stock=None)
        order = shop_svc.create_order(db, tenant_id=tid, items=[(pid, 99)], line_user_id="U1")
        assert order.total_cents == 99 * 100

    def test_inactive_product_rejected(self, db):
        tid = _tenant(db)
        pid = _product(db, tid, stock=5, active=False)
        with pytest.raises(shop_svc.ProductInactive):
            shop_svc.create_order(db, tenant_id=tid, items=[(pid, 1)], line_user_id="U1")

    def test_price_snapshot_survives_price_change(self, db):
        tid = _tenant(db)
        pid = _product(db, tid, price=100, stock=10)
        order = shop_svc.create_order(db, tenant_id=tid, items=[(pid, 1)], line_user_id="U1")
        shop_svc.update_product(db, tenant_id=tid, product_id=pid, price_cents=999)
        items = shop_svc.list_order_items(db, tenant_id=tid, order_id=order.id)
        assert items[0].unit_price_cents == 100  # 快照不受改價影響

    def test_cancel_restores_stock(self, db):
        tid = _tenant(db)
        pid = _product(db, tid, stock=10)
        order = shop_svc.create_order(db, tenant_id=tid, items=[(pid, 4)], line_user_id="U1")
        assert db.get(Product, pid).stock == 6
        shop_svc.cancel_order(db, tenant_id=tid, order_id=order.id)
        assert db.get(Product, pid).stock == 10
        # 重複取消不再回補
        shop_svc.cancel_order(db, tenant_id=tid, order_id=order.id)
        assert db.get(Product, pid).stock == 10

    def test_mark_paid(self, db):
        tid = _tenant(db)
        pid = _product(db, tid, stock=10)
        order = shop_svc.create_order(db, tenant_id=tid, items=[(pid, 1)], line_user_id="U1")
        paid = shop_svc.mark_order_paid(db, tenant_id=tid, order_id=order.id)
        assert paid.status == "paid" and paid.paid_at is not None

    def test_cross_tenant_order_not_found(self, db):
        tid = _tenant(db)
        other = _tenant(db, name="other")
        pid = _product(db, tid, stock=10)
        order = shop_svc.create_order(db, tenant_id=tid, items=[(pid, 1)], line_user_id="U1")
        with pytest.raises(shop_svc.OrderNotFound):
            shop_svc.get_order(db, tenant_id=other, order_id=order.id)


class TestOrderTradeNo:
    def test_create_order_assigns_unguessable_trade_no(self, db):
        """建單即產生不可猜 trade_no(PEA-3):OD 前綴、20 字、含隨機段。"""
        tid = _tenant(db)
        pid = _product(db, tid, stock=10)
        o1 = shop_svc.create_order(db, tenant_id=tid, items=[(pid, 1)], line_user_id="U1")
        o2 = shop_svc.create_order(db, tenant_id=tid, items=[(pid, 1)], line_user_id="U1")
        assert o1.merchant_trade_no and o1.merchant_trade_no.startswith("OD")
        assert len(o1.merchant_trade_no) == 20
        assert o1.merchant_trade_no != o2.merchant_trade_no


class TestPaymentStub:
    def test_stub_checkout_url(self):
        from types import SimpleNamespace

        provider = get_payment_provider()
        assert isinstance(provider, StubPaymentProvider)
        order = SimpleNamespace(id=5, total_cents=150, currency="TWD")
        url = provider.create_checkout(None, order=order)
        assert "order=5" in url and "amount=150" in url
