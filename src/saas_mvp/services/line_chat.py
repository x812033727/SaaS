"""後台 LINE 客服服務 — 對話紀錄存取 + 後台回覆（push）。

對標 vibeaico「後台直接回覆顧客 LINE 訊息（不切到 LINE OA Manager）」。

- record_inbound / record_outbound：寫入 LineMessage（各自獨立 commit，與其他流程解耦）。
- list_conversations：每位 line_user 的最新一則 + 顯示名稱（join 顧客檔）。
- list_messages：單一對話的訊息序列（時間升序）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from saas_mvp.models.customer import Customer
from saas_mvp.models.line_message import (
    DIRECTION_IN,
    DIRECTION_OUT,
    LineMessage,
)
from saas_mvp.services.tenants import tenant_query


def _record(
    db: Session,
    *,
    tenant_id: int,
    line_user_id: str,
    text: str,
    direction: str,
    customer_id: int | None = None,
) -> LineMessage:
    msg = LineMessage(
        tenant_id=tenant_id,
        line_user_id=line_user_id,
        text=text,
        direction=direction,
        customer_id=customer_id,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def record_inbound(
    db: Session, *, tenant_id: int, line_user_id: str, text: str,
    customer_id: int | None = None,
) -> LineMessage:
    return _record(
        db, tenant_id=tenant_id, line_user_id=line_user_id, text=text,
        direction=DIRECTION_IN, customer_id=customer_id,
    )


def record_outbound(
    db: Session, *, tenant_id: int, line_user_id: str, text: str,
    customer_id: int | None = None,
) -> LineMessage:
    return _record(
        db, tenant_id=tenant_id, line_user_id=line_user_id, text=text,
        direction=DIRECTION_OUT, customer_id=customer_id,
    )


def list_messages(
    db: Session, *, tenant_id: int, line_user_id: str, limit: int = 200
) -> list[LineMessage]:
    rows = (
        tenant_query(db, LineMessage, tenant_id)
        .filter(LineMessage.line_user_id == line_user_id)
        .order_by(LineMessage.id.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(rows))  # 時間升序回傳


def list_conversations(db: Session, *, tenant_id: int) -> list[dict]:
    """每位 line_user 的對話摘要：最後一則內容/方向/時間 + 顧客顯示名稱。"""
    # 各 line_user 的最後一則 message id
    last_ids_subq = (
        select(func.max(LineMessage.id))
        .where(LineMessage.tenant_id == tenant_id)
        .group_by(LineMessage.line_user_id)
    )
    rows = (
        tenant_query(db, LineMessage, tenant_id)
        .filter(LineMessage.id.in_(last_ids_subq))
        .order_by(LineMessage.id.desc())
        .all()
    )
    # 預載顧客顯示名稱（line_user_id → display_name）。
    names: dict[str, str] = {}
    user_ids = [r.line_user_id for r in rows]
    if user_ids:
        for cust in (
            tenant_query(db, Customer, tenant_id)
            .filter(Customer.line_user_id.in_(user_ids))
            .all()
        ):
            if cust.line_user_id:
                names[cust.line_user_id] = cust.display_name or ""
    return [
        {
            "line_user_id": r.line_user_id,
            "display_name": names.get(r.line_user_id, ""),
            "last_text": r.text,
            "last_direction": r.direction,
            "last_at": r.created_at,
        }
        for r in rows
    ]
