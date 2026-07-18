"""line_webhook 套件:booking 對話 + commerce replies(純搬移自 line_webhook.py 1017-2379)。"""
import datetime

from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.line_client import (
    LineProfileClient,
    LineReplyClient,
)
from saas_mvp.models.line_webhook_event import (
    LineWebhookEventStage,
)
from saas_mvp.translation import TranslationResult, Translator
from saas_mvp.booking.commands import parse_booking_command, parse_postback_data
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import booking_form as booking_form_svc
from saas_mvp.services import waitlist as waitlist_svc
from saas_mvp.services import catalog as catalog_svc
from saas_mvp.services import coupons as coupons_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import flex_menu as flex_menu_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.services import slots as slots_svc
from saas_mvp.services import staff as staff_svc
from saas_mvp.services.payment import get_payment_provider


from saas_mvp.routers.line_webhook._shared import (
    _log,
)


_BOOKING_HELP = (
    "可用指令：\n"
    "・時段 — 查看並選擇可預約時段\n"
    "・預約 — 引導式預約（或：預約 <時段編號> <人數>）\n"
    "・我的預約 — 查看我的預約\n"
    "・改期 <預約編號> — 引導改到其他時段\n"
    "・取消 <預約編號> — 例：取消 7\n"
    "・候補 — 查看/取消我的額滿候補"
    "\n・套票 — 查看可用服務次數與到期日"
    "\n・禮物卡 — 查看餘額；領取禮物卡 <卡號> — 加入錢包"
)
# follow（加好友）預設歡迎文案：租戶未自訂（welcome_message NULL/空白）時依 bot_mode 選用。
_DEFAULT_WELCOME_BOOKING = (
    "感謝加入好友！🎉\n"
    "點下方按鈕即可開始預約，或輸入「時段」查看可預約時段、「我的預約」管理既有預約。"
)
_DEFAULT_WELCOME_TRANSLATION = (
    "感謝加入好友！直接傳訊息即可自動翻譯；輸入 /lang <語言代碼> 可切換目標語言（例：/lang ja）。"
)
_DEFAULT_WELCOME_GENERIC = "感謝加入好友！有任何問題歡迎直接留言。"
# 歡迎訊息／非文字訊息引導的 quick-reply（booking 模式）。
_WELCOME_QUICK_REPLY = [
    ("開始預約", "action=book"),
    ("查看時段", "action=slots"),
    ("我的預約", "action=my"),
]
# 引導式人數上限（quick-reply 按鈕數）
_PARTY_CHOICES_MAX = 6
# 列給使用者選的時段上限（LINE quick-reply 最多 13 筆）
_SLOT_CHOICES_MAX = 12


def _booking_intent(event: dict) -> tuple[str | None, dict]:
    """由 message(text) 或 postback 取出 (action, params)。"""
    etype = event.get("type")
    if etype == "message" and event.get("message", {}).get("type") == "text":
        return parse_booking_command(event["message"].get("text", ""))
    if etype == "postback":
        action, params = parse_postback_data(event.get("postback", {}).get("data", ""))
        # datetimepicker（A1.3）：LINE 把選定日期放 postback.params.date，
        # 併入 params（data 內已帶 date 者優先，不覆蓋）。
        picker = event.get("postback", {}).get("params") or {}
        if action is not None and picker.get("date"):
            params.setdefault("date", picker["date"])
        return action, params
    return None, {}


def _available_slots(db: Session, tenant_id: int) -> list:
    return [
        s
        for s in slots_svc.list_slots(db, tenant_id=tenant_id, active_only=True)
        if s.online_available > 0
    ][:_SLOT_CHOICES_MAX]


def _available_slots_on_date(db: Session, tenant_id: int, date: str | None) -> list:
    """指定日期（'YYYY-MM-DD'）的可預約時段；date 缺/不合法時退回全部（安全降級）。"""
    base = [
        s
        for s in slots_svc.list_slots(db, tenant_id=tenant_id, active_only=True)
        if s.online_available > 0
    ]
    if date:
        base = [s for s in base if s.slot_start.date().isoformat() == date]
    return base[:_SLOT_CHOICES_MAX]


def _available_dates(db: Session, tenant_id: int, limit: int = 10) -> list[str]:
    """有可預約時段（online_available>0）的日期，去重 + 升冪排序 + 取前 limit 筆。"""
    seen: set[str] = set()
    dates: list[str] = []
    for s in slots_svc.list_slots(db, tenant_id=tenant_id, active_only=True):
        if s.online_available <= 0:
            continue
        d = s.slot_start.date().isoformat()
        if d not in seen:
            seen.add(d)
            dates.append(d)
    return sorted(dates)[:limit]


# 日期 quick-reply 上限（LINE quick-reply 最多 13 筆）
_DATE_CHOICES_MAX = 13


def _date_choice_buttons(service_id: int, dates: list[str]) -> list:
    """日期 → quick-reply 按鈕（postback action=pick_date，攜帶 service_id + date）。

    末位附 datetimepicker（A1.3）：日期多於按鈕上限或想選較遠日期時，
    可用原生日曆挑日；選定值由 LINE 放在 postback.params.date
    （_booking_intent 併入 params）。
    """
    _weekday_zh = ("一", "二", "三", "四", "五", "六", "日")
    buttons: list = []
    for d in dates[: _DATE_CHOICES_MAX - 1]:
        try:
            dt = datetime.date.fromisoformat(d)
            label = f"{dt.strftime('%m/%d')} (週{_weekday_zh[dt.weekday()]})"
        except ValueError:
            label = d
        buttons.append(
            (label, f"action=pick_date&service_id={service_id}&date={d}")
        )
    if dates:
        buttons.append({
            "type": "datetimepicker",
            "label": "📅 挑其他日期",
            "data": f"action=pick_date&service_id={service_id}",
            "mode": "date",
            "initial": dates[0],
            "min": dates[0],
            "max": dates[-1],
        })
    return buttons


def _slot_choice_buttons(slots: list) -> list[tuple[str, str]]:
    """時段 → quick-reply 按鈕（postback action=pick_slot）。"""
    return [
        (s.slot_start.strftime("%m/%d %H:%M"), f"action=pick_slot&slot_id={s.id}")
        for s in slots
    ]


def _party_choice_buttons(slot_id: int, max_party: int) -> list[tuple[str, str]]:
    """人數 → quick-reply 按鈕（postback action=book）。"""
    upper = max(1, min(_PARTY_CHOICES_MAX, max_party))
    return [
        (f"{n} 位", f"action=book&slot_id={slot_id}&party={n}")
        for n in range(1, upper + 1)
    ]


