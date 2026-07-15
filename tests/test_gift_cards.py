"""禮物卡：安全發行、領取、POS 分次折抵、退款、隔離與法規欄位。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base
from saas_mvp.models.customer import Customer
from saas_mvp.models.gift_card import GiftCard, GiftCardLedger
from saas_mvp.models.product import Product
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import gift_cards as gift_cards_svc
from saas_mvp.services import pos as pos_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.booking.commands import parse_booking_command
from saas_mvp.routers.line_webhook import _gift_cards_reply

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    with _Session() as session:
        yield session


def _seed(db, suffix="a"):
    tenant = Tenant(name=f"gift-{suffix}-{uuid.uuid4().hex[:6]}", plan="pro")
    db.add(tenant)
    db.flush()
    customer = Customer(
        tenant_id=tenant.id, line_user_id=f"Ugift-{suffix}",
        phone=f"09{tenant.id:08d}"[-10:], display_name=f"顧客 {suffix}",
    )
    product = Product(
        tenant_id=tenant.id, name=f"商品 {suffix}", price_cents=30000, stock=5
    )
    db.add_all([customer, product])
    db.commit()
    return tenant, customer, product


def _issue(db, tenant, *, customer_id=None, amount=20000, key="issue-key-1234567890"):
    result = gift_cards_svc.issue_card(
        db, tenant_id=tenant.id, amount_cents=amount,
        fulfillment_guarantee="由測試銀行提供自出售日起至少一年履約保障，逾期仍履約。",
        issuance_key=key, issued_by_user_id=1,
        recipient_customer_id=customer_id,
    )
    db.commit()
    return result


def test_issue_stores_hash_only_and_is_idempotent(db):
    tenant, customer, _ = _seed(db)
    issued = _issue(db, tenant, customer_id=customer.id)
    assert issued.created and issued.code
    normalized = issued.code.replace("-", "")
    assert normalized not in issued.card.code_hash
    assert issued.card.code_last4 == normalized[-4:]
    assert not hasattr(issued.card, "expires_at")  # 台灣有償禮券不設定期限
    duplicate = _issue(db, tenant, customer_id=customer.id)
    assert not duplicate.created and duplicate.card.id == issued.card.id
    assert duplicate.code is None
    assert db.query(GiftCard).count() == 1
    assert gift_cards_svc.balance_cents(
        db, tenant_id=tenant.id, gift_card_id=issued.card.id
    ) == 20000


def test_claim_and_cross_tenant_or_other_customer_are_rejected(db):
    tenant, customer, _ = _seed(db, "a")
    issued = _issue(db, tenant)
    gift_cards_svc.claim_card(
        db, tenant_id=tenant.id, code=issued.code, customer_id=customer.id
    )
    db.commit()
    other = Customer(tenant_id=tenant.id, line_user_id="Uother")
    db.add(other)
    db.commit()
    with pytest.raises(gift_cards_svc.GiftCardUnavailable):
        gift_cards_svc.claim_card(
            db, tenant_id=tenant.id, code=issued.code, customer_id=other.id
        )
    tenant2, customer2, _ = _seed(db, "b")
    with pytest.raises(gift_cards_svc.GiftCardNotFound):
        gift_cards_svc.claim_card(
            db, tenant_id=tenant2.id, code=issued.code, customer_id=customer2.id
        )


def test_pos_partial_redemption_and_cancel_refund(db):
    tenant, customer, product = _seed(db)
    issued = _issue(db, tenant, customer_id=customer.id, amount=20000)
    order = pos_svc.checkout(
        db, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": product.id, "qty": 1}],
        gift_card_code=issued.code,
    )
    assert order.gift_card_cents == 20000
    assert order.total_cents == 10000  # 不足額保留為其他付款
    assert gift_cards_svc.balance_cents(
        db, tenant_id=tenant.id, gift_card_id=issued.card.id
    ) == 0
    shop_svc.cancel_order(db, tenant_id=tenant.id, order_id=order.id)
    # 重複取消是 no-op，不會重複退款。
    shop_svc.cancel_order(db, tenant_id=tenant.id, order_id=order.id)
    assert gift_cards_svc.balance_cents(
        db, tenant_id=tenant.id, gift_card_id=issued.card.id
    ) == 20000
    assert db.query(GiftCardLedger).filter_by(
        tenant_id=tenant.id, order_id=order.id, kind="refund"
    ).count() == 1


def test_full_redemption_keeps_unspent_balance_and_lookup_wallet(db):
    tenant, customer, product = _seed(db)
    product.price_cents = 12000
    db.commit()
    issued = _issue(db, tenant, customer_id=customer.id, amount=20000)
    order = pos_svc.checkout(
        db, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": product.id, "qty": 1}], gift_card_code=issued.code,
    )
    assert order.total_cents == 0 and order.gift_card_cents == 12000
    lookup = pos_svc.lookup_by_phone(db, tenant_id=tenant.id, phone=customer.phone)
    assert lookup["gift_card_balance_cents"] == 8000


def test_invalid_card_rolls_back_order_and_stock(db):
    tenant, customer, product = _seed(db)
    with pytest.raises(gift_cards_svc.GiftCardNotFound):
        pos_svc.checkout(
            db, tenant_id=tenant.id, customer_id=customer.id,
            items=[{"product_id": product.id, "qty": 1}],
            gift_card_code="AAAA-BBBB-CCCC-DDDD",
        )
    db.rollback()
    db.refresh(product)
    assert product.stock == 5


def test_void_zeroes_remaining_balance(db):
    tenant, customer, _ = _seed(db)
    issued = _issue(db, tenant, customer_id=customer.id)
    gift_cards_svc.void_card(
        db, tenant_id=tenant.id, gift_card_id=issued.card.id,
        note="已依原付款方式退款", actor_user_id=7,
    )
    db.commit()
    assert issued.card.status == "void"
    assert gift_cards_svc.balance_cents(
        db, tenant_id=tenant.id, gift_card_id=issued.card.id
    ) == 0


def test_line_claim_and_balance(db):
    tenant, customer, _ = _seed(db)
    issued = _issue(db, tenant, amount=50000)
    action, params = parse_booking_command(f"領取禮物卡 {issued.code}")
    assert action == "claim_gift_card" and params["code"] == issued.code
    reply = _gift_cards_reply(
        db, tenant.id, customer.line_user_id, claim_code=issued.code
    )
    assert "NT$ 500" in reply and "永久有效" in reply
    assert "末四碼" in _gift_cards_reply(db, tenant.id, customer.line_user_id)
