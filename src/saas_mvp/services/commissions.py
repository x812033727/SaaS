"""員工抽成快照與薪資結算服務。"""

from __future__ import annotations

import datetime
import json
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from saas_mvp.models.commission import (
    BASIS_GROSS,
    ITEM_ALL,
    ITEM_TIP,
    METHOD_FIXED,
    METHOD_PERCENT,
    PAY_RUN_DRAFT,
    PAY_RUN_FINALIZED,
    PAY_RUN_PAID,
    PERIOD_BIWEEKLY,
    PERIOD_DAILY,
    PERIOD_FOUR_WEEK,
    PERIOD_MONTHLY,
    PERIOD_WEEKLY,
    STRUCTURE_FIXED,
    STRUCTURE_TIERED,
    VALID_BASES,
    VALID_GOAL_ITEM_TYPES,
    VALID_METHODS,
    VALID_RULE_ITEM_TYPES,
    VALID_SALES_PERIODS,
    CommissionEarning,
    CommissionRule,
    CommissionTier,
    PayRun,
    PayRunItem,
    StaffSalesGoal,
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
    return (
        value
        if value.tzinfo is not None
        else value.replace(tzinfo=datetime.timezone.utc)
    )


def _staff(db: Session, tenant_id: int, staff_id: int) -> Staff:
    row = tenant_query(db, Staff, tenant_id).filter(Staff.id == staff_id).first()
    if row is None:
        raise CommissionError("找不到該員工。")
    return row


def period_bounds(
    on_date: datetime.date, period: str, *, anchor: datetime.date
) -> tuple[datetime.date, datetime.date]:
    """返回包含 on_date 的銷售期間（含頭含尾）。

    雙週／四週以規則生效日為週期錨點，避免不同年份週碼斷層。
    """
    if period not in VALID_SALES_PERIODS:
        raise CommissionError("銷售期間不正確。")
    if period == PERIOD_DAILY:
        return on_date, on_date
    if period == PERIOD_WEEKLY:
        start = on_date - datetime.timedelta(days=on_date.weekday())
        return start, start + datetime.timedelta(days=6)
    if period in {PERIOD_BIWEEKLY, PERIOD_FOUR_WEEK}:
        days = 14 if period == PERIOD_BIWEEKLY else 28
        offset = max(0, (on_date - anchor).days)
        start = anchor + datetime.timedelta(days=(offset // days) * days)
        return start, start + datetime.timedelta(days=days - 1)
    if period == PERIOD_MONTHLY:
        start = on_date.replace(day=1)
        next_month = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
        return start, next_month - datetime.timedelta(days=1)
    quarter_month = ((on_date.month - 1) // 3) * 3 + 1
    start = on_date.replace(month=quarter_month, day=1)
    if quarter_month == 10:
        next_quarter = start.replace(year=start.year + 1, month=1)
    else:
        next_quarter = start.replace(month=quarter_month + 3)
    return start, next_quarter - datetime.timedelta(days=1)


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
        structure=STRUCTURE_FIXED,
        sales_period=None,
        value=value,
        calculation_basis=calculation_basis,
        effective_from=effective_from,
        is_active=True,
        created_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()
    return row


def save_tiered_rule(
    db: Session,
    *,
    tenant_id: int,
    staff_id: int,
    item_type: str,
    method: str,
    tiers: list[tuple[int, int]],
    calculation_basis: str,
    sales_period: str,
    effective_from: datetime.date,
    actor_user_id: int,
) -> CommissionRule:
    """新增階梯式規則；門檻為期間累計銷售額，歷史版本不覆寫。"""
    _staff(db, tenant_id, staff_id)
    if item_type not in VALID_RULE_ITEM_TYPES:
        raise CommissionError("抽成類型不正確。")
    if method not in VALID_METHODS:
        raise CommissionError("抽成方式不正確。")
    if calculation_basis not in VALID_BASES:
        raise CommissionError("抽成計算基礎不正確。")
    if sales_period not in VALID_SALES_PERIODS:
        raise CommissionError("銷售期間不正確。")
    normalized = sorted(set(tiers))
    if len(normalized) < 2 or len(normalized) > 10:
        raise CommissionError("階梯抽成需要 2 至 10 個級距。")
    if normalized[0][0] != 0:
        raise CommissionError("第一個級距必須從 0 開始。")
    thresholds = [threshold for threshold, _value in normalized]
    if thresholds != sorted(set(thresholds)) or any(
        value < 0 for _, value in normalized
    ):
        raise CommissionError("階梯級距不正確。")
    values = [value for _threshold, value in normalized]
    if values != sorted(values):
        raise CommissionError("階梯抽成值必須隨銷售門檻持平或提高。")
    if method == METHOD_PERCENT and any(value > 10_000 for _, value in normalized):
        raise CommissionError("百分比抽成不可超過 100%。")
    if method == METHOD_FIXED and any(value > 100_000_000 for _, value in normalized):
        raise CommissionError("固定抽成金額過大。")
    row = CommissionRule(
        tenant_id=tenant_id,
        staff_id=staff_id,
        item_type=item_type,
        method=method,
        structure=STRUCTURE_TIERED,
        sales_period=sales_period,
        value=normalized[0][1],
        calculation_basis=calculation_basis,
        effective_from=effective_from,
        is_active=True,
        created_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()
    for threshold_cents, value in normalized:
        db.add(
            CommissionTier(
                tenant_id=tenant_id,
                rule_id=row.id,
                threshold_cents=threshold_cents,
                value=value,
            )
        )
    db.flush()
    return row


def rule_tiers(db: Session, *, tenant_id: int, rule_id: int) -> list[CommissionTier]:
    return (
        tenant_query(db, CommissionTier, tenant_id)
        .filter(CommissionTier.rule_id == rule_id)
        .order_by(CommissionTier.threshold_cents, CommissionTier.id)
        .all()
    )


def latest_rules(
    db: Session, *, tenant_id: int
) -> dict[tuple[int, str], CommissionRule]:
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
    return db.execute(
        select(CommissionRule)
        .where(
            CommissionRule.tenant_id == tenant_id,
            CommissionRule.staff_id == staff_id,
            CommissionRule.item_type == item_type,
            CommissionRule.is_active.is_(True),
            CommissionRule.effective_from <= earned_date,
        )
        .order_by(CommissionRule.effective_from.desc(), CommissionRule.id.desc())
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()


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


def _tiered_amount(
    db: Session,
    *,
    rule: CommissionRule,
    earned_at: datetime.datetime,
    basis_cents: int,
    qty: int,
) -> tuple[int, int, str]:
    tiers = rule_tiers(db, tenant_id=rule.tenant_id, rule_id=rule.id)
    if len(tiers) < 2 or not rule.sales_period:
        raise CommissionError("階梯抽成規則不完整，請重新建立版本。")
    period_start, _period_end = period_bounds(
        earned_at.date(), rule.sales_period, anchor=rule.effective_from
    )
    period_start = max(period_start, rule.effective_from)
    start_at = datetime.datetime.combine(
        period_start, datetime.time.min, tzinfo=datetime.timezone.utc
    )
    basis_column = (
        CommissionEarning.gross_cents
        if rule.calculation_basis == BASIS_GROSS
        else CommissionEarning.net_cents
    )
    before = int(
        db.execute(
            select(func.coalesce(func.sum(basis_column), 0)).where(
                CommissionEarning.tenant_id == rule.tenant_id,
                CommissionEarning.staff_id == rule.staff_id,
                CommissionEarning.item_type == rule.item_type,
                CommissionEarning.reversal_of_id.is_(None),
                CommissionEarning.reversed_at.is_(None),
                CommissionEarning.earned_at >= start_at,
                CommissionEarning.earned_at <= earned_at,
            )
        ).scalar_one()
    )
    details: list[dict[str, int]] = []
    if rule.method == METHOD_FIXED:
        reached = before + basis_cents
        selected = max(
            (tier for tier in tiers if tier.threshold_cents <= reached),
            key=lambda tier: tier.threshold_cents,
        )
        amount = selected.value * qty
        details.append(
            {
                "threshold_cents": selected.threshold_cents,
                "value": selected.value,
                "basis_cents": basis_cents,
            }
        )
        return amount, before, json.dumps(details, separators=(",", ":"))

    sale_end = before + basis_cents
    amount = 0
    for index, tier in enumerate(tiers):
        next_threshold = (
            tiers[index + 1].threshold_cents if index + 1 < len(tiers) else sale_end
        )
        portion = max(
            0,
            min(sale_end, next_threshold) - max(before, tier.threshold_cents),
        )
        if portion <= 0:
            continue
        segment_amount = (portion * tier.value + 5_000) // 10_000
        amount += segment_amount
        details.append(
            {
                "threshold_cents": tier.threshold_cents,
                "value": tier.value,
                "basis_cents": portion,
                "commission_cents": segment_amount,
            }
        )
    return amount, before, json.dumps(details, separators=(",", ":"))


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
        sales_before = 0
        tier_detail = None
        if rule.structure == STRUCTURE_TIERED:
            amount, sales_before, tier_detail = _tiered_amount(
                db,
                rule=rule,
                earned_at=earned_at,
                basis_cents=basis_cents,
                qty=item.qty,
            )
        else:
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
            rule_structure_snapshot=rule.structure,
            sales_period_snapshot=rule.sales_period,
            period_sales_before_cents=sales_before,
            tier_detail_snapshot=tier_detail,
            commission_cents=amount,
            earned_at=earned_at,
        )
        db.add(earning)
        # autoflush 關閉；同筆訂單的下一項必須看得到已累計額。
        db.flush()
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
                rule_structure_snapshot=STRUCTURE_FIXED,
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
    grouped: dict[int, dict[str, int]] = defaultdict(
        lambda: {"commission": 0, "tip": 0}
    )
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
        attach_to = (
            pay_run.id
            if pay_run is not None and pay_run.status == PAY_RUN_DRAFT
            else None
        )
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
            rule_structure_snapshot=original.rule_structure_snapshot,
            sales_period_snapshot=original.sales_period_snapshot,
            period_sales_before_cents=original.period_sales_before_cents,
            tier_detail_snapshot=original.tier_detail_snapshot,
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
    start = datetime.datetime.combine(
        period_start, datetime.time.min, tzinfo=datetime.timezone.utc
    )
    end = datetime.datetime.combine(
        period_end + datetime.timedelta(days=1),
        datetime.time.min,
        tzinfo=datetime.timezone.utc,
    )
    earnings = (
        db.execute(
            select(CommissionEarning)
            .where(
                CommissionEarning.tenant_id == tenant_id,
                CommissionEarning.pay_run_id.is_(None),
                CommissionEarning.reversed_at.is_(None),
                CommissionEarning.earned_at >= start,
                CommissionEarning.earned_at < end,
            )
            .order_by(
                CommissionEarning.staff_id,
                CommissionEarning.earned_at,
                CommissionEarning.id,
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )
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


def get_pay_run(
    db: Session, *, tenant_id: int, pay_run_id: int, lock: bool = False
) -> PayRun:
    """lock=True 供狀態轉移用:鎖列避免 check-then-act 併發(調整已確認單/
    delete-vs-finalize 交錯造成已確認明細釋回池)。SQLite 無 FOR UPDATE,忽略。"""
    query = tenant_query(db, PayRun, tenant_id).filter(PayRun.id == pay_run_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
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
    run = get_pay_run(db, tenant_id=tenant_id, pay_run_id=pay_run_id, lock=True)
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
    run = get_pay_run(db, tenant_id=tenant_id, pay_run_id=pay_run_id, lock=True)
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
    run = get_pay_run(db, tenant_id=tenant_id, pay_run_id=pay_run_id, lock=True)
    if run.status != PAY_RUN_FINALIZED:
        raise CommissionError("結算單必須先確認，才能標記已付款。")
    run.status = PAY_RUN_PAID
    run.paid_at = _utcnow()
    run.paid_by_user_id = actor_user_id
    db.flush()
    return run


def delete_draft(db: Session, *, tenant_id: int, pay_run_id: int) -> None:
    run = get_pay_run(db, tenant_id=tenant_id, pay_run_id=pay_run_id, lock=True)
    if run.status != PAY_RUN_DRAFT:
        raise CommissionError("只有草稿結算單可以刪除。")
    earnings = (
        tenant_query(db, CommissionEarning, tenant_id)
        .filter(CommissionEarning.pay_run_id == run.id)
        .all()
    )
    # 沖銷配對(original 已標 reversed_at + 同 run 的負向 reversal)淨額為
    # 零,不可拆開釋回:sweep 會排除 original(reversed_at 過濾)卻收下
    # 裸 reversal,令下一張結算單憑空倒扣。配對成立時連 reversal 一併刪
    # 除(original 保留標記,不再可結算)。
    in_run_ids = {e.id for e in earnings}
    for earning in earnings:
        if (
            earning.reversal_of_id is not None
            and earning.reversal_of_id in in_run_ids
        ):
            db.delete(earning)
        else:
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


def recent_earnings(
    db: Session, *, tenant_id: int, limit: int = 50
) -> list[CommissionEarning]:
    return (
        tenant_query(db, CommissionEarning, tenant_id)
        .order_by(CommissionEarning.earned_at.desc(), CommissionEarning.id.desc())
        .limit(limit)
        .all()
    )


def save_sales_goal(
    db: Session,
    *,
    tenant_id: int,
    staff_id: int,
    item_type: str,
    target_cents: int,
    sales_period: str,
    effective_from: datetime.date,
    actor_user_id: int,
) -> StaffSalesGoal:
    _staff(db, tenant_id, staff_id)
    if item_type not in VALID_GOAL_ITEM_TYPES:
        raise CommissionError("業績目標類型不正確。")
    if sales_period not in VALID_SALES_PERIODS:
        raise CommissionError("銷售期間不正確。")
    if target_cents <= 0 or target_cents > 1_000_000_000:
        raise CommissionError("業績目標必須大於 0，且不可超過 NT$ 10,000,000。")
    row = StaffSalesGoal(
        tenant_id=tenant_id,
        staff_id=staff_id,
        item_type=item_type,
        target_cents=target_cents,
        sales_period=sales_period,
        effective_from=effective_from,
        is_active=True,
        created_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()
    return row


def latest_sales_goals(
    db: Session, *, tenant_id: int, on_date: datetime.date
) -> dict[tuple[int, str], StaffSalesGoal]:
    rows = (
        tenant_query(db, StaffSalesGoal, tenant_id)
        .filter(
            StaffSalesGoal.is_active.is_(True),
            StaffSalesGoal.effective_from <= on_date,
        )
        .order_by(
            StaffSalesGoal.staff_id,
            StaffSalesGoal.item_type,
            StaffSalesGoal.effective_from.desc(),
            StaffSalesGoal.id.desc(),
        )
        .all()
    )
    out: dict[tuple[int, str], StaffSalesGoal] = {}
    for row in rows:
        out.setdefault((row.staff_id, row.item_type), row)
    return out


def sales_goal_progress(
    db: Session, *, tenant_id: int, on_date: datetime.date
) -> list[dict]:
    goals = latest_sales_goals(db, tenant_id=tenant_id, on_date=on_date)
    results: list[dict] = []
    for goal in goals.values():
        period_start, period_end = period_bounds(
            on_date, goal.sales_period, anchor=goal.effective_from
        )
        period_start = max(period_start, goal.effective_from)
        start_at = datetime.datetime.combine(
            period_start, datetime.time.min, tzinfo=datetime.timezone.utc
        )
        end_at = datetime.datetime.combine(
            period_end + datetime.timedelta(days=1),
            datetime.time.min,
            tzinfo=datetime.timezone.utc,
        )
        filters = [
            Order.tenant_id == tenant_id,
            Order.status == ORDER_PAID,
            Order.paid_at >= start_at,
            Order.paid_at < end_at,
            OrderItem.tenant_id == tenant_id,
            OrderItem.staff_id == goal.staff_id,
        ]
        if goal.item_type != ITEM_ALL:
            filters.append(OrderItem.item_type == goal.item_type)
        actual = int(
            db.execute(
                select(func.coalesce(func.sum(OrderItem.line_total_cents), 0))
                .join(Order, Order.id == OrderItem.order_id)
                .where(*filters)
            ).scalar_one()
        )
        results.append(
            {
                "goal": goal,
                "actual_cents": actual,
                "percent": min(999.0, actual * 100 / goal.target_cents),
                "period_start": period_start,
                "period_end": period_end,
            }
        )
    return sorted(
        results, key=lambda row: (row["goal"].staff_id, row["goal"].item_type)
    )


def pay_run_export_data(
    db: Session, *, tenant_id: int, pay_run_id: int
) -> tuple[PayRun, list[tuple[PayRunItem, Staff | None]]]:
    run = get_pay_run(db, tenant_id=tenant_id, pay_run_id=pay_run_id)
    items = pay_run_items(db, tenant_id=tenant_id, pay_run_id=pay_run_id)
    staff_ids = {item.staff_id for item in items}
    staff_by_id = {
        row.id: row
        for row in tenant_query(db, Staff, tenant_id)
        .filter(Staff.id.in_(staff_ids or {-1}))
        .all()
    }
    return run, [(item, staff_by_id.get(item.staff_id)) for item in items]


def activity_export_data(
    db: Session,
    *,
    tenant_id: int,
    period_start: datetime.date,
    period_end: datetime.date,
    staff_id: int | None = None,
    item_type: str | None = None,
) -> list[CommissionEarning]:
    if period_end < period_start or (period_end - period_start).days > 366:
        raise CommissionError("匯出期間不正確，且不可超過一年。")
    if item_type and item_type not in {*VALID_RULE_ITEM_TYPES, ITEM_TIP}:
        raise CommissionError("匯出類型不正確。")
    start_at = datetime.datetime.combine(
        period_start, datetime.time.min, tzinfo=datetime.timezone.utc
    )
    end_at = datetime.datetime.combine(
        period_end + datetime.timedelta(days=1),
        datetime.time.min,
        tzinfo=datetime.timezone.utc,
    )
    query = tenant_query(db, CommissionEarning, tenant_id).filter(
        CommissionEarning.earned_at >= start_at,
        CommissionEarning.earned_at < end_at,
    )
    if staff_id is not None:
        _staff(db, tenant_id, staff_id)
        query = query.filter(CommissionEarning.staff_id == staff_id)
    if item_type:
        query = query.filter(CommissionEarning.item_type == item_type)
    return query.order_by(CommissionEarning.earned_at, CommissionEarning.id).all()