def _prompt_choose_slot(db: Session, tenant_id: int) -> tuple[str, list | None]:
    slots = _available_slots(db, tenant_id)
    if not slots:
        return "目前沒有可預約的時段。", None
    return "請選擇時段：", _slot_choice_buttons(slots)


# ── 引導式對話：服務 → 日期 → 員工 → 時段 → 確認（stateless，狀態以 postback 攜帶） ──

def _active_services(db: Session, tenant_id: int) -> list:
    """上架中的服務項目（供引導式第一步）。最多 12（carousel 上限）。"""
    return [
        s
        for s in catalog_svc.list_services(db, tenant_id=tenant_id)
        if s.is_active
    ][:flex_menu_svc.MAX_CARDS]


def _service_carousel(services: list) -> dict:
    """服務清單 → LINE Flex carousel（每張卡片一個「選擇」postback 按鈕）。"""
    bubbles = []
    for s in services:
        subtitle_parts = []
        if s.duration_minutes:
            subtitle_parts.append(f"{s.duration_minutes} 分鐘")
        if s.price_cents:
            subtitle_parts.append(f"${s.price_cents}")
        subtitle = "・".join(subtitle_parts) or "點選預約"
        bubbles.append(
            {
                "type": "bubble",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": s.name, "weight": "bold",
                         "size": "lg", "wrap": True},
                        {"type": "text", "text": subtitle, "size": "sm",
                         "color": "#888888", "wrap": True},
                    ],
                },
                "footer": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "action": {
                                "type": "postback",
                                "label": "選擇",
                                "data": f"action=pick_service&service_id={s.id}",
                                "displayText": f"選擇 {s.name}"[:300],
                            },
                        }
                    ],
                },
            }
        )
    return {
        "type": "flex",
        "altText": "請選擇服務項目",
        "contents": {"type": "carousel", "contents": bubbles},
    }


def _my_reservations_carousel(db: Session, tenant_id: int, rows: list) -> dict:
    """「我的預約」清單 → LINE Flex carousel（每張卡片附「取消預約」按鈕，上限 12）。"""
    from saas_mvp.models.booking_slot import BookingSlot

    rows = rows[:12]  # carousel 上限 12 張
    slot_ids = [r.slot_id for r in rows if r.slot_id is not None]
    slots = {}
    if slot_ids:
        slots = {
            s.id: s
            for s in db.query(BookingSlot)
            .filter(BookingSlot.tenant_id == tenant_id, BookingSlot.id.in_(slot_ids))
            .all()
        }
    bubbles = []
    for r in rows:
        slot = slots.get(r.slot_id)
        when = slot.slot_start.strftime("%m/%d %H:%M") if slot is not None else "—"
        bubbles.append({
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": f"預約 #{r.id}", "weight": "bold",
                     "size": "lg"},
                    {"type": "text", "text": f"時間：{when}", "size": "sm",
                     "color": "#555555", "wrap": True},
                    {"type": "text", "text": f"人數：{r.party_size} 位", "size": "sm",
                     "color": "#888888"},
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "action": {
                            "type": "postback",
                            "label": "改期",
                            "data": f"action=reschedule&reservation_id={r.id}",
                            "displayText": f"改期 #{r.id}",
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "postback",
                            "label": "取消預約",
                            "data": f"action=cancel&reservation_id={r.id}",
                            "displayText": f"取消預約 #{r.id}",
                        },
                    },
                ],
            },
        })
    return {
        "type": "flex",
        "altText": "你的預約",
        "contents": {"type": "carousel", "contents": bubbles},
    }


def _slots_fitting_service(db: Session, tenant_id: int, slots: list, service_id):
    """依服務時長過濾時段：slot 長度不足者剔除。

    slot_end 為 NULL 的舊資料一律放行（降級不擋，資訊不足不誤殺）；
    服務不存在或未設時長也放行。
    """
    if service_id is None:
        return slots
    try:
        service = catalog_svc.get_service(
            db, tenant_id=tenant_id, service_id=service_id
        )
    except Exception:  # noqa: BLE001 — 服務查無：不套過濾
        return slots
    duration = getattr(service, "duration_minutes", None)
    if duration:
        needed = datetime.timedelta(minutes=duration)
        slots = [
            s
            for s in slots
            if s.slot_end is None or (s.slot_end - s.slot_start) >= needed
        ]
    if features_svc.is_enabled(db, tenant_id, features_svc.BOOKABLE_RESOURCES):
        from saas_mvp.services import bookable_resources as resources_svc

        slots = [
            slot
            for slot in slots
            if resources_svc.slot_has_required_resources(
                db,
                tenant_id=tenant_id,
                service_id=service_id,
                slot=slot,
            )
        ]
    return slots


def _waitlist_join_buttons(
    slot_id: int | None,
    party_size: int,
    service_id: int | None = None,
    staff_id: int | None = None,
) -> list[tuple[str, str]] | None:
    """額滿回覆的「加入候補」quick-reply 按鈕。"""
    if slot_id is None:
        return None
    data = f"action=waitlist_join&slot_id={slot_id}&party={party_size}"
    if service_id is not None:
        data += f"&service_id={service_id}"
    if staff_id is not None:
        data += f"&staff_id={staff_id}"
    return [("加入候補", data)]


def _my_waitlist_reply(
    db: Session, tenant_id: int, line_user_id: str
) -> tuple[str, list | None]:
    """「候補」指令：列出有效候補 + 取消按鈕。"""
    if not line_user_id:
        return "無法識別使用者，請從 LINE 操作。", None
    entries = waitlist_svc.list_my_waitlist(
        db, tenant_id=tenant_id, line_user_id=line_user_id
    )
    if not entries:
        return "你目前沒有候補。時段額滿時可點「加入候補」登記。", None
    from saas_mvp.models.booking_slot import BookingSlot

    slot_ids = [e.slot_id for e in entries]
    slots = {
        s.id: s
        for s in db.query(BookingSlot)
        .filter(BookingSlot.tenant_id == tenant_id, BookingSlot.id.in_(slot_ids))
        .all()
    }
    lines = []
    buttons: list[tuple[str, str]] = []
    for e in entries[:13]:
        slot = slots.get(e.slot_id)
        when = slot.slot_start.strftime("%m/%d %H:%M") if slot else "—"
        state = "已通知" if e.status == "notified" else "等候中"
        lines.append(f"・{when}（{e.party_size} 位，{state}）")
        buttons.append(
            (f"取消候補 {when}"[:20], f"action=waitlist_cancel&entry_id={e.id}")
        )
    return "你的候補：\n" + "\n".join(lines), buttons


