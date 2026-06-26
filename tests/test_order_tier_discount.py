"""shop.create_order 會員等級折扣 + 套券（讓 REST 訂單 / LINE 站內購買與 POS 一致）。"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.db import Base, import_all_models  # noqa: E402

import_all_models()

from saas_mvp.models.coupon import Coupon  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import shop as shop_svc  # noqa: E402

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


def _tenant(db) -> int:
    t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
    db.add(t)
    db.flush()
    return t.id


def _member(db, tid, *, line, tier="gold") -> Customer:
    c = Customer(tenant_id=tid, line_user_id=line, display_name="會員", tier=tier)
    db.add(c)
    db.commit()
    return c


def _product(db, tid, *, price, stock=10):
    from saas_mvp.models.product import Product
    p = Product(tenant_id=tid, name="商品", price_cents=price, stock=stock)
    db.add(p)
    db.commit()
    return p


def test_member_tier_discount_applied(db):
    tid = _tenant(db)
    _member(db, tid, line="Ugold", tier="gold")
    p = _product(db, tid, price=10000)
    order = shop_svc.create_order(
        db, tenant_id=tid, items=[(p.id, 1)], line_user_id="Ugold"
    )
    assert order.discount_cents == 1000  # gold 10%
    assert order.total_cents == 9000


def test_non_member_no_discount(db):
    tid = _tenant(db)
    p = _product(db, tid, price=10000)
    # line_user_id 無對應建檔顧客 → 散客，不折
    order = shop_svc.create_order(
        db, tenant_id=tid, items=[(p.id, 1)], line_user_id="Unobody"
    )
    assert order.discount_cents == 0
    assert order.total_cents == 10000


def test_member_tier_plus_coupon_stack(db):
    tid = _tenant(db)
    _member(db, tid, line="Ustk", tier="gold")
    p = _product(db, tid, price=10000)
    db.add(Coupon(
        tenant_id=tid, code="C100", name="折100",
        discount_type="amount", discount_value=100,
    ))
    db.commit()
    order = shop_svc.create_order(
        db, tenant_id=tid, items=[(p.id, 1)], line_user_id="Ustk", coupon_code="C100"
    )
    # 10000 → gold 10% off → 9000 → 券折 100 → 8900
    assert order.discount_cents == 1100
    assert order.total_cents == 8900
    assert order.coupon_code == "C100"


def test_regular_member_no_tier_discount(db):
    tid = _tenant(db)
    _member(db, tid, line="Ureg", tier="regular")
    p = _product(db, tid, price=10000)
    order = shop_svc.create_order(
        db, tenant_id=tid, items=[(p.id, 1)], line_user_id="Ureg"
    )
    assert order.discount_cents == 0


def test_line_buy_reply_applies_member_and_coupon(db):
    """LINE 站內購買(_buy_reply)套用會員等級折扣 + 券,回覆顯示折抵。"""
    from saas_mvp.routers import line_webhook as lw
    tid = _tenant(db)
    _member(db, tid, line="Ubuy", tier="gold")
    p = _product(db, tid, price=10000)
    db.add(Coupon(
        tenant_id=tid, code="C100", name="折100",
        discount_type="amount", discount_value=100,
    ))
    db.commit()
    reply = lw._buy_reply(db, tid, p.id, 1, "Ubuy", "C100")
    assert "已折抵" in reply and "1100" in reply  # gold 1000 + 券 100
    assert "8900" in reply  # 應付


def test_line_buy_reply_bad_coupon_message(db):
    from saas_mvp.routers import line_webhook as lw
    tid = _tenant(db)
    _member(db, tid, line="Ubad", tier="regular")
    p = _product(db, tid, price=10000)
    reply = lw._buy_reply(db, tid, p.id, 1, "Ubad", "NOPE")
    assert "優惠券無法套用" in reply
