"""禮物卡線上販售(R11-A)。

金流面鐵律:
* 發卡在 mark_order_paid 的 PENDING→PAID **同一交易**內(on_order_paid),
  crash 於 commit 前=order 仍 PENDING,gateway 重送 callback 自然重跑;
  crash 於 commit 後=卡已存在且 purchase.status=issued 擋重發。
* issuance_key 由 merchant_trade_no 決定性導出:即使 purchase 狀態列
  意外遺失,replay 也只會命中 issue_card 的 (tenant, key) 冪等。
* 明碼只在首次發卡回傳一次 → 同交易加密存入 purchase.code_enc,
  成功頁與交付信皆由此重覆取用。
* 交付信走 email outbox(deliver_or_queue **自帶 commit**,絕不可在
  金流交易中途呼叫)— 只在付款交易 commit 之後 best-effort 執行。
"""

from __future__ import annotations

import datetime
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.gift_card_sale import (
    PURCHASE_ISSUED,
    PURCHASE_PENDING,
    GiftCardPurchase,
    TenantGiftCardConfig,
)


class GiftCardSaleError(ValueError):
    pass


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_DENOMS = 10
_MIN_TWD = 100
_MAX_TWD = 1_000_000


def get_config(db: Session, tenant_id: int) -> TenantGiftCardConfig | None:
    return db.execute(
        select(TenantGiftCardConfig).where(
            TenantGiftCardConfig.tenant_id == tenant_id
        )
    ).scalar_one_or_none()


def denominations_of(config: TenantGiftCardConfig | None) -> list[int]:
    """面額清單(元);防禦性解析,壞資料回空。"""
    if config is None or not config.denominations:
        return []
    try:
        values = json.loads(config.denominations)
        return [int(v) for v in values if isinstance(v, (int, float))]
    except (ValueError, TypeError):
        return []


def save_config(
    db: Session,
    *,
    tenant_id: int,
    online_sale_enabled: bool,
    denominations: list[int],
    fulfillment_guarantee: str,
    updated_by_user_id: int | None,
) -> TenantGiftCardConfig:
    """upsert 販售設定;flush only,caller 負責 commit(比照 invoice_profiles)。

    啟用販售時嚴格驗證:issue_card 在付款 callback 才會用到 guarantee,
    設定時不擋、收錢後才炸是不可接受的。
    """
    guarantee = (fulfillment_guarantee or "").strip()
    seen: list[int] = []
    for value in denominations:
        if not isinstance(value, int) or value < _MIN_TWD or value > _MAX_TWD:
            raise GiftCardSaleError(
                f"面額需為 NT${_MIN_TWD:,}～NT${_MAX_TWD:,} 的整數。"
            )
        if value not in seen:
            seen.append(value)
    if len(seen) > _MAX_DENOMS:
        raise GiftCardSaleError(f"面額最多 {_MAX_DENOMS} 個。")
    if online_sale_enabled:
        if not seen:
            raise GiftCardSaleError("啟用線上販售需至少一個面額。")
        if len(guarantee) < 10 or len(guarantee) > 2000:
            raise GiftCardSaleError("啟用線上販售需 10～2,000 字的履約保障資訊。")
    if len(guarantee) > 2000:
        raise GiftCardSaleError("履約保障資訊最多 2,000 字。")

    row = get_config(db, tenant_id)
    if row is None:
        row = TenantGiftCardConfig(tenant_id=tenant_id)
        db.add(row)
    row.online_sale_enabled = online_sale_enabled
    row.denominations = json.dumps(sorted(seen))
    row.fulfillment_guarantee = guarantee or None
    row.updated_by_user_id = updated_by_user_id
    db.flush()
    return row


_REAL_PROVIDERS = frozenset({"ecpay", "newebpay", "linepay"})


def _payment_ready(db: Session) -> bool:
    from saas_mvp.services.platform_payment_config import payment_provider

    return payment_provider(db, settings) in _REAL_PROVIDERS


