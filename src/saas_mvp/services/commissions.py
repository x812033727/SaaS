"""員工抽成快照與薪資結算服務。"""

from __future__ import annotations

import datetime
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.models.commission import (
    BASIS_GROSS,
    ITEM_TIP,
    METHOD_FIXED,
    METHOD_PERCENT,
    PAY_RUN_DRAFT,
    PAY_RUN_FINALIZED,
    PAY_RUN_PAID,
    VALID_BASES,
    VALID_METHODS,
    VALID_RULE_ITEM_TYPES,
    CommissionEarning,
    CommissionRule,
    PayRun,
    PayRunItem,
)
from saas_mvp.models.order import ORDER_PAID, Order
from saas_mvp.models.order_item import OrderItem
from saas_mvp.models.staff import Staff
from saas_mvp.services.tenants import tenant_query


class CommissionError(ValueError):
    pass


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _as_aware(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.timezone.utc)


def _staff(db: Session, tenant_id: int, staff_id: int) -> Staff:
    row = tenant_query(db, Staff, tenant_id).filter(Staff.id == staff_id).first()
    if row is None:
        raise CommissionError("找不到該員工。")
    return row


def save_rule(
    db: Session,
    *,
    tenant_id: int,
    staff_id: int,
    item_type: str,
    method: str,
    value: int,
    calculation_basis: str,
    effective_from: datetime.date,
    actor_user_id: int,
) -> CommissionRule:
    """新增具生效日的規則版本；歷史規則不覆寫，確保可追溯。"""
    _staff(db, tenant_id, staff_id)
    if item_type not in VALID_RULE_ITEM_TYPES:
        raise CommissionError("抽成類型不正確。")
    if method not in VALID_METHODS:
        raise CommissionError("抽成方式不正確。")
    if calculation_basis not in VALID_BASES:
        raise CommissionError("抽成計算基礎不正確。")
    if value < 0:
        raise CommissionError("抽成不可為負數。")
    if method == METHOD_PERCENT and value > 10_000:
        raise CommissionError("百分比抽成不可超過 100%。")
    if method == METHOD_FIXED and value > 100_000_000:
        raise CommissionError("固定抽成金額過大。")

    row = CommissionRule(
        tenant_id=tenant_id,
        staff_id=staff_id,
        item_type=item_type,
        method=method,
        value=value,
        calculation_basis=calculation_basis,
        effective_from=effective_from,
        is_active=True,
        created_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()
    return row


def latest_rules(db: Session, *, tenant_id: int) -> dict[tuple[int, str], CommissionRule]:
    rows = (
        tenant_query(db, CommissionRule, tenant_id)
        .filter(CommissionRule.is_active.is_(True))
        .order_by(
            CommissionRule.staff_id,
            CommissionRule.item_type,
            CommissionRule.effective_from.desc(),
            CommissionRule.id.desc(),
        )
        .all()
    )
    out: dict[tuple[int, str], CommissionRule] = {}
    for row in rows:
        out.setdefault((row.staff_id, row.item_type), row)
    return out


def _effective_rule(
    db: Session,
    *,
    tenant_id: int,
    staff_id: int,
    item_type: str,
    earned_date: datetime.date,
) -> CommissionRule | None:
    return (
        tenant_query(db, CommissionRule, tenant_id)
        .filter(
            CommissionRule.staff_id == staff_id,
            CommissionRule.item_type == item_type,
            CommissionRule.is_active.is_(True),
            CommissionRule.effective_from <= earned_date,
        )
        .order_by(CommissionRule.effective_from.desc(), CommissionRule.id.desc())
        .first()
    )


def _allocate_net(items: list[OrderItem], order: Order) -> dict[int, int]:
    subtotal = sum(max(0, item.line_total_cents) for item in items)
    if subtotal <= 0:
        return {item.id: 0 for item in items}
    deduction = min(
        subtotal,
        max(0, order.discount_cents or 0) + max(0, order.points_cents or 0),
    )
    remaining = deduction
    allocated: dict[int, int] = {}
    for index, item in enumerate(items):
        if index == len(items) - 1:
            share = remaining
        else:
            share = deduction * max(0, item.line_total_cents) // subtotal
            remaining -= share
        allocated[item.id] = max(0, item.line_total_cents - share)
    return allocated


def record_paid_order(
    db: Session,
    *,
    order: Order,
    items: list[OrderItem] | None = None,
) -> list[CommissionEarning]:
    """依成交當下規則建立冪等快照；只接受已付訂單。"""
    if order.status != ORDER_PAID or order.paid_at is None:
        return []
    line_items = items or (
        tenant_query(db, OrderItem, order.tenant_id)
        .filter(OrderItem.order_id == order.id)
        .order_by(OrderItem.id)
        .all()
    )
    db.flush()
    nets = _allocate_net(line_items, order)
    earned_at = _as_aware(order.paid_at)
    created: list[CommissionEarning] = []

    for item in line_items:
        if item.staff_id is None or item.item_type not in VALID_RULE_ITEM_TYPES:
            continue
        source_key = f"order_item:{item.id}"
        existing = (
            tenant_query(db, CommissionEarning, order.tenant_id)
            .filter(CommissionEarning.source_key == source_key)
            .first()
        )
        if existing is not None:
            continue
        rule = _effective_rule(
            db,
            tenant_id=order.tenant_id,
            staff_id=item.staff_id,
            item_type=item.item_type,
            earned_date=earned_at.date(),
        )
        if rule is None or rule.value == 0:
            continue
        gross = item.line_total_cents
        net = nets.get(item.id, gross)
        basis_cents = gross if rule.calculation_basis == BASIS_GROSS else net
        amount = (
            (basis_cents * rule.value + 5_000) // 10_000
            if rule.method == METHOD_PERCENT
            else rule.value * item.qty
        )
        earning = CommissionEarning(
            tenant_id=order.tenant_id,
            staff_id=item.staff_id,
            order_id=order.id,
            order_item_id=item.id,
            source_key=source_key,
            item_type=item.item_type,
            item_name_snapshot=item.name_snapshot,
            gross_cents=gross,
            net_cents=net,
            calculation_basis=rule.calculation_basis,
            method_snapshot=rule.method,
            value_snapshot=rule.value,
            commission_cents=amount,
            earned_at=earned_at,
        )
        db.add(earning)
        created.append(earning)

    if order.staff_id is not None and (order.tip_cents or 0) > 0:
        source_key = f"order_tip:{order.id}"
        existing = (
            tenant_query(db, CommissionEarning, order.tenant_id)
            .filter(CommissionEarning.source_key == source_key)
            .first()
        )
        if existing is None:
            tip = int(order.tip_cents)
            earning = CommissionEarning(
                tenant_id=order.tenant_id,
                staff_id=order.staff_id,
                order_id=order.id,
                order_item_id=None,
                source_key=source_key,
                item_type=ITEM_TIP,
                item_name_snapshot="小費",
                gross_cents=tip,
                net_cents=tip,
                calculation_basis=BASIS_GROSS,
                method_snapshot=METHOD_FIXED,
                value_snapshot=tip,
                commission_cents=tip,
                earned_at=earned_at,
            )
            db.add(earning)
            created.append(earning)
    db.flush()
    return created


def _recalculate_pay_run(db: Session, pay_run: PayRun) -> None:
    earnings = (
        tenant_query(db, CommissionEarning, pay_run.tenant_id)
        .filter(CommissionEarning.pay_run_id == pay_run.id)
        .all()
    )
    grouped: dict[int, dict[str, int]] = defaultdict(lambda: {"commission": 0, "tip": 0})
    for earning in earnings:
        key = "tip" if earning.item_type == ITEM_TIP else "commission"
        grouped[earning.staff_id][key] += earning.commission_cents
    existing = {
        row.staff_id: row
        for row in tenant_query(db, PayRunItem, pay_run.tenant_id)
        .filter(PayRunItem.pay_run_id == pay_run.id)
        .all()
    }
    for staff_id, totals in grouped.items():
        row = existing.pop(staff_id, None)
        if row is None:
            row = PayRunItem(
                tenant_id=pay_run.tenant_id, pay_run_id=pay_run.id, staff_id=staff_id
            )
            db.add(row)
        row.commission_cents = totals["commission"]
        row.tip_cents = totals["tip"]
        row.total_cents = (
            row.commission_cents + row.tip_cents + (row.adjustment_cents or 0)
        )
    for row in existing.values():
        if row.adjustment_cents:
            row.commission_cents = 0
            row.tip_cents = 0
            row.total_cents = row.adjustment_cents
        else:
            db.delete(row)
    db.flush()
    pay_run.total_cents = sum(
        row.total_cents
        for row in tenant_query(db, PayRunItem, pay_run.tenant_id)
        .filter(PayRunItem.pay_run_id == pay_run.id)
        .all()
    )


def reverse_order(db: Session, *, order: Order) -> list[CommissionEarning]:
    """取消已付訂單：未結算快照作廢；已納入結算者留下負數沖銷。"""
    now = _utcnow()
    originals = (
        tenant_query(db, CommissionEarning, order.tenant_id)
        .filter(
            CommissionEarning.order_id == order.id,
            CommissionEarning.reversal_of_id.is_(None),
            CommissionEarning.reversed_at.is_(None),
        )
        .all()
    )
    created: list[CommissionEarning] = []
    dirty_drafts: set[int] = set()
    for original in originals:
        original.reversed_at = now
        if original.pay_run_id is None:
            continue
        pay_run = db.get(PayRun, original.pay_run_id)
        attach_to = pay_run.id if pay_run is not None and pay_run.status == PAY_RUN_DRAFT else None
        reversal = CommissionEarning(
            tenant_id=original.tenant_id,
            staff_id=original.staff_id,
            order_id=original.order_id,
            order_item_id=original.order_item_id,
            pay_run_id=attach_to,
            reversal_of_id=original.id,
            source_key=f"reversal:{original.id}",
            item_type=original.item_type,
            item_name_snapshot=f"沖銷：{original.item_name_snapshot}",
            gross_cents=-original.gross_cents,
            net_cents=-original.net_cents,
            calculation_basis=original.calculation_basis,
            method_snapshot=original.method_snapshot,
            value_snapshot=original.value_snapshot,
            commission_cents=-original.commission_cents,
            earned_at=now,
        )
        db.add(reversal)
        created.append(reversal)
        if attach_to is not None:
            dirty_drafts.add(attach_to)
    db.flush()
    for pay_run_id in dirty_drafts:
        pay_run = db.get(PayRun, pay_run_id)
        if pay_run is not None:
            _recalculate_pay_run(db, pay_run)
    return created


def create_pay_run(
    db: Session,
    *,
    tenant_id: int,
    period_start: datetime.date,
    period_end: datetime.date,
    actor_user_id: int,
) -> PayRun:
    if period_end < period_start:
        raise CommissionError("結算結束日不可早於開始日。")
    if (period_end - period_start).days > 366:
        raise CommissionError("單次結算期間不可超過一年。")
    start = datetime.datetime.combine(period_start, datetime.time.min, tzinfo=datetime.timezone.utc)
    end = datetime.datetime.combine(
        period_end + datetime.timedelta(days=1), datetime.time.min, tzinfo=datetime.timezone.utc
    )
    earnings = db.execute(
        select(CommissionEarning)
        .where(
            CommissionEarning.tenant_id == tenant_id,
            CommissionEarning.pay_run_id.is_(None),
            CommissionEarning.reversed_at.is_(None),
            CommissionEarning.earned_at >= start,
            CommissionEarning.earned_at < end,
        )
        .order_by(CommissionEarning.staff_id, CommissionEarning.earned_at, CommissionEarning.id)
        .with_for_update()
    ).scalars().all()
    if not earnings:
        raise CommissionError("此期間沒有尚未結算的抽成或小費。")
    run = PayRun(
        tenant_id=tenant_id,
        period_start=period_start,
        period_end=period_end,
        created_by_user_id=actor_user_id,
    )
    db.add(run)
    db.flush()
    for earning in earnings:
        earning.pay_run_id = run.id
    # SessionLocal 明確關閉 autoflush；先落實歸屬，彙總查詢才看得到本批明細。
    db.flush()
    _recalculate_pay_run(db, run)
    return run


def get_pay_run(db: Session, *, tenant_id: int, pay_run_id: int) -> PayRun:
    row = tenant_query(db, PayRun, tenant_id).filter(PayRun.id == pay_run_id).first()
    if row is None:
        raise CommissionError("找不到該結算單。")
    return row


def update_adjustment(
    db: Session,
    *,
    tenant_id: int,
    pay_run_id: int,
    staff_id: int,
    adjustment_cents: int,
    note: str | None,
) -> PayRunItem:
    run = get_pay_run(db, tenant_id=tenant_id, pay_run_id=pay_run_id)
    if run.status != PAY_RUN_DRAFT:
        raise CommissionError("只有草稿結算單可以調整。")
    if abs(adjustment_cents) > 100_000_000:
        raise CommissionError("調整金額過大。")
    row = (
        tenant_query(db, PayRunItem, tenant_id)
        .filter(PayRunItem.pay_run_id == run.id, PayRunItem.staff_id == staff_id)
        .first()
    )
    if row is None:
        raise CommissionError("該員工不在這張結算單內。")
    row.adjustment_cents = adjustment_cents
    row.adjustment_note = (note or "").strip() or None
    row.total_cents = row.commission_cents + row.tip_cents + row.adjustment_cents
    db.flush()
    run.total_cents = sum(
        item.total_cents
        for item in tenant_query(db, PayRunItem, tenant_id)
        .filter(PayRunItem.pay_run_id == run.id)
        .all()
    )
    return row


def finalize_pay_run(
    db: Session, *, tenant_id: int, pay_run_id: int, actor_user_id: int
) -> PayRun:
    run = get_pay_run(db, tenant_id=tenant_id, pay_run_id=pay_run_id)
    if run.status != PAY_RUN_DRAFT:
        raise CommissionError("只有草稿結算單可以確認。")
    run.status = PAY_RUN_FINALIZED
    run.finalized_at = _utcnow()
    run.finalized_by_user_id = actor_user_id
    db.flush()
    return run


def mark_pay_run_paid(
    db: Session, *, tenant_id: int, pay_run_id: int, actor_user_id: int
) -> PayRun:
    run = get_pay_run(db, tenant_id=tenant_id, pay_run_id=pay_run_id)
    if run.status != PAY_RUN_FINALIZED:
        raise CommissionError("結算單必須先確認，才能標記已付款。")
    run.status = PAY_RUN_PAID
    run.paid_at = _utcnow()
    run.paid_by_user_id = actor_user_id
    db.flush()
    return run


def delete_draft(db: Session, *, tenant_id: int, pay_run_id: int) -> None:
    run = get_pay_run(db, tenant_id=tenant_id, pay_run_id=pay_run_id)
    if run.status != PAY_RUN_DRAFT:
        raise CommissionError("只有草稿結算單可以刪除。")
    for earning in (
        tenant_query(db, CommissionEarning, tenant_id)
        .filter(CommissionEarning.pay_run_id == run.id)
        .all()
    ):
        earning.pay_run_id = None
    db.flush()
    db.delete(run)


def list_pay_runs(db: Session, *, tenant_id: int, limit: int = 24) -> list[PayRun]:
    return (
        tenant_query(db, PayRun, tenant_id)
        .order_by(PayRun.id.desc())
        .limit(limit)
        .all()
    )


def pay_run_items(db: Session, *, tenant_id: int, pay_run_id: int) -> list[PayRunItem]:
    return (
        tenant_query(db, PayRunItem, tenant_id)
        .filter(PayRunItem.pay_run_id == pay_run_id)
        .order_by(PayRunItem.staff_id)
        .all()
    )


def recent_earnings(db: Session, *, tenant_id: int, limit: int = 50) -> list[CommissionEarning]:
    return (
        tenant_query(db, CommissionEarning, tenant_id)
        .order_by(CommissionEarning.earned_at.desc(), CommissionEarning.id.desc())
        .limit(limit)
        .all()
    )
