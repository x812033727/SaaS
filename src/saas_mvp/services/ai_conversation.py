"""AI 對話驅動（A2）— 自然語言預約的伺服器端狀態機。

流程（每輪恰一次 LLM 呼叫，AI 只填槽）：
  1. 閘門：AI_BOOKING_AGENT 開通、額度未罄（超額降級引導式，不中斷）。
  2. 載入/重置對話（TTL 過期即重置；既有指令/postback 不經過本模組 —
     指令天然優先於 AI session）。
  3. agent.converse 抽槽 → 伺服器端驗證（服務存在、日期可約、人數 1–6）
     後才併入 slots。
  4. 確定性推進：
     * service + date 齊 → 列該日可約時段 quick-reply（pick_slot postback
       攜 service_id/party → **走既有引導式建單路徑**，AI 永不直接建單）。
     * 缺 date → 用 agent 回覆追問 + 日期按鈕。
     * 缺 service（店家有服務目錄）→ 追問 + 熱門服務按鈕。
  5. 後扣計量（回覆成功組出才 consume）；對話與計量同交易 commit。
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.ai.base import AIError
from saas_mvp.models.line_conversation import (
    STATE_FILLING,
    LineConversation,
)
from saas_mvp.services import ai_context as ai_context_svc
from saas_mvp.services import ai_quota as ai_quota_svc
from saas_mvp.services import features as features_svc

_log = logging.getLogger(__name__)

_TTL_MINUTES = 30
_QUOTA_EXCEEDED = "本月 AI 對話額度已用完，請點下方按鈕用選單預約："
_FALLBACK_BUTTONS = [
    ("開始預約", "action=book"),
    ("查看時段", "action=slots"),
    ("我的預約", "action=my"),
]


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _load_conversation(
    db: Session, tenant_id: int, line_user_id: str
) -> LineConversation:
    now = _utcnow()
    row = db.execute(
        select(LineConversation).where(
            LineConversation.tenant_id == tenant_id,
            LineConversation.line_user_id == line_user_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = LineConversation(
            tenant_id=tenant_id,
            line_user_id=line_user_id,
            state=STATE_FILLING,
            expires_at=now + datetime.timedelta(minutes=_TTL_MINUTES),
        )
        db.add(row)
        db.flush()
        return row
    exp = row.expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=datetime.timezone.utc)
    if exp is None or exp <= now:
        # TTL 過期：重置槽位（中斷後無殘留記憶）。
        row.slots = {}
        row.turn_count = 0
    row.state = STATE_FILLING
    row.expires_at = now + datetime.timedelta(minutes=_TTL_MINUTES)
    return row


def handle_free_text(
    db: Session,
    tenant_id: int,
    line_user_id: str,
    text: str,
    *,
    agent=None,
) -> tuple[str, list | None] | None:
    """自然語言入口。回 None = 未接手（呼叫端走既有 AI_ASSISTANT QA / 說明）。"""
    if not line_user_id or not text:
        return None
    if not features_svc.is_enabled(
        db, tenant_id, features_svc.AI_BOOKING_AGENT
    ):
        return None

    if not ai_quota_svc.has_ai_quota(db, tenant_id):
        return _QUOTA_EXCEEDED, list(_FALLBACK_BUTTONS)

    from saas_mvp.ai.agent import get_agent

    effective_agent = agent or get_agent()
    conv = _load_conversation(db, tenant_id, line_user_id)
    context = ai_context_svc.build_agent_context(db, tenant_id)

    try:
        turn = effective_agent.converse(text, conv.slots, context)
    except AIError:
        _log.warning("AI agent failed for tenant %d", tenant_id, exc_info=True)
        db.rollback()
        return None  # LLM 掛掉：交回既有路徑，不中斷服務

    # 只在有「預約意圖」時接手：本輪抽到槽位、或對話已在補槽中。
    # 純 QA 問題（「退貨怎麼處理？」）不搶 — 交回 AI_ASSISTANT FAQ 客服，
    # 也不消耗 AI 對話額度。
    extracted = any(
        v is not None for v in (turn.service_id, turn.date, turn.party_size)
    )
    if not extracted and not conv.slots:
        db.rollback()
        return None

    slots = dict(conv.slots)
    _merge_validated(db, tenant_id, slots, turn)
    conv.slots = slots
    conv.turn_count = (conv.turn_count or 0) + 1

    reply, buttons = _advance(db, tenant_id, slots, turn)

    # 後扣：回覆已組出才計量；對話狀態 + 計量同交易一次 commit。
    ai_quota_svc.consume_ai_in_txn(db, tenant_id)
    db.commit()
    return reply, buttons


def _merge_validated(db: Session, tenant_id: int, slots: dict, turn) -> None:
    """抽取值經伺服器驗證才採納（防幻覺）。"""
    from saas_mvp.services import booking_form as bf_svc

    if turn.service_id is not None:
        active_ids = {s.id for s in bf_svc.active_services(db, tenant_id)}
        if turn.service_id in active_ids:
            slots["service_id"] = turn.service_id
    if turn.date is not None:
        if turn.date in bf_svc.available_dates(db, tenant_id, limit=60):
            slots["date"] = turn.date
        else:
            slots.pop("date", None)
            slots["_bad_date"] = turn.date  # 供回覆說明
    if turn.party_size is not None and 1 <= turn.party_size <= 6:
        slots["party_size"] = turn.party_size


def _advance(
    db: Session, tenant_id: int, slots: dict, turn
) -> tuple[str, list | None]:
    """依已蒐集槽位決定下一步（確定性；建單走既有 pick_slot postback）。"""
    from saas_mvp.services import booking_form as bf_svc

    bad_date = slots.pop("_bad_date", None)
    service_id = slots.get("service_id")
    date = slots.get("date")
    party = slots.get("party_size", 1)

    services = bf_svc.active_services(db, tenant_id)

    # 齊了：列時段按鈕（pick_slot 攜帶狀態 → 既有確定性建單）。
    if date and (service_id or not services):
        candidates = bf_svc.slots_for(db, tenant_id, date=date, service_id=service_id)[:11]
        if candidates:
            buttons = [
                (
                    s.slot_start.strftime("%H:%M"),
                    "action=pick_slot"
                    + f"&slot_id={s.id}"
                    + (f"&service_id={service_id}" if service_id else "")
                    + f"&party={party}",
                )
                for s in candidates
            ]
            label = next(
                (s.name for s in services if s.id == service_id), "預約"
            )
            return (
                f"為您找到 {date} 的「{label}」時段，點選即可預約"
                f"（{party} 位）：",
                buttons,
            )
        dates = bf_svc.available_dates(db, tenant_id, limit=8)
        slots.pop("date", None)
        return (
            f"{date} 目前沒有可預約的時段 😢 這些日期還有空檔：",
            [(d, _date_button_data(service_id, d)) for d in dates] or None,
        )

    # 日期壞掉/不可約。
    if bad_date:
        dates = bf_svc.available_dates(db, tenant_id, limit=8)
        return (
            f"{bad_date} 沒有開放預約，可以選這些日期：",
            [(d, _date_button_data(service_id, d)) for d in dates] or None,
        )

    # 缺服務（店家有目錄）：追問 + 服務按鈕。
    if services and service_id is None:
        buttons = [
            (s.name[:20], f"action=pick_service&service_id={s.id}")
            for s in services[:11]
        ]
        ask = turn.reply_text or "請問想預約哪個服務呢？"
        return ask, buttons

    # 缺日期：追問 + 日期按鈕。
    dates = bf_svc.available_dates(db, tenant_id, limit=8)
    ask = turn.reply_text or "請問想約哪一天呢？"
    return ask, [(d, _date_button_data(service_id, d)) for d in dates] or None


def _date_button_data(service_id: int | None, date: str) -> str:
    if service_id:
        return f"action=pick_date&service_id={service_id}&date={date}"
    return f"action=pick_slot&date={date}"  # 無服務目錄店家：raw slot 流程