def sale_available(db: Session, tenant_id: int) -> TenantGiftCardConfig | None:
    """可販售 → 回 config;否則 None。

    閘門=feature GIFT_CARDS + 設定啟用+完整 + **平台有真實金流**
    (stub=買家會被導向不存在的假結帳頁,寧可整頁 404)。
    """
    from saas_mvp.services import features as features_svc

    if not _payment_ready(db):
        return None
    if not features_svc.is_enabled(db, tenant_id, features_svc.GIFT_CARDS):
        return None
    config = get_config(db, tenant_id)
    if config is None or not config.online_sale_enabled:
        return None
    guarantee = (config.fulfillment_guarantee or "").strip()
    if len(guarantee) < 10 or not denominations_of(config):
        return None
    return config


def start_purchase(
    db: Session,
    *,
    tenant_id: int,
    amount_twd: int,
    purchaser_email: str,
    purchaser_name: str = "",
    recipient_name: str = "",
    message: str = "",
) -> GiftCardPurchase:
    """建立購買(Order+GiftCardPurchase,commit);回傳 purchase(order 已掛)。"""
    from saas_mvp.models.order import Order
    from saas_mvp.services import shop as shop_svc

    config = sale_available(db, tenant_id)
    if config is None:
        raise GiftCardSaleError("此店家目前未開放線上購買禮物卡。")
    if amount_twd not in denominations_of(config):
        raise GiftCardSaleError("面額不在販售清單內。")
    email = (purchaser_email or "").strip()
    if not _EMAIL_RE.fullmatch(email) or len(email) > 256:
        raise GiftCardSaleError("請填寫正確的電子郵件(用於寄送卡號)。")
    # 匿名端點防灌單:每租戶每小時新購買數上限(公開 IP 限流之外的第二層)
    hour_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=1
    )
    recent = (
        db.query(GiftCardPurchase)
        .filter(
            GiftCardPurchase.tenant_id == tenant_id,
            GiftCardPurchase.created_at >= hour_ago,
        )
        .count()
    )
    if recent >= 30:
        raise GiftCardSaleError("目前購買人數眾多,請稍後再試。")

    order = Order(tenant_id=tenant_id, total_cents=amount_twd * 100)
    db.add(order)
    db.flush()
    order.merchant_trade_no = shop_svc.gen_order_trade_no(order.id)
    purchase = GiftCardPurchase(
        tenant_id=tenant_id,
        order_id=order.id,
        amount_cents=amount_twd * 100,
        purchaser_name=(purchaser_name or "").strip()[:128] or None,
        purchaser_email=email,
        recipient_name=(recipient_name or "").strip()[:128] or None,
        message=(message or "").strip()[:500] or None,
    )
    db.add(purchase)
    db.commit()
    db.refresh(purchase)
    return purchase


def purchase_for_order(db: Session, order_id: int) -> GiftCardPurchase | None:
    return db.execute(
        select(GiftCardPurchase).where(GiftCardPurchase.order_id == order_id)
    ).scalar_one_or_none()


def _issuance_key(merchant_trade_no: str) -> str:
    # 決定性:同一張單 replay 必命中 issue_card 冪等;前綴補足 16 字下限
    return f"gcpurchase-{merchant_trade_no}"


def on_order_paid(db: Session, order) -> None:
    """mark_order_paid 的 PENDING→PAID 交易內呼叫(commit 前)。

    非購卡訂單=no-op。order 列已被 FOR UPDATE 鎖住,序列化重送。
    """
    from saas_mvp.services import gift_cards as gift_cards_svc

    purchase = purchase_for_order(db, order.id)
    if purchase is None or purchase.status == PURCHASE_ISSUED:
        return
    config = get_config(db, order.tenant_id)
    guarantee = (config.fulfillment_guarantee or "").strip() if config else ""
    if len(guarantee) < 10:
        # 收錢後設定被清掉的極端情況:仍要發卡,用保底文案(法遵風險小於吞錢)
        guarantee = "本店禮物卡依禮券定型化契約規定辦理履約保障,詳情請洽店家。"
    result = gift_cards_svc.issue_card(
        db,
        tenant_id=order.tenant_id,
        amount_cents=purchase.amount_cents,
        fulfillment_guarantee=guarantee,
        issuance_key=_issuance_key(order.merchant_trade_no),
        issued_by_user_id=None,
        purchaser_name=purchase.purchaser_name,
        recipient_name=purchase.recipient_name,
        message=purchase.message,
    )
    purchase.gift_card_id = result.card.id
    purchase.status = PURCHASE_ISSUED
    purchase.issued_at = datetime.datetime.now(datetime.timezone.utc)
    if result.created and result.code:
        purchase.set_plain_code(result.code)
    db.flush()


