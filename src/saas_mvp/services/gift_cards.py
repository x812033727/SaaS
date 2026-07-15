"""電子禮物卡：安全卡號、餘額帳本、領取、POS 折抵與退款。"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from saas_mvp.models.customer import Customer
from saas_mvp.models.gift_card import GiftCard, GiftCardLedger


class GiftCardError(ValueError):
    pass


class GiftCardNotFound(GiftCardError):
    pass


class GiftCardUnavailable(GiftCardError):
    pass


@dataclass(frozen=True)
class IssuedGiftCard:
    card: GiftCard
    code: str | None
    created: bool


@dataclass(frozen=True)
class GiftCardBalance:
    card: GiftCard
    balance_cents: int


def _normalize_code(code: str) -> str:
    value = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
    if len(value) != 16:
        raise GiftCardNotFound("禮物卡卡號格式不正確。")
    return value


def _hash_code(code: str) -> str:
    return hashlib.sha256(_normalize_code(code).encode()).hexdigest()


def _new_code() -> str:
    # 排除 I/O/0/1，降低人工輸入誤判；80 bits 以上熵，資料庫只保存雜湊。
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def _card_by_code(db: Session, *, tenant_id: int, code: str, lock: bool = False) -> GiftCard:
    stmt = select(GiftCard).where(
        GiftCard.tenant_id == tenant_id,
        GiftCard.code_hash == _hash_code(code),
    )
    if lock:
        stmt = stmt.with_for_update()
    card = db.execute(stmt).scalar_one_or_none()
    if card is None:
        raise GiftCardNotFound("找不到禮物卡，請確認卡號。")
    return card


def balance_cents(db: Session, *, tenant_id: int, gift_card_id: int) -> int:
    return int(db.execute(
        select(func.coalesce(func.sum(GiftCardLedger.delta_cents), 0)).where(
            GiftCardLedger.tenant_id == tenant_id,
            GiftCardLedger.gift_card_id == gift_card_id,
        )
    ).scalar_one())


def issue_card(
    db: Session,
    *,
    tenant_id: int,
    amount_cents: int,
    fulfillment_guarantee: str,
    issuance_key: str,
    issued_by_user_id: int | None,
    recipient_customer_id: int | None = None,
    purchaser_name: str | None = None,
    recipient_name: str | None = None,
    message: str | None = None,
) -> IssuedGiftCard:
    if amount_cents < 10000 or amount_cents > 100000000:
        raise GiftCardError("禮物卡面額須介於 NT$100～NT$1,000,000。")
    guarantee = (fulfillment_guarantee or "").strip()
    if len(guarantee) < 10 or len(guarantee) > 2000:
        raise GiftCardError("請填寫 10～2,000 字的履約保障資訊。")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", issuance_key or ""):
        raise GiftCardError("發行識別碼格式不正確，請重新整理後再試。")

    existing = db.execute(select(GiftCard).where(
        GiftCard.tenant_id == tenant_id, GiftCard.issuance_key == issuance_key
    )).scalar_one_or_none()
    if existing is not None:
        return IssuedGiftCard(existing, None, False)

    if recipient_customer_id is not None:
        customer = db.execute(select(Customer).where(
            Customer.id == recipient_customer_id, Customer.tenant_id == tenant_id
        )).scalar_one_or_none()
        if customer is None:
            raise GiftCardError("收禮顧客不存在。")

    code = _new_code()
    card = GiftCard(
        tenant_id=tenant_id,
        code_hash=_hash_code(code),
        code_last4=_normalize_code(code)[-4:],
        recipient_customer_id=recipient_customer_id,
        initial_value_cents=amount_cents,
        purchaser_name=(purchaser_name or "").strip()[:128] or None,
        recipient_name=(recipient_name or "").strip()[:128] or None,
        message=(message or "").strip()[:500] or None,
        fulfillment_guarantee=guarantee,
        issuance_key=issuance_key,
        issued_by_user_id=issued_by_user_id,
    )
    db.add(card)
    db.flush()
    db.add(GiftCardLedger(
        tenant_id=tenant_id, gift_card_id=card.id,
        customer_id=recipient_customer_id, delta_cents=amount_cents,
        kind="issue", note="禮物卡發行", actor_user_id=issued_by_user_id,
    ))
    return IssuedGiftCard(card, code, True)


def claim_card(db: Session, *, tenant_id: int, code: str, customer_id: int) -> GiftCard:
    customer = db.execute(select(Customer).where(
        Customer.id == customer_id, Customer.tenant_id == tenant_id
    )).scalar_one_or_none()
    if customer is None:
        raise GiftCardError("顧客不存在。")
    card = _card_by_code(db, tenant_id=tenant_id, code=code, lock=True)
    if card.status != "active":
        raise GiftCardUnavailable("此禮物卡已停用。")
    if card.recipient_customer_id not in (None, customer_id):
        raise GiftCardUnavailable("此禮物卡已由其他顧客領取。")
    card.recipient_customer_id = customer_id
    return card


def customer_wallet(db: Session, *, tenant_id: int, customer_id: int) -> list[GiftCardBalance]:
    cards = db.execute(select(GiftCard).where(
        GiftCard.tenant_id == tenant_id,
        GiftCard.recipient_customer_id == customer_id,
        GiftCard.status == "active",
    ).order_by(GiftCard.created_at)).scalars().all()
    out = []
    for card in cards:
        balance = balance_cents(db, tenant_id=tenant_id, gift_card_id=card.id)
        if balance > 0:
            out.append(GiftCardBalance(card, balance))
    return out


def redeem_for_order(
    db: Session, *, tenant_id: int, code: str, order_id: int,
    amount_due_cents: int, customer_id: int | None,
) -> int:
    if amount_due_cents <= 0:
        return 0
    existing = db.execute(select(GiftCardLedger).where(
        GiftCardLedger.tenant_id == tenant_id,
        GiftCardLedger.order_id == order_id,
        GiftCardLedger.kind == "redeem",
    )).scalar_one_or_none()
    if existing is not None:
        return -existing.delta_cents
    card = _card_by_code(db, tenant_id=tenant_id, code=code, lock=True)
    if card.status != "active":
        raise GiftCardUnavailable("此禮物卡已停用。")
    if card.recipient_customer_id is not None and card.recipient_customer_id != customer_id:
        raise GiftCardUnavailable("此禮物卡已綁定其他顧客。")
    available = balance_cents(db, tenant_id=tenant_id, gift_card_id=card.id)
    if available <= 0:
        raise GiftCardUnavailable("此禮物卡已無餘額。")
    used = min(available, amount_due_cents)
    if card.recipient_customer_id is None and customer_id is not None:
        card.recipient_customer_id = customer_id
    db.add(GiftCardLedger(
        tenant_id=tenant_id, gift_card_id=card.id, customer_id=customer_id,
        order_id=order_id, delta_cents=-used, kind="redeem", note="POS 結帳折抵",
    ))
    return used


def refund_order(db: Session, *, tenant_id: int, order_id: int, actor_user_id: int | None = None) -> int:
    redeemed = db.execute(select(GiftCardLedger).where(
        GiftCardLedger.tenant_id == tenant_id,
        GiftCardLedger.order_id == order_id,
        GiftCardLedger.kind == "redeem",
    )).scalar_one_or_none()
    if redeemed is None:
        return 0
    existing = db.execute(select(GiftCardLedger).where(
        GiftCardLedger.tenant_id == tenant_id,
        GiftCardLedger.order_id == order_id,
        GiftCardLedger.kind == "refund",
    )).scalar_one_or_none()
    if existing is not None:
        return existing.delta_cents
    card = db.execute(select(GiftCard).where(
        GiftCard.id == redeemed.gift_card_id, GiftCard.tenant_id == tenant_id
    ).with_for_update()).scalar_one()
    amount = -redeemed.delta_cents
    db.add(GiftCardLedger(
        tenant_id=tenant_id, gift_card_id=card.id, customer_id=redeemed.customer_id,
        order_id=order_id, delta_cents=amount, kind="refund",
        note="訂單取消退回禮物卡", actor_user_id=actor_user_id,
    ))
    return amount


def void_card(db: Session, *, tenant_id: int, gift_card_id: int, note: str, actor_user_id: int) -> GiftCard:
    card = db.execute(select(GiftCard).where(
        GiftCard.id == gift_card_id, GiftCard.tenant_id == tenant_id
    ).with_for_update()).scalar_one_or_none()
    if card is None:
        raise GiftCardNotFound("找不到禮物卡。")
    if card.status == "void":
        return card
    remaining = balance_cents(db, tenant_id=tenant_id, gift_card_id=card.id)
    if remaining:
        db.add(GiftCardLedger(
            tenant_id=tenant_id, gift_card_id=card.id,
            customer_id=card.recipient_customer_id, delta_cents=-remaining,
            kind="adjust", note=(note or "作廢並完成退款")[:255], actor_user_id=actor_user_id,
        ))
    import datetime
    card.status = "void"
    card.voided_at = datetime.datetime.now(datetime.timezone.utc)
    return card


def recent_cards(db: Session, *, tenant_id: int, limit: int = 100) -> list[GiftCardBalance]:
    cards = db.execute(select(GiftCard).where(GiftCard.tenant_id == tenant_id)
                       .order_by(GiftCard.id.desc()).limit(limit)).scalars().all()
    return [GiftCardBalance(c, balance_cents(db, tenant_id=tenant_id, gift_card_id=c.id)) for c in cards]