def _resched_date_buttons(
    reservation_id: int, dates: list[str]
) -> list[tuple[str, str]]:
    """改期：日期 → quick-reply（action=resched_date，前向攜帶 reservation_id）。"""
    _weekday_zh = ("一", "二", "三", "四", "五", "六", "日")
    buttons: list[tuple[str, str]] = []
    for d in dates[:_DATE_CHOICES_MAX]:
        try:
            dt = datetime.date.fromisoformat(d)
            label = f"{dt.strftime('%m/%d')} (週{_weekday_zh[dt.weekday()]})"
        except ValueError:
            label = d
        buttons.append(
            (label, f"action=resched_date&reservation_id={reservation_id}&date={d}")
        )
    return buttons


def _resched_slot_buttons(
    reservation_id: int, slots: list
) -> list[tuple[str, str]]:
    """改期：時段 → quick-reply（action=resched_slot，前向攜帶 reservation_id）。"""
    return [
        (
            s.slot_start.strftime("%m/%d %H:%M"),
            f"action=resched_slot&reservation_id={reservation_id}&slot_id={s.id}",
        )
        for s in slots
    ]


def _owned_confirmed_reservation(
    db: Session, tenant_id: int, reservation_id: int, line_user_id: str
):
    """取自己的 confirmed 預約；查無/他人/已取消回 (None, 錯誤訊息)。"""
    from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation

    resv = (
        db.query(Reservation)
        .filter(
            Reservation.tenant_id == tenant_id,
            Reservation.id == reservation_id,
        )
        .first()
    )
    if resv is None:
        return None, f"找不到預約 #{reservation_id}。"
    if resv.line_user_id != line_user_id:
        return None, "無法改期其他人的預約。"
    if resv.status != RESERVATION_CONFIRMED:
        return None, f"預約 #{reservation_id} 已取消，無法改期。"
    return resv, None


def _staff_choice_buttons(
    service_id: int, staff_list: list, date: str | None = None
) -> list[tuple[str, str]]:
    """員工 → quick-reply 按鈕（postback action=pick_staff）；首項為「不指定」。

    date（'YYYY-MM-DD'）若有則前向攜帶至每個按鈕（含「不指定」）。
    """
    suffix = f"&date={date}" if date else ""
    buttons: list[tuple[str, str]] = [
        ("不指定", f"action=pick_staff&service_id={service_id}{suffix}")
    ]
    for st in staff_list:
        buttons.append(
            (
                st.name[:20],
                f"action=pick_staff&service_id={service_id}"
                f"&staff_id={st.id}{suffix}",
            )
        )
    return buttons[:13]


def _service_staff(db: Session, tenant_id: int, service_id: int) -> list:
    """指派到該服務的 active 員工清單。"""
    links = catalog_svc.list_service_staff(
        db, tenant_id=tenant_id, service_id=service_id
    )
    out = []
    for link in links:
        try:
            st = staff_svc.get_staff(db, tenant_id=tenant_id, staff_id=link.staff_id)
        except Exception:  # noqa: BLE001 — 指派但員工已刪：略過
            continue
        if st.is_active:
            out.append(st)
    return out


def _slot_buttons_with_state(
    slots: list, service_id: int, staff_id: int | None
) -> list[tuple[str, str]]:
    """時段 → quick-reply，data 攜帶 service_id / staff_id 前向狀態。"""
    buttons = []
    for s in slots:
        data = f"action=pick_slot&service_id={service_id}&slot_id={s.id}"
        if staff_id is not None:
            data += f"&staff_id={staff_id}"
        buttons.append((s.slot_start.strftime("%m/%d %H:%M"), data))
    return buttons


def _confirm_text(db: Session, tenant_id: int, resv, slot_id: int) -> str:
    """建單成功確認文字 + 加入 Google 行事曆連結。"""
    from saas_mvp.services import calendar_ics

    base = (
        f"預約成功！\n預約編號：{resv.id}\n人數：{resv.party_size} 位\n"
        f"如需取消請輸入：取消 {resv.id}"
    )
    # R5-B2:顧客 portal「管理預約」連結(建單交易已補發 token;read-only)。
    if resv.customer_id is not None:
        from saas_mvp.models.customer import Customer as _Customer
        from saas_mvp.services import customer_portal as _portal_svc

        _cust = db.get(_Customer, resv.customer_id)
        if _cust is not None:
            _purl = _portal_svc.portal_url(_cust)
            if _purl:
                base += f"\n管理預約:{_purl}"
    # 定金（C4）:需付定金時置頂提示 + 付款連結。
    if getattr(resv, "deposit_status", None) == "pending" and settings.public_base_url:
        from saas_mvp.models.tenant import Tenant as _Tenant
        from saas_mvp.services import deposit as deposit_svc

        _t = db.get(_Tenant, tenant_id)
        if _t is not None:
            base = (
                "預約成功！⚠️ " + deposit_svc.deposit_prompt(resv, _t)
                + f"\n付款連結：{deposit_svc.payment_url(resv)}"
                + f"\n預約編號：{resv.id}(人數 {resv.party_size} 位)"
            )
    from saas_mvp.services import client_forms as client_forms_svc
    form_rows = client_forms_svc.for_reservation(
        db, tenant_id=tenant_id, reservation_id=resv.id
    )
    pending_forms = [row for row in form_rows if row.status == "pending"]
    if pending_forms:
        base += "\n預約前請完成：" + "；".join(
            f"{row.template_name_snapshot} {client_forms_svc.form_url(row)}"
            for row in pending_forms
        )
    # 取時段時間組「加入 Google 行事曆」連結。
    from saas_mvp.models.booking_slot import BookingSlot

    slot_obj = (
        db.query(BookingSlot)
        .filter(BookingSlot.tenant_id == tenant_id, BookingSlot.id == slot_id)
        .first()
    )
    if slot_obj is not None:
        start = slot_obj.slot_start
        end = slot_obj.slot_end or start
        url = calendar_ics.google_calendar_url(
            title="預約", start=start, end=end
        )
        base += f"\n加入 Google 行事曆：{url}"
    return base