def status_url_for_order(db: Session, order) -> str | None:
    """購卡訂單的成功/狀態頁絕對網址(capability URL=trade_no);非購卡回 None。"""
    purchase = purchase_for_order(db, order.id)
    if purchase is None:
        return None
    from saas_mvp.services import profile as profile_svc

    prof = profile_svc.get_by_tenant(db, order.tenant_id)
    if prof is None or not prof.slug:
        return None
    base = settings.public_base_url.rstrip("/")
    return f"{base}/p/{prof.slug}/gift-cards/{order.merchant_trade_no}"


def queue_delivery_email(db: Session, order) -> None:
    """付款交易 commit 後 best-effort:交付信入 outbox。

    deliver_or_queue 自帶 commit,絕不可在交易中途呼叫;任何失敗吞掉
    (成功頁仍可顯示卡號,outbox cron 亦會重試)。
    """
    try:
        purchase = purchase_for_order(db, order.id)
        if (
            purchase is None
            or purchase.status != PURCHASE_ISSUED
            or purchase.email_queued_at is not None
        ):
            return
        code = purchase.plain_code
        if not code:
            return
        from saas_mvp.models.tenant import Tenant
        from saas_mvp.services.email_delivery import deliver_or_queue
        from saas_mvp.services.mailer import get_mailer

        tenant = db.get(Tenant, order.tenant_id)
        store = tenant.name if tenant else "店家"
        status_url = status_url_for_order(db, order)
        lines = [
            f"感謝您購買「{store}」電子禮物卡!",
            "",
            f"面額:NT${purchase.amount_cents // 100:,}",
            f"卡號:{code}",
        ]
        if purchase.recipient_name:
            lines.append(f"收禮人:{purchase.recipient_name}")
        if purchase.message:
            lines.append(f"祝福訊息:{purchase.message}")
        if status_url:
            lines += ["", f"隨時可於此頁查看卡號:{status_url}"]
        lines += [
            "",
            "使用方式:於店內結帳時出示卡號即可折抵消費。",
            "請妥善保管卡號,持有卡號者即可使用。",
        ]
        # 明碼只進 body(EmailDelivery body 加密存放,subject 為明文)
        deliver_or_queue(
            db,
            get_mailer(db),
            user_id=None,
            category="gift_card_purchase",
            recipient=purchase.purchaser_email,
            subject=f"您的「{store}」電子禮物卡",
            body="\n".join(lines),
        )
        purchase.email_queued_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()
    except Exception:  # noqa: BLE001 — 交付信失敗不得影響金流回調
        db.rollback()


def purchase_by_trade_no(
    db: Session, *, tenant_id: int, trade_no: str
) -> GiftCardPurchase | None:
    """成功頁查詢:trade_no → order → purchase,租戶必須相符(防跨租戶)。"""
    from saas_mvp.services import shop as shop_svc

    order = shop_svc.get_order_by_trade_no(db, trade_no)
    if order is None or order.tenant_id != tenant_id:
        return None
    purchase = purchase_for_order(db, order.id)
    if purchase is None:
        return None
    purchase.order = order  # 附掛供頁面顯示(非 relationship)
    return purchase


__all__ = [
    "GiftCardSaleError",
    "PURCHASE_ISSUED",
    "PURCHASE_PENDING",
    "denominations_of",
    "get_config",
    "on_order_paid",
    "purchase_by_trade_no",
    "purchase_for_order",
    "queue_delivery_email",
    "sale_available",
    "save_config",
    "start_purchase",
    "status_url_for_order",
]
