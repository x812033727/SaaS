"""發票開立編排（C2）— 冪等、絕不擋金流回調。

流程:issue_for_charge(charge) → 以 subscription_charge_id 查重(回調重放
不重開)→ 先落 pending commit → 呼叫 issuer → 成功 issued / 失敗 failed
(留 error_msg 給 ops/retry_failed_invoices 重試)。任何例外不外拋。
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.models.invoice import (
    INVOICE_FAILED,
    INVOICE_ISSUED,
    INVOICE_PENDING,
    Invoice,
)
from saas_mvp.services.invoice_ecpay import InvoiceError, get_invoice_issuer

_log = logging.getLogger(__name__)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _relate_number(charge_id: int) -> str:
    """綠界 RelateNumber:≤30 英數、唯一。"""
    return f"SC{charge_id}T{int(_utcnow().timestamp())}"[:30]


def issue_for_charge(db: Session, charge, *, issuer=None) -> Invoice | None:
    """為一筆成功扣款開發票。冪等;永不拋錯(發票失敗不擋金流回調)。

    charge 需含 id/tenant_id/amount_cents;買受人=該租戶第一位 owner 的 email。
    """
    try:
        existing = db.execute(
            select(Invoice).where(Invoice.subscription_charge_id == charge.id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing  # 回調重放:已有列(任何狀態)不重開

        from saas_mvp.models.user import User
        from saas_mvp.config import settings
        from saas_mvp.services.platform_invoice_config import effective_invoice_config

        owner = db.execute(
            select(User).where(
                User.tenant_id == charge.tenant_id,
                User.role == "owner",
            ).order_by(User.id)
        ).scalars().first()
        buyer_email = owner.email if owner else ""

        config = effective_invoice_config(db, settings)
        row = Invoice(
            tenant_id=charge.tenant_id,
            subscription_charge_id=charge.id,
            relate_number=_relate_number(charge.id),
            amount_cents=charge.amount_cents,
            buyer_email=buyer_email,
            status=INVOICE_PENDING,
            provider=config.provider,
        )
        db.add(row)
        db.commit()  # 先落 pending:issuer 掛掉也留有可重試的紀錄
        db.refresh(row)

        _attempt_issue(db, row, issuer=issuer)
        return row
    except Exception:  # noqa: BLE001 — 發票絕不影響金流回調
        _log.warning(
            "issue_for_charge unexpected failure charge=%s",
            getattr(charge, "id", "?"), exc_info=True,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _attempt_issue(db: Session, row: Invoice, *, issuer=None) -> None:
    """對一筆 pending/failed 發票列嘗試開立(供首開與 ops 重試共用)。"""
    effective = issuer or get_invoice_issuer(db)
    try:
        result = effective.issue(
            relate_number=row.relate_number,
            amount_twd=row.amount_cents // 100,
            buyer_email=row.buyer_email or "",
            item_name="LINE 預約平台月費",
        )
        row.status = INVOICE_ISSUED
        row.invoice_no = result.invoice_no
        row.invoice_date = result.invoice_date
        row.random_number = result.random_number
        row.issued_at = _utcnow()
        row.error_msg = None
    except InvoiceError as exc:
        row.status = INVOICE_FAILED
        row.error_msg = str(exc)[:255]
        _log.warning("invoice issue failed relate=%s: %s", row.relate_number, exc)
    db.commit()