def _try_conversational(
    db: Session,
    tenant_id: int,
    action: str | None,
    params: dict,
    line_user_id: str,
    display_name: str | None = None,
    source_webhook_event_id: str | None = None,
) -> tuple[str | None, list | None, dict | None] | None:
    """引導式對話步驟機（服務→日期→員工→時段→確認），以 postback 攜帶狀態。

    回傳 (text, quick_reply, flex) 表示「已由本流程處理」；回 None 表示本流程
    不接手，交回既有 _dispatch_booking（向後相容：無服務時退回原始時段流程）。

    優雅降級：沒有任何上架服務時，'book'（無 slot_id）不接手，由既有
    _prompt_choose_slot 處理，使既有 raw-slot 預約測試不受影響。
    """
    # /menu 或「選單」：推送租戶 active FlexMenu（圖文選單卡片）。
    if action == "menu":
        if not features_svc.is_enabled(db, tenant_id, features_svc.FLEX_MENU):
            return "本店尚未開放圖文選單。", None, None
        menu = flex_menu_svc.get_active_menu(db, tenant_id=tenant_id)
        if menu is None:
            return "目前沒有可用的選單。", None, None
        cards = flex_menu_svc.list_cards(db, tenant_id=tenant_id, menu_id=menu.id)
        if not cards:
            return "目前沒有可用的選單。", None, None
        return None, None, flex_menu_svc.build_flex_payload(menu, cards)

    # 「我的預約」→ Flex carousel（每張附取消按鈕）；無預約則回文字提示。
    if action == "my":
        rows = booking_svc.list_my_reservations(
            db, tenant_id=tenant_id, line_user_id=line_user_id
        )
        if not rows:
            return "你目前沒有預約。輸入「時段」開始預約。", None, None
        return None, None, _my_reservations_carousel(db, tenant_id, rows)

    # ── 改期三步（reschedule → resched_date → resched_slot）────────────────
    # 第一步：驗擁有者 → 日期 quick-reply（前向攜帶 reservation_id）。
    if action == "reschedule":
        reservation_id = params.get("reservation_id")
        if reservation_id is None:
            return "請指定預約編號，例：改期 7", None, None
        _resv, err = _owned_confirmed_reservation(
            db, tenant_id, reservation_id, line_user_id
        )
        if err is not None:
            return err, None, None
        dates = _available_dates(db, tenant_id)
        if not dates:
            return "目前沒有可改期的日期。", None, None
        return (
            f"改期預約 #{reservation_id}，請選擇新日期：",
            _resched_date_buttons(reservation_id, dates),
            None,
        )

    # 第二步：選定新日期 → 該日可預約時段 quick-reply。
    if action == "resched_date":
        reservation_id = params.get("reservation_id")
        if reservation_id is None:
            return "請重新輸入「改期 <預約編號>」開始。", None, None
        date = params.get("date")
        slots = _available_slots_on_date(db, tenant_id, date)
        if not slots:
            return "該日期目前沒有可預約的時段，請改選其他日期。", None, None
        return (
            "請選擇新時段：",
            _resched_slot_buttons(reservation_id, slots),
            None,
        )

    # 第三步：選定新時段 → 原子換 slot（服務層鎖雙 slot、單一 commit）。
    if action == "resched_slot":
        reservation_id = params.get("reservation_id")
        slot_id = params.get("slot_id")
        if reservation_id is None or slot_id is None:
            return "請重新輸入「改期 <預約編號>」開始。", None, None
        try:
            resv = booking_svc.reschedule_reservation(
                db,
                tenant_id=tenant_id,
                reservation_id=reservation_id,
                new_slot_id=slot_id,
                line_user_id=line_user_id,
            )
        except booking_svc.ReservationPermissionError:
            return "無法改期其他人的預約。", None, None
        except booking_svc.ReservationNotFoundError:
            return f"找不到可改期的預約 #{reservation_id}。", None, None
        except booking_svc.SlotNotFoundError:
            return f"找不到時段 #{slot_id}，請重新輸入「改期 {reservation_id}」。", None, None
        except booking_svc.SlotFullError:
            # 原預約保留；可候補新時段（名額釋出通知後再改期）。
            return (
                f"時段 #{slot_id} 已額滿，可加入候補或改選其他時段"
                f"（原預約 #{reservation_id} 仍保留）。",
                _waitlist_join_buttons(slot_id, 1),
                None,
            )
        except booking_svc.ResourceUnavailableError:
            return (
                "此時段所需的房間或設備已被預約，請改選其他時段。"
                f"（原預約 #{reservation_id} 仍保留）",
                None,
                None,
            )
        from saas_mvp.models.booking_slot import BookingSlot

        new_slot = (
            db.query(BookingSlot)
            .filter(
                BookingSlot.tenant_id == tenant_id, BookingSlot.id == resv.slot_id
            )
            .first()
        )
        when = (
            new_slot.slot_start.strftime("%m/%d %H:%M")
            if new_slot is not None
            else "—"
        )
        return (
            f"改期成功！\n預約 #{resv.id} 已改至 {when}\n"
            f"人數：{resv.party_size} 位",
            None,
            None,
        )

    # 引導式第一步：'book'（無 slot_id）且有上架服務 → 服務 carousel。
    if action == "book" and params.get("slot_id") is None:
        services = _active_services(db, tenant_id)
        if not services:
            return None  # 退回既有時段流程（優雅降級）
        return None, None, _service_carousel(services)

    # 第二步：選定服務 → 日期 quick-reply（只列有可預約時段的日期）。
    if action == "pick_service":
        service_id = params.get("service_id")
        if service_id is None:
            return None
        try:
            catalog_svc.get_service(db, tenant_id=tenant_id, service_id=service_id)
        except Exception:  # noqa: BLE001 — 服務不存在/跨租戶
            return "找不到該服務，請重新輸入「預約」。", None, None
        dates = _available_dates(db, tenant_id)
        if not dates:
            return "目前沒有可預約的日期。", None, None
        return (
            "請選擇日期：",
            _date_choice_buttons(service_id, dates),
            None,
        )

    # 第三步：選定（服務 + 日期）→ 員工 quick-reply（含「不指定」，攜帶日期）。
    if action == "pick_date":
        service_id = params.get("service_id")
        if service_id is None:
            return None
        date = params.get("date")
        staff_list = _service_staff(db, tenant_id, service_id)
        return (
            "請選擇服務人員：",
            _staff_choice_buttons(service_id, staff_list, date),
            None,
        )

    # 第四步：選定（服務 + 員工 + 日期）→ 該日期可預約時段 quick-reply（攜帶狀態）。
    if action == "pick_staff":
        service_id = params.get("service_id")
        if service_id is None:
            return None
        staff_id = params.get("staff_id")
        date = params.get("date")
        slots = _available_slots_on_date(db, tenant_id, date)
        # 依服務時長過濾（slot_end 為 NULL 的舊時段放行）。
        slots = _slots_fitting_service(db, tenant_id, slots, service_id)
        if not slots:
            return "該日期目前沒有時長足夠的可預約時段。", None, None
        return (
            "請選擇時段：",
            _slot_buttons_with_state(slots, service_id, staff_id),
            None,
        )

    # 第四步：選定時段（帶 service_id）→ 建單 + 確認。
    if action == "pick_slot" and params.get("service_id") is not None:
        service_id = params.get("service_id")
        staff_id = params.get("staff_id")
        slot_id = params.get("slot_id")
        party_size = params.get("party_size", 1)
        if slot_id is None:
            return _prompt_choose_slot(db, tenant_id) + (None,)
        try:
            resv = booking_svc.book_slot(
                db,
                tenant_id=tenant_id,
                slot_id=slot_id,
                party_size=party_size,
                line_user_id=line_user_id,
                display_name=display_name,
                staff_id=staff_id,
                service_id=service_id,
                source_webhook_event_id=source_webhook_event_id,
            )
        except booking_svc.CustomerBlacklistedError:
            return "很抱歉，您目前無法在線上預約，請直接與店家聯繫。", None, None
        except booking_svc.CrossTenantReferenceError:
            return "預約資料有誤，請重新輸入「預約」開始。", None, None
        except booking_svc.SlotNotFoundError:
            return f"找不到時段 #{slot_id}，請重新輸入「預約」查看。", None, None
        except booking_svc.SlotFullError:
            return (
                f"時段 #{slot_id} 已額滿，可加入候補（名額釋出時通知您）"
                f"或改選其他時段。",
                _waitlist_join_buttons(slot_id, party_size, service_id, staff_id),
                None,
            )
        except booking_svc.ResourceUnavailableError:
            return "此時段所需的房間或設備已被預約，請改選其他時段。", None, None
        return _confirm_text(db, tenant_id, resv, slot_id), None, None

    return None


