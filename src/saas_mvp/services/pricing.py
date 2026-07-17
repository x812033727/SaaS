"""訂單折扣彙整 — 會員等級折扣 + 優惠券。

三條結帳路徑共用唯一實作（POS `pos.checkout` / 訂單 REST + LINE 站內購買
`shop.create_order`），確保折扣順序與語意一致。皆於呼叫端的同一交易內執行、**不 commit**。

順序：
  1. 會員等級折扣：對「毛額小計」套用（散客 / 未建檔顧客 customer=None → 不折）。
  2. 優惠券：以「等級折扣後」金額為折抵基準；``min_spend`` 仍以毛額判定；
     核銷走 :func:`coupons.redeem_coupon_core`（鎖券 + 重驗有效性/額度/一人一券 + 記 order_id）。

副作用：設定 ``order.discount_cents``（等級+券）與 ``order.coupon_code``。
回傳：扣「等級+券」後的金額（**尚未扣點數**；POS 之後另行折抵點數）。
例外：券無效 / 過期 / 已用 / 未達最低消費 → 拋 :class:`coupons.CouponError` 子類（呼叫端轉 HTTP）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from saas_mvp.models.order import Order


def apply_order_discounts(
    db: Session,
    *,
    tenant_id: int,
    order: Order,
    customer,
    subtotal_cents: int,
    line_user_id: str | None,
    coupon_code: str | None,
) -> int:
    """套用會員等級折扣 + 優惠券到 ``order``（不 commit）；回傳折後（未扣點）金額。

    ``customer``：已解析的會員（None＝散客，不折等級、不可用券）。
    ``line_user_id``：券核銷身分；customer 有 line_user_id 時優先用之。
    """
    from saas_mvp.services import coupons as coupons_svc
    from saas_mvp.services import loyalty_config
    from saas_mvp.services import membership as membership_svc

    running = subtotal_cents

    tier_discount = 0
    if customer is not None:
        # R6-B3:per-tenant 折扣設定(無設定 → 全域預設)。
        discounts = loyalty_config.discounts_for(
            loyalty_config.get_config(db, tenant_id)
        )
        tier_discount = membership_svc.tier_discount_for(
            customer.tier, subtotal_cents, discounts=discounts
        )
        running -= tier_discount

    coupon_discount = 0
    if coupon_code:
        redeem_line_user = (
            (customer.line_user_id if customer is not None else None) or line_user_id
        )
        if not redeem_line_user:
            raise coupons_svc.CouponError("coupon requires a LINE user id")
        coupon, _ = coupons_svc.redeem_coupon_core(
            db,
            tenant_id=tenant_id,
            code=coupon_code,
            line_user_id=redeem_line_user,
            customer_id=customer.id if customer is not None else None,
            order_id=order.id,
            subtotal_cents=subtotal_cents,  # min_spend 以毛額判定
        )
        coupon_discount = coupons_svc.compute_discount(coupon, max(0, running))
        running -= coupon_discount
        order.coupon_code = coupon_code

    order.discount_cents = tier_discount + coupon_discount
    return max(0, running)
