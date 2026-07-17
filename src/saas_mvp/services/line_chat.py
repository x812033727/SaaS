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


# ── 後台回覆(R5-A4:/ui line-chat 與 console JSON API 共用) ──────────────────


class LineChatError(Exception):
    """後台回覆失敗(空內容/未設 token/推播失敗);訊息為使用者可讀文案。"""


def send_reply(
    db: Session,
    *,
    tenant_id: int,
    line_user_id: str,
    text: str,
    push_client,
) -> LineMessage:
    """店家回覆顧客:LINE push → 存 outbound → SSE 廣播;失敗拋 LineChatError。

    從 routers/ui/assistant.py 的 line_chat_reply 抽出,/ui HTML 與
    /api/v1 JSON 兩端點共用同一條路徑(行為與錯誤文案一致)。
    """
    from saas_mvp.line_client import LinePushError
    from saas_mvp.models.line_channel_config import LineChannelConfig
    from saas_mvp.services.events import publish_event

    text = (text or "").strip()
    if not text:
        raise LineChatError("回覆內容不可為空。")
    cfg = (
        db.query(LineChannelConfig)
        .filter(LineChannelConfig.tenant_id == tenant_id)
        .first()
    )
    try:
        token = cfg.access_token if cfg else None
    except Exception:  # noqa: BLE001 — 解密失敗視同未設定
        token = None
    if not token:
        raise LineChatError("尚未設定 LINE channel access token，無法回覆。")
    try:
        push_client.push(line_user_id, text, access_token=token)
    except LinePushError as exc:
        raise LineChatError(f"LINE 推播失敗：{exc}") from exc
    msg = record_outbound(
        db, tenant_id=tenant_id, line_user_id=line_user_id, text=text
    )
    publish_event(
        tenant_id,
        "line_message",
        line_user_id=line_user_id,
        text=text,
        direction="out",
    )
    return msg