def _dispatch_booking(
    db: Session,
    tenant_id: int,
    action: str | None,
    params: dict,
    line_user_id: str,
    raw_text: str = "",
    display_name: str | None = None,
    source_webhook_event_id: str | None = None,
) -> tuple[str, list | None]:
    """執行預約指令；回傳 (回覆文字, quick_reply 按鈕或 None)。預期錯誤轉友善訊息。"""
    # 引導式第一步：選時段（「時段」或「預約」無參數）
    if action == "slots" or (action == "book" and params.get("slot_id") is None):
        return _prompt_choose_slot(db, tenant_id)

    # 引導式第二步：已選時段，選人數
    if action == "pick_slot":
        slot_id = params.get("slot_id")
        slot = None
        if slot_id is not None:
            slot = next(
                (s for s in _available_slots(db, tenant_id) if s.id == slot_id), None
            )
        if slot is None:
            return _prompt_choose_slot(db, tenant_id)
        return (
            f"時段 {slot.slot_start.strftime('%m/%d %H:%M')}，請選擇人數：",
            _party_choice_buttons(slot_id, slot.online_available),
        )

    # 第三步 / 一次性：建單
    if action == "book":
        slot_id = params.get("slot_id")
        party_size = params.get("party_size", 1)
        service_id = params.get("service_id")
        staff_id = params.get("staff_id")
        try:
            resv = booking_svc.book_slot(
                db,
                tenant_id=tenant_id,
                slot_id=slot_id,
                party_size=party_size,
                line_user_id=line_user_id,
                display_name=display_name,
                service_id=service_id,
                staff_id=staff_id,
                source_webhook_event_id=source_webhook_event_id,
            )
        except booking_svc.CustomerBlacklistedError:
            return "很抱歉，您目前無法在線上預約，請直接與店家聯繫。", None
        except booking_svc.SlotNotFoundError:
            return f"找不到時段 #{slot_id}，請重新輸入「時段」查看。", None
        except booking_svc.SlotFullError:
            return (
                f"時段 #{slot_id} 已額滿，可加入候補（名額釋出時通知您）"
                f"或改選其他時段。",
                _waitlist_join_buttons(slot_id, party_size, service_id, staff_id),
            )
        except booking_svc.ResourceUnavailableError:
            return "此時段所需的房間或設備已被預約，請改選其他時段。", None
        # 統一走 _confirm_text（含行事曆連結與定金提示）。
        return _confirm_text(db, tenant_id, resv, slot_id), None

    if action == "my":
        rows = booking_svc.list_my_reservations(
            db, tenant_id=tenant_id, line_user_id=line_user_id
        )
        if not rows:
            return "你目前沒有預約。輸入「時段」開始預約。", None
        return "你的預約：\n" + "\n".join(
            f"#{r.id} {r.party_size} 位" for r in rows
        ), None

    if action == "confirm":
        reservation_id = params.get("reservation_id")
        if reservation_id is None or not line_user_id:
            return "請從提醒訊息的「確認出席」按鈕操作。", None
        try:
            booking_svc.confirm_reservation(
                db,
                tenant_id=tenant_id,
                reservation_id=reservation_id,
                line_user_id=line_user_id,
            )
        except booking_svc.ReservationNotFoundError:
            return f"找不到有效的預約 #{reservation_id}。", None
        except booking_svc.ReservationPermissionError:
            return "無法確認其他人的預約。", None
        return f"已為您確認預約 #{reservation_id}，期待您的光臨！", None

    if action == "rate":
        # 滿意度調查（A3.3）：問卷 quick-reply 的 1–5 分按鈕。
        reservation_id = params.get("reservation_id")
        score = params.get("score")
        if reservation_id is None or score is None or not line_user_id:
            return "請從調查訊息的評分按鈕操作。", None
        from saas_mvp.services import feedback as feedback_svc

        row = feedback_svc.record_score(
            db,
            tenant_id=tenant_id,
            reservation_id=reservation_id,
            line_user_id=line_user_id,
            score=score,
        )
        if row is None:
            return "找不到對應的調查，感謝您的回饋！", None
        if score <= 3:
            return (
                "非常抱歉這次的體驗未達期待 😔 您的意見已轉達店家，"
                "我們會持續改進，期待下次給您更好的服務。",
                None,
            )
        thanks = f"感謝您的 {score} 分好評！🎉 期待再次為您服務。"
        if features_svc.is_enabled(db, tenant_id, features_svc.COUPON_SYSTEM):
            thanks += "\n輸入「優惠券」看看本店的回饋活動！"
        return thanks, None

    if action == "cancel":
        reservation_id = params.get("reservation_id")
        if reservation_id is None:
            return "請指定預約編號，例：取消 7", None
        try:
            booking_svc.cancel_reservation(
                db,
                tenant_id=tenant_id,
                reservation_id=reservation_id,
                line_user_id=line_user_id,
            )
        except booking_svc.ReservationNotFoundError:
            return f"找不到預約 #{reservation_id}。", None
        except booking_svc.ReservationPermissionError:
            return "無法取消其他人的預約。", None
        return f"預約 #{reservation_id} 已取消。", None

    if action in ("coupons", "redeem"):
        if not features_svc.is_enabled(db, tenant_id, features_svc.COUPON_SYSTEM):
            return "本店尚未開放優惠券功能。", None
        if action == "coupons":
            return _list_coupons_reply(db, tenant_id)
        return _redeem_coupon_reply(db, tenant_id, params.get("code"), line_user_id), None

    if action == "points":
        return _points_reply(db, tenant_id, line_user_id), None

    if action == "packages":
        return _packages_reply(db, tenant_id, line_user_id), None

    if action in ("gift_cards", "claim_gift_card"):
        return _gift_cards_reply(
            db, tenant_id, line_user_id,
            claim_code=params.get("code") if action == "claim_gift_card" else None,
        ), None

    # 顧客自助留聯絡資料：PRIVACY_MODE 開通時回 tokenized PII 表單連結
    # （不在聊天室索取個資）；未開通回引導文案。
    if action == "contact":
        if not line_user_id:
            return "無法識別使用者，請從 LINE 操作。", None
        if not features_svc.is_enabled(db, tenant_id, features_svc.PRIVACY_MODE):
            return (
                "本店未開放線上填寫個資，如需留下聯絡方式請直接告知店家。",
                None,
            )
        from saas_mvp.services import pii as pii_svc

        return (
            pii_svc.push_form_link(
                db, tenant_id=tenant_id, line_user_id=line_user_id
            ),
            None,
        )

    # ── 額滿候補 ────────────────────────────────────────────────────────────
    if action == "waitlist":
        return _my_waitlist_reply(db, tenant_id, line_user_id)

    if action == "waitlist_join":
        slot_id = params.get("slot_id")
        if slot_id is None or not line_user_id:
            return "請從額滿時段的「加入候補」按鈕登記。", None
        try:
            entry = waitlist_svc.join_waitlist(
                db,
                tenant_id=tenant_id,
                slot_id=slot_id,
                line_user_id=line_user_id,
                party_size=params.get("party_size", 1),
                display_name=display_name,
                service_id=params.get("service_id"),
                staff_id=params.get("staff_id"),
            )
        except waitlist_svc.WaitlistSlotNotFound:
            return f"找不到時段 #{slot_id}。", None
        except waitlist_svc.SlotNotFullError:
            return (
                f"時段 #{slot_id} 目前有名額，可直接預約！",
                [("立即預約", f"action=pick_slot&slot_id={slot_id}")],
            )
        return (
            f"已加入候補！時段釋出 {entry.party_size} 位以上名額時會通知您。\n"
            f"輸入「候補」可查看或取消。",
            None,
        )

    if action == "waitlist_cancel":
        entry_id = params.get("entry_id")
        if entry_id is None:
            return "請輸入「候補」查看後，點選要取消的候補。", None
        try:
            waitlist_svc.cancel_waitlist(
                db,
                tenant_id=tenant_id,
                entry_id=entry_id,
                line_user_id=line_user_id,
            )
        except waitlist_svc.WaitlistEntryNotFound:
            return "找不到該筆候補。", None
        return "候補已取消。", None

    # AI 預約 agent（A2）：無法辨識的純文字先給 agent 補槽（AI_BOOKING_AGENT
    # 開通時）；agent 只填槽，建單走既有 pick_slot postback 確定性路徑。
    # 未開通 / LLM 失敗回 None → 落回下方既有 AI_ASSISTANT QA / 說明。
    if action is None and raw_text and line_user_id:
        from saas_mvp.services import ai_conversation as ai_conversation_svc

        agent_out = ai_conversation_svc.handle_free_text(
            db, tenant_id, line_user_id, raw_text
        )
        if agent_out is not None:
            return agent_out

    # AI 客服 fallback：無法辨識的純文字訊息，若租戶開通 AI_ASSISTANT，
    # 以 get_assistant() 回答（context 由 faq.match 注入）。surgical、behind flag。
    if action is None and raw_text and features_svc.is_enabled(
        db, tenant_id, features_svc.AI_ASSISTANT
    ):
        return _ai_reply(db, tenant_id, raw_text), None

    if action in ("shop", "buy", "my_orders"):
        if not features_svc.is_enabled(db, tenant_id, features_svc.PRODUCT_SALES):
            return "本店尚未開放商品購買功能。", None
        if action == "shop":
            return _list_products_reply(db, tenant_id)
        if action == "buy":
            return _buy_reply(db, tenant_id, params.get("product_id"), params.get("qty", 1), line_user_id, params.get("coupon")), None
        return _my_orders_reply(db, tenant_id, line_user_id), None

    # help 或無法辨識
    return _BOOKING_HELP, None


def _ai_reply(db: Session, tenant_id: int, text: str) -> str:
    """以 AI 助手回答自由文字（context 由 faq.build_context 注入）。失敗回退說明。"""
    from saas_mvp.ai import AIError, get_assistant
    from saas_mvp.services import faq as faq_svc

    assistant = get_assistant(db)
    context = faq_svc.build_context(
        db, tenant_id, text, max_entries=assistant.context_max_entries
    )
    # D4:無 FAQ 命中 → 記為「AI 答不好的問題」(upsert 去重,永不拋)
    if not context:
        faq_svc.record_unanswered(db, tenant_id=tenant_id, question=text)
    try:
        return assistant.answer(text, context).answer
    except AIError:
        faq_svc.record_unanswered(db, tenant_id=tenant_id, question=text)
        return _BOOKING_HELP


def _list_coupons_reply(db: Session, tenant_id: int) -> tuple[str, list | None]:
    """列出有效券，附 quick-reply 兌換按鈕。"""
    coupons = [c for c in coupons_svc.list_coupons(db, tenant_id=tenant_id) if c.is_active][:12]
    if not coupons:
        return "目前沒有可用的優惠券。", None
    buttons = [(f"兌換 {c.name}"[:20], f"action=redeem&code={c.code}") for c in coupons]
    return "可用優惠券：\n" + "\n".join(f"・{c.name}（{c.code}）" for c in coupons), buttons


def _redeem_coupon_reply(
    db: Session, tenant_id: int, code: str | None, line_user_id: str
) -> str:
    if not code:
        return "請輸入券碼，例：兌換 ABC123"
    if not line_user_id:
        return "無法識別使用者，請從 LINE 操作。"
    try:
        coupons_svc.redeem_coupon(
            db, tenant_id=tenant_id, code=code, line_user_id=line_user_id
        )
    except coupons_svc.CouponNotFound:
        return f"找不到券碼 {code}。"
    except coupons_svc.CouponInactive:
        return f"券碼 {code} 已停用。"
    except coupons_svc.CouponExpired:
        return f"券碼 {code} 不在有效期間。"
    except coupons_svc.CouponExhausted:
        return f"券碼 {code} 已被領完。"
    except coupons_svc.AlreadyRedeemed:
        return f"你已兌換過券碼 {code}。"
    return f"兌換成功！券碼 {code} 已套用。"


def _points_reply(db: Session, tenant_id: int, line_user_id: str) -> str:
    from saas_mvp.models.customer import Customer

    customer = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id, Customer.line_user_id == line_user_id)
        .first()
    )
    if customer is None:
        return "你目前沒有會員資料，完成預約後即可累積點數。"
    return f"你的點數：{customer.points_balance or 0}\n會員等級：{customer.tier or 'regular'}"


def _packages_reply(db: Session, tenant_id: int, line_user_id: str) -> str:
    if not features_svc.is_enabled(db, tenant_id, features_svc.SERVICE_PACKAGES):
        return "本店尚未開放服務套票功能。"
    from saas_mvp.models.customer import Customer
    from saas_mvp.services import service_packages as packages_svc

    customer = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id, Customer.line_user_id == line_user_id)
        .first()
    )
    if customer is None:
        return "你目前沒有服務套票。"
    wallet = packages_svc.customer_wallet(
        db, tenant_id=tenant_id, customer_id=customer.id
    )
    if not wallet:
        return "你目前沒有可用的服務套票（可能已用完或過期）。"
    lines = ["你的服務套票："]
    for credit in wallet[:20]:
        expires = credit.customer_package.expires_at.strftime("%Y-%m-%d")
        lines.append(
            f"・{credit.customer_package.package_name_snapshot}／{credit.service.name}："
            f"剩 {credit.remaining} 次（{expires} 到期）"
        )
    lines.append("網頁預約時可勾選「使用服務套票」自動扣次。")
    return "\n".join(lines)


def _gift_cards_reply(
    db: Session, tenant_id: int, line_user_id: str, claim_code: str | None = None
) -> str:
    if not features_svc.is_enabled(db, tenant_id, features_svc.GIFT_CARDS):
        return "本店尚未開放電子禮物卡功能。"
    from saas_mvp.models.customer import Customer
    from saas_mvp.services import gift_cards as gift_cards_svc

    customer = db.query(Customer).filter(
        Customer.tenant_id == tenant_id, Customer.line_user_id == line_user_id
    ).first()
    if customer is None:
        return "你目前沒有會員資料，完成一次預約後即可領取禮物卡。"
    if claim_code:
        try:
            gift_cards_svc.claim_card(
                db, tenant_id=tenant_id, code=claim_code, customer_id=customer.id
            )
            db.commit()
        except gift_cards_svc.GiftCardError as exc:
            db.rollback()
            return str(exc)
    wallet = gift_cards_svc.customer_wallet(
        db, tenant_id=tenant_id, customer_id=customer.id
    )
    if not wallet:
        return "你目前沒有可用的禮物卡。收到卡號後輸入：領取禮物卡 <卡號>"
    lines = ["你的禮物卡（永久有效）："]
    for item in wallet[:20]:
        lines.append(f"・末四碼 {item.card.code_last4}：NT$ {item.balance_cents // 100}")
    lines.append("可在店內結帳時出示卡號，餘額可分次使用。")
    return "\n".join(lines)


def _list_products_reply(db: Session, tenant_id: int) -> tuple[str, list | None]:
    products = shop_svc.list_products(db, tenant_id=tenant_id, active_only=True)
    products = [p for p in products if p.stock is None or p.stock > 0][:12]
    if not products:
        return "目前沒有可購買的商品。", None
    buttons = [
        (f"購買 {p.name}"[:20], f"action=buy&product_id={p.id}&qty=1") for p in products
    ]
    lines = "\n".join(f"・{p.name}（{p.price_cents} {p.currency}）" for p in products)
    return "可購買商品：\n" + lines, buttons


def _buy_reply(
    db: Session, tenant_id: int, product_id: int | None, qty: int, line_user_id: str,
    coupon_code: str | None = None,
) -> str:
    if product_id is None:
        return "請指定商品，例：購買 1 2（先輸入「商品」查看）"
    try:
        order = shop_svc.create_order(
            db,
            tenant_id=tenant_id,
            items=[(product_id, qty)],
            line_user_id=line_user_id or None,
            coupon_code=coupon_code or None,
        )
    except shop_svc.ProductNotFound:
        return f"找不到商品 #{product_id}。"
    except shop_svc.ProductInactive:
        return f"商品 #{product_id} 已下架。"
    except shop_svc.OutOfStock:
        return f"商品 #{product_id} 庫存不足。"
    except shop_svc.CouponApplyError as exc:
        return f"優惠券無法套用：{exc}"
    checkout = get_payment_provider(db).create_checkout(db, order=order)
    # 有折抵（會員等級 / 優惠券）時附上折抵金額，讓顧客看到優惠。
    discount_line = (
        f"已折抵：{order.discount_cents} {order.currency}\n"
        if (order.discount_cents or 0) > 0 else ""
    )
    return (
        f"已建立訂單 #{order.id}\n"
        f"{discount_line}"
        f"應付：{order.total_cents} {order.currency}\n"
        f"付款連結：{checkout}"
    )


def _my_orders_reply(db: Session, tenant_id: int, line_user_id: str) -> str:
    if not line_user_id:
        return "無法識別使用者。"
    orders = [
        o for o in shop_svc.list_orders(db, tenant_id=tenant_id)
        if o.line_user_id == line_user_id
    ]
    if not orders:
        return "你目前沒有訂單。輸入「商品」開始購買。"
    # pending 訂單附上付款連結:結帳 URL 改以不可猜 trade_no 為鍵後(PEA-3),
    # 舊訊息裡的整數 id 連結會失效,這裡是顧客重取連結的入口。僅對「純組 URL」
    # 的 provider 產生(ecpay/newebpay/stub);linepay 的 create_checkout 會真的
    # 打 Request API,不適合在列表查詢時逐單外呼。
    provider = get_payment_provider(db)
    lines = []
    for o in orders:
        line = f"#{o.id} {o.total_cents} {o.currency}（{o.status}）"
        if o.status == shop_svc.ORDER_PENDING and provider.name() != "linepay":
            line += f"\n　付款：{provider.create_checkout(db, order=o)}"
        lines.append(line)
    return "你的訂單：\n" + "\n".join(lines)


# 會實際建單的 booking 動作；只有這些動作才需向 LINE 取使用者 displayName，
# 避免「時段/我的預約/取消」等查詢類訊息也多打一次 profile API。
_BOOKING_CREATE_ACTIONS = {"book", "pick_slot"}


def _resolve_display_name(
    profile_client: LineProfileClient | None,
    line_user_id: str,
    access_token: str,
) -> str | None:
    """向 LINE 取使用者 displayName 供建單回填；任何失敗皆降級為 None，不阻擋建單。

    webhook event.source 只給 userId，displayName 需另呼叫 profile API 取得。
    profile API 僅對「已加 bot 好友」者回名字，非好友/封鎖回 404；網路/憑證失敗
    亦同——一律吞掉並回 None，由 book_slot 照常以 line_user_id 建單。
    """
    if not line_user_id or profile_client is None:
        return None
    try:
        profile = profile_client.get_profile(line_user_id, access_token=access_token)
    except Exception:  # noqa: BLE001 - profile 失敗不得中斷建單
        _log.warning(
            "LINE profile fetch failed for user %s; proceeding without display_name",
            line_user_id,
        )
        return None
    return profile.display_name if profile else None


def _handle_booking_event(
    db: Session,
    tenant_id: int,
    access_token: str,
    event: dict,
    line_client: LineReplyClient,
    stage_holder: list[str] | None = None,
    profile_client: LineProfileClient | None = None,
) -> str:
    """booking 模式事件處理：解析指令 → 執行 → reply（含引導式 quick-reply 按鈕）。

    冪等性：mutating 動作（book/cancel）在 _dispatch_booking 內 commit 後，
    才把 stage 標到 REPLY_SENT 並 reply；若 reply 失敗，event 記 FAILED@REPLY_SENT
    → 不重試 → 不會因重送而重複建單/取消（與翻譯路徑「已送出不重扣」同類語意）。
    """
    stage = LineWebhookEventStage.CLAIMED.value
    etype = event.get("type")
    # A0.2 冪等鍵:建單時掛上此 webhook 事件 id,重放同一事件不重複建單。
    webhook_event_id = event.get("webhookEventId")
    if etype not in ("message", "postback"):
        return stage  # 其他事件靜默略過（follow/unfollow 等）

    reply_token = event.get("replyToken", "")
    line_user_id = event.get("source", {}).get("userId", "")

    action, params = _booking_intent(event)
    # 取出原始文字（供無法辨識時的 AI 客服 fallback）。
    raw_text = ""
    if etype == "message" and event.get("message", {}).get("type") == "text":
        raw_text = event["message"].get("text", "")

    # 非文字訊息（貼圖/圖片/位置/語音等）：友善引導 + 預約 quick-reply。
    # 原本落到通用說明文字牆，對點錯/傳貼圖的顧客不友善。
    if etype == "message" and event.get("message", {}).get("type") != "text":
        stage = LineWebhookEventStage.REPLY_SENT.value
        if stage_holder is not None:
            stage_holder[0] = stage
        if reply_token:
            line_client.reply(
                reply_token,
                "收到您的訊息！需要預約服務嗎？點下方按鈕即可開始：",
                access_token=access_token,
                quick_reply=_WELCOME_QUICK_REPLY,
            )
        return stage

    # 後台客服：存檔顧客傳入的文字訊息 + SSE 推播到後台（best-effort，不影響預約）。
    if raw_text and line_user_id:
        try:
            from saas_mvp.services import line_chat as line_chat_svc
            from saas_mvp.services.events import publish_event

            line_chat_svc.record_inbound(
                db, tenant_id=tenant_id, line_user_id=line_user_id, text=raw_text
            )
            publish_event(
                tenant_id, "line_message",
                line_user_id=line_user_id, text=raw_text, direction="in",
            )
        except Exception:  # noqa: BLE001 — 客服存檔失敗不得影響預約主流程
            db.rollback()

    # 僅在會建單的動作向 LINE 取 displayName，供顧客檔回填（可核對是誰預約）。
    display_name = None
    if action in _BOOKING_CREATE_ACTIONS and line_user_id:
        display_name = _resolve_display_name(profile_client, line_user_id, access_token)

    # 引導式對話（服務→日期→員工→時段→確認）優先攔截；未接手者交回既有 dispatcher。
    conv = _try_conversational(
        db, tenant_id, action, params, line_user_id, display_name,
        source_webhook_event_id=webhook_event_id,
    )
    if conv is not None:
        reply_text, quick_reply, flex = conv
    else:
        # message 但非文字（圖片/貼圖）→ action 為 None；回說明
        reply_text, quick_reply = _dispatch_booking(
            db, tenant_id, action, params, line_user_id, raw_text, display_name,
            source_webhook_event_id=webhook_event_id,
        )
        flex = None

    # 網頁預約入口（A1.1）：WEB_BOOKING 開通時，booking 模式所有文字回覆
    # （含無 quick-reply 者）一律附「用網頁預約」URI 按鈕 — 通用入口，
    # token 深連結 TTL 30 分（token 便宜，每次回覆發一枚可接受）。
    # public_base_url 未設（dev）不附，避免無效 URI 讓 LINE 整則回覆被拒。
    if (
        flex is None
        and line_user_id
        and settings.public_base_url
        and features_svc.is_enabled(db, tenant_id, features_svc.WEB_BOOKING)
    ):
        try:
            form_row = booking_form_svc.issue_token(
                db,
                tenant_id=tenant_id,
                line_user_id=line_user_id,
                display_name=display_name,
            )
            quick_reply = list(quick_reply or [])[:12] + [{
                "type": "uri",
                "label": "🌐 用網頁預約",
                "uri": booking_form_svc.form_url(form_row),
            }]
        except Exception:  # noqa: BLE001 — 表單入口失敗不得阻擋主回覆
            db.rollback()

    # 副作用（若有）已於 dispatcher 內 commit；標記不可重試後再 reply。
    stage = LineWebhookEventStage.REPLY_SENT.value
    if stage_holder is not None:
        stage_holder[0] = stage
    if reply_token:
        if flex is not None:
            line_client.reply_flex(
                reply_token,
                flex.get("altText", "選單"),
                flex["contents"],
                access_token=access_token,
            )
        else:
            line_client.reply(
                reply_token,
                reply_text,
                access_token=access_token,
                quick_reply=quick_reply,
            )
    return stage


def _translate_sync(
    translator: Translator,
    text: str,
    target_lang: str,
) -> TranslationResult:
    """同步呼叫翻譯介面（背景任務內執行）。

    helper 封裝是為了維持單一翻譯呼叫點，未來換 async SDK 只改這裡。
    為何可 sync 直呼：見步驟 6c 註解（canonical 說明位置）。
    """
    return translator.translate(text, target_lang)
