"""Console JSON API(R3-C1)— Next.js saas-console 的日常營運端點。

薄 pydantic 層包既有 service:
* GET /api/v1/reservations       — enriched 預約列(join 時段/顧客/員工/服務)
* GET /api/v1/customers/{id}/reservations — 單一顧客的預約歷史
* GET /api/v1/calendar/month|week — 直接包 services/calendar_view
* GET /api/v1/dashboard/today    — 今日營運快照(預約+摘要+營收+待辦)

皆為 Bearer JWT + 租戶隔離 + rate limit;時間語意沿用 /ui(naive 顯示值),
console 與 Jinja UI 顯示一致。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from saas_mvp.auth.dependencies import Actor, get_current_actor
from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.line_client import LinePushClient, get_push_client
from saas_mvp.models.booking_waitlist import WAITLIST_WAITING, WaitlistEntry
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.models.user import User
from saas_mvp.services import analytics as analytics_svc
from saas_mvp.services import audit as audit_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import calendar_view as calendar_svc
from saas_mvp.services import customers as customers_svc
from saas_mvp.services.tenants import tenant_query

router = APIRouter(
    prefix="/api/v1",
    tags=["v1-console"],
    dependencies=[Depends(require_rate_limit)],
)


class ReservationRow(BaseModel):
    id: int
    status: str
    party_size: int
    attended: bool | None
    line_user_id: str | None
    deposit_status: str | None
    deposit_cents: int | None
    slot_id: int
    slot_start: datetime.datetime
    slot_end: datetime.datetime | None
    customer_id: int | None
    customer_name: str | None
    customer_phone: str | None
    staff_id: int | None
    staff_name: str | None
    service_id: int | None
    service_name: str | None


def _parse_date(value: str | None, field: str) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} 需為 YYYY-MM-DD")


def _day_start(d: datetime.date) -> datetime.datetime:
    # 沿用 calendar_view 的 naive 日界線語意,與 /ui 顯示一致。
    return datetime.datetime(d.year, d.month, d.day)


@router.get("/reservations", response_model=list[ReservationRow])
def list_reservations_enriched(
    response: Response,
    date_from: str | None = Query(default=None, description="YYYY-MM-DD(含)"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD(不含)"),
    status: str | None = Query(default=None, alias="status"),
    customer_id: int | None = Query(default=None),
    staff_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    df = _parse_date(date_from, "date_from")
    dt = _parse_date(date_to, "date_to")
    kwargs = dict(
        tenant_id=current_user.tenant_id,
        date_from=_day_start(df) if df else None,
        date_to=_day_start(dt) if dt else None,
        status=status,
        customer_id=customer_id,
        staff_id=staff_id,
    )
    rows = booking_svc.list_reservations_enriched(db, **kwargs, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(
        booking_svc.count_reservations_enriched(db, **kwargs)
    )
    return rows


@router.get("/customers/{customer_id}/reservations", response_model=list[ReservationRow])
def customer_reservations(
    customer_id: int,
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    # 顧客必須屬於本租戶(查無即 404,防跨租戶枚舉)。
    customers_svc.get_customer(
        db, tenant_id=current_user.tenant_id, customer_id=customer_id
    )
    kwargs = dict(tenant_id=current_user.tenant_id, customer_id=customer_id)
    rows = booking_svc.list_reservations_enriched(db, **kwargs, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(
        booking_svc.count_reservations_enriched(db, **kwargs)
    )
    return rows


@router.get("/calendar/month")
def calendar_month(
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return calendar_svc.build_month(
        db, tenant_id=current_user.tenant_id, year=year, month=month
    )


@router.get("/calendar/week")
def calendar_week(
    anchor: str = Query(description="YYYY-MM-DD;回該日所在週(週一起)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    d = _parse_date(anchor, "anchor")
    return calendar_svc.build_week(db, tenant_id=current_user.tenant_id, anchor=d)


@router.get("/dashboard/today")
def dashboard_today(
    date: str | None = Query(default=None, description="YYYY-MM-DD;預設今天"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant_id = current_user.tenant_id
    day = _parse_date(date, "date") or datetime.date.today()
    start, end = _day_start(day), _day_start(day + datetime.timedelta(days=1))

    reservations = booking_svc.list_reservations_enriched(
        db, tenant_id=tenant_id, date_from=start, date_to=end, limit=200
    )
    reservations.reverse()  # 當日由早到晚

    now = datetime.datetime.now()
    attendance_unmarked = sum(
        1 for r in reservations
        if r["status"] == RESERVATION_CONFIRMED
        and r["attended"] is None
        and r["slot_start"].replace(tzinfo=None) < now
    )
    waitlist_waiting = (
        tenant_query(db, WaitlistEntry, tenant_id)
        .filter(WaitlistEntry.status == WAITLIST_WAITING)
        .count()
    )
    deposits_pending = (
        tenant_query(db, Reservation, tenant_id)
        .filter(Reservation.deposit_status == "pending")
        .count()
    )

    return {
        "date": day.isoformat(),
        "reservations": [ReservationRow(**r).model_dump() for r in reservations],
        "summary": analytics_svc.booking_summary(
            db, tenant_id=tenant_id, date_from=start, date_to=end
        ),
        "revenue": analytics_svc.revenue_summary(
            db, tenant_id=tenant_id, date_from=start, date_to=end
        ),
        "pending": {
            "waitlist_waiting": waitlist_waiting,
            "deposits_pending": deposits_pending,
            "attendance_unmarked": attendance_unmarked,
        },
    }


# ── R5-A3:通知與推播歷程(console /notifications 頁) ─────────────────────────


class BookingNotificationRow(BaseModel):
    id: int
    reservation_id: int | None
    kind: str
    status: str
    payload_text: str
    sent_at: datetime.datetime | None
    attempt_count: int
    last_error: str | None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class CampaignSendRow(BaseModel):
    id: int
    campaign_id: int
    customer_id: int | None
    status: str
    period_key: str
    sent_at: datetime.datetime | None
    attempt_count: int
    last_error: str | None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class PushUsageRow(BaseModel):
    period: str
    used: int


@router.get("/notifications/bookings", response_model=list[BookingNotificationRow])
def list_booking_notifications(
    response: Response,
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """預約異動通知歷程(新→舊);X-Total-Count 供前端分頁。"""
    from saas_mvp.services import notifications_history as notif_history_svc

    rows, total = notif_history_svc.list_booking_notifications(
        db,
        tenant_id=current_user.tenant_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return rows


@router.get(
    "/notifications/campaign-sends", response_model=list[CampaignSendRow]
)
def list_campaign_sends(
    response: Response,
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """行銷發送紀錄(新→舊);X-Total-Count 供前端分頁。"""
    from saas_mvp.services import notifications_history as notif_history_svc

    rows, total = notif_history_svc.list_campaign_sends(
        db,
        tenant_id=current_user.tenant_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return rows


@router.get("/notifications/push-usage", response_model=list[PushUsageRow])
def push_usage(
    months: int = Query(default=6, ge=1, le=24),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """近 N 個月推播用量(新→舊,缺月補 0)。"""
    from saas_mvp.services import notifications_history as notif_history_svc

    return notif_history_svc.push_usage_history(
        db, tenant_id=current_user.tenant_id, months=months
    )


# ── R5-A4:LINE 客服(console /line-chat 頁) ──────────────────────────────────


class ConversationRow(BaseModel):
    line_user_id: str
    display_name: str
    last_text: str
    last_direction: str
    last_at: datetime.datetime


class LineMessageRow(BaseModel):
    id: int
    line_user_id: str
    direction: str
    text: str
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class ReplyBody(BaseModel):
    text: str


@router.get("/line-chat/conversations", response_model=list[ConversationRow])
def line_chat_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """對話列表(每位 line_user 最後一則+顧客顯示名,新→舊)。"""
    from saas_mvp.services import line_chat as line_chat_svc

    return line_chat_svc.list_conversations(
        db, tenant_id=current_user.tenant_id
    )


@router.get(
    "/line-chat/{line_user_id}/messages", response_model=list[LineMessageRow]
)
def line_chat_messages(
    line_user_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """單一對話訊息序列(時間升序)。"""
    from saas_mvp.services import line_chat as line_chat_svc

    return line_chat_svc.list_messages(
        db,
        tenant_id=current_user.tenant_id,
        line_user_id=line_user_id,
        limit=limit,
    )


@router.post(
    "/line-chat/{line_user_id}/reply",
    response_model=LineMessageRow,
    status_code=201,
)
def line_chat_reply(
    line_user_id: str,
    body: ReplyBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    push_client: LinePushClient = Depends(get_push_client),
):
    """店家回覆顧客(push+存檔+SSE);與 /ui/line-chat 共用 send_reply。"""
    from saas_mvp.services import line_chat as line_chat_svc

    try:
        return line_chat_svc.send_reply(
            db,
            tenant_id=current_user.tenant_id,
            line_user_id=line_user_id,
            text=body.text,
            push_client=push_client,
        )
    except line_chat_svc.LineChatError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ────────────────────────── 電子禮物卡(R7-C1)──────────────────────────
# 與 /ui/gift-cards 共用 services/gift_cards;發卡/作廢限 owner(比照
# require_ui_owner 的角色語意),feature 閘門沿 require_feature(403)。


def _require_owner(actor: Actor) -> None:
    """API 版 owner 檢查:比照 require_ui_owner(NULL 防禦性視為 owner)。"""
    role = getattr(actor.user, "role", None) or "owner"
    if role != "owner" and not actor.user.is_admin:
        raise HTTPException(status_code=403, detail="此操作僅限負責人。")


class GiftCardRow(BaseModel):
    id: int
    code_last4: str
    status: str
    initial_value_cents: int
    balance_cents: int
    recipient_customer_id: int | None
    purchaser_name: str | None
    recipient_name: str | None
    message: str | None
    created_at: datetime.datetime


class GiftCardIssued(BaseModel):
    card: GiftCardRow
    code: str | None
    created: bool


class GiftCardIssueBody(BaseModel):
    amount_twd: int
    fulfillment_guarantee: str
    issuance_key: str
    compliance_ack: bool = False
    recipient_customer_id: int | None = None
    purchaser_name: str = ""
    recipient_name: str = ""
    message: str = ""


class GiftCardVoidBody(BaseModel):
    note: str = ""


def _gift_card_row(balance) -> GiftCardRow:
    card = balance.card
    return GiftCardRow(
        id=card.id,
        code_last4=card.code_last4,
        status=card.status,
        initial_value_cents=card.initial_value_cents,
        balance_cents=balance.balance_cents,
        recipient_customer_id=card.recipient_customer_id,
        purchaser_name=card.purchaser_name,
        recipient_name=card.recipient_name,
        message=card.message,
        created_at=card.created_at,
    )


@router.get(
    "/gift-cards",
    response_model=list[GiftCardRow],
    dependencies=[Depends(features_svc.require_feature(features_svc.GIFT_CARDS))],
)
def list_gift_cards(
    response: Response,
    limit: int = Query(100, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """近期發行的禮物卡(含 ledger 加總餘額)。"""
    from saas_mvp.services import gift_cards as gift_cards_svc

    rows = gift_cards_svc.recent_cards(
        db, tenant_id=current_user.tenant_id, limit=limit
    )
    response.headers["X-Total-Count"] = str(len(rows))
    return [_gift_card_row(b) for b in rows]


@router.post(
    "/gift-cards",
    response_model=GiftCardIssued,
    status_code=201,
    dependencies=[Depends(features_svc.require_feature(features_svc.GIFT_CARDS))],
)
def issue_gift_card(
    body: GiftCardIssueBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """發行禮物卡;明碼卡號只在本回應出現一次(created=false 為冪等重放)。"""
    from saas_mvp.services import gift_cards as gift_cards_svc

    _require_owner(actor)
    try:
        if not body.compliance_ack:
            raise gift_cards_svc.GiftCardError(
                "請確認已核對履約保障與禮券法規資訊。"
            )
        result = gift_cards_svc.issue_card(
            db,
            tenant_id=actor.user.tenant_id,
            amount_cents=body.amount_twd * 100,
            fulfillment_guarantee=body.fulfillment_guarantee,
            issuance_key=body.issuance_key,
            issued_by_user_id=actor.user.id,
            recipient_customer_id=body.recipient_customer_id,
            purchaser_name=body.purchaser_name,
            recipient_name=body.recipient_name,
            message=body.message,
        )
        if result.created:
            audit_svc.record_from_actor(
                db,
                actor,
                action="gift_cards.issue",
                target=f"gift_card:{result.card.id}",
                detail={
                    "amount_cents": result.card.initial_value_cents,
                    "recipient_customer_id": result.card.recipient_customer_id,
                },
                request=request,
            )
        db.commit()
    except gift_cards_svc.GiftCardError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    balance = gift_cards_svc.GiftCardBalance(
        result.card,
        gift_cards_svc.balance_cents(
            db, tenant_id=actor.user.tenant_id, gift_card_id=result.card.id
        ),
    )
    return GiftCardIssued(
        card=_gift_card_row(balance), code=result.code, created=result.created
    )


@router.post(
    "/gift-cards/{gift_card_id}/void",
    response_model=GiftCardRow,
    dependencies=[Depends(features_svc.require_feature(features_svc.GIFT_CARDS))],
)
def void_gift_card(
    gift_card_id: int,
    body: GiftCardVoidBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """作廢禮物卡(餘額歸零沖銷;冪等——已作廢直接回現況)。"""
    from saas_mvp.services import gift_cards as gift_cards_svc

    _require_owner(actor)
    try:
        card = gift_cards_svc.void_card(
            db,
            tenant_id=actor.user.tenant_id,
            gift_card_id=gift_card_id,
            note=body.note,
            actor_user_id=actor.user.id,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="gift_cards.void",
            target=f"gift_card:{card.id}",
            request=request,
        )
        db.commit()
    except gift_cards_svc.GiftCardNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc))
    except gift_cards_svc.GiftCardError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    balance = gift_cards_svc.GiftCardBalance(
        card,
        gift_cards_svc.balance_cents(
            db, tenant_id=actor.user.tenant_id, gift_card_id=card.id
        ),
    )
    return _gift_card_row(balance)


# ────────────────────── 顧客表單／同意書(R7-C1)──────────────────────
# 與 /ui/client-forms 共用 services/client_forms;建立/加題/啟停限 owner。
# 公開填寫流(/forms/{token})不在本 API 範圍。


class ClientFormQuestionRow(BaseModel):
    id: int
    label: str
    field_type: str
    is_required: bool
    options: list[str]
    sort_order: int


class ClientFormTemplateRow(BaseModel):
    id: int
    name: str
    intro: str | None
    consent_text: str
    service_id: int | None
    require_signature: bool
    is_active: bool
    version: int
    questions: list[ClientFormQuestionRow]


class ClientFormCreateBody(BaseModel):
    name: str
    intro: str = ""
    consent_text: str
    service_id: int | None = None
    require_signature: bool = True


class ClientFormQuestionBody(BaseModel):
    label: str
    field_type: str
    required: bool = False
    options: str = ""


class ClientFormActiveBody(BaseModel):
    active: bool


def _client_form_template_row(db, tenant_id: int, template) -> ClientFormTemplateRow:
    from saas_mvp.services import client_forms as client_forms_svc

    rows = client_forms_svc.questions(
        db, tenant_id=tenant_id, template_id=template.id
    )
    import json as _json

    return ClientFormTemplateRow(
        id=template.id,
        name=template.name,
        intro=template.intro,
        consent_text=template.consent_text,
        service_id=template.service_id,
        require_signature=bool(template.require_signature),
        is_active=bool(template.is_active),
        version=template.version,
        questions=[
            ClientFormQuestionRow(
                id=q.id,
                label=q.label,
                field_type=q.field_type,
                is_required=bool(q.is_required),
                options=_json.loads(q.options_json) if q.options_json else [],
                sort_order=q.sort_order,
            )
            for q in rows
        ],
    )


@router.get(
    "/client-forms",
    response_model=list[ClientFormTemplateRow],
    dependencies=[Depends(features_svc.require_feature(features_svc.CLIENT_FORMS))],
)
def list_client_forms(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """表單範本列表(含題目)。"""
    from saas_mvp.services import client_forms as client_forms_svc

    templates = client_forms_svc.list_templates(db, tenant_id=current_user.tenant_id)
    response.headers["X-Total-Count"] = str(len(templates))
    return [
        _client_form_template_row(db, current_user.tenant_id, t) for t in templates
    ]


@router.post(
    "/client-forms",
    response_model=ClientFormTemplateRow,
    status_code=201,
    dependencies=[Depends(features_svc.require_feature(features_svc.CLIENT_FORMS))],
)
def create_client_form(
    body: ClientFormCreateBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import client_forms as client_forms_svc

    _require_owner(actor)
    try:
        row = client_forms_svc.create_template(
            db,
            tenant_id=actor.user.tenant_id,
            name=body.name,
            intro=body.intro,
            consent_text=body.consent_text,
            service_id=body.service_id,
            require_signature=body.require_signature,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="client_forms.create",
            target=f"form:{row.id}",
            request=request,
        )
        db.commit()
    except client_forms_svc.ClientFormError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    return _client_form_template_row(db, actor.user.tenant_id, row)


@router.post(
    "/client-forms/{template_id}/questions",
    response_model=ClientFormTemplateRow,
    status_code=201,
    dependencies=[Depends(features_svc.require_feature(features_svc.CLIENT_FORMS))],
)
def add_client_form_question(
    template_id: int,
    body: ClientFormQuestionBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import client_forms as client_forms_svc

    _require_owner(actor)
    try:
        client_forms_svc.add_question(
            db,
            tenant_id=actor.user.tenant_id,
            template_id=template_id,
            label=body.label,
            field_type=body.field_type,
            required=body.required,
            options=body.options,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="client_forms.question.add",
            target=f"form:{template_id}",
            request=request,
        )
        db.commit()
    except client_forms_svc.ClientFormNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc))
    except client_forms_svc.ClientFormError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    template = next(
        t
        for t in client_forms_svc.list_templates(db, tenant_id=actor.user.tenant_id)
        if t.id == template_id
    )
    return _client_form_template_row(db, actor.user.tenant_id, template)


@router.post(
    "/client-forms/{template_id}/active",
    response_model=ClientFormTemplateRow,
    dependencies=[Depends(features_svc.require_feature(features_svc.CLIENT_FORMS))],
)
def set_client_form_active(
    template_id: int,
    body: ClientFormActiveBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import client_forms as client_forms_svc

    _require_owner(actor)
    try:
        row = client_forms_svc.set_active(
            db,
            tenant_id=actor.user.tenant_id,
            template_id=template_id,
            active=body.active,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="client_forms.active",
            target=f"form:{row.id}",
            detail={"active": row.is_active},
            request=request,
        )
        db.commit()
    except client_forms_svc.ClientFormNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc))
    except client_forms_svc.ClientFormError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    return _client_form_template_row(db, actor.user.tenant_id, row)


# ─────────────────────────── 服務套票(R7-C2)───────────────────────────
# 與 /ui/packages 共用 services/service_packages;定義 CRUD 限 owner。
# 顧客層(發放/錢包/取消)已在 customer-detail 流程,不在本 API 範圍。


class PackageItemRow(BaseModel):
    service_id: int
    service_name: str
    included_quantity: int


class PackageRow(BaseModel):
    id: int
    name: str
    description: str | None
    price_cents: int
    validity_days: int
    is_active: bool
    items: list[PackageItemRow]


class PackageCreateBody(BaseModel):
    name: str
    description: str = ""
    price_twd: int
    validity_days: int


class PackageItemBody(BaseModel):
    service_id: int
    included_quantity: int


class PackageActiveBody(BaseModel):
    active: bool


def _package_row(db, tenant_id: int, package) -> PackageRow:
    from saas_mvp.models.service import Service
    from saas_mvp.services import service_packages as packages_svc

    items = packages_svc.package_items(
        db, tenant_id=tenant_id, package_id=package.id
    )
    names = {
        s.id: s.name
        for s in db.query(Service).filter(Service.tenant_id == tenant_id).all()
    }
    return PackageRow(
        id=package.id,
        name=package.name,
        description=package.description,
        price_cents=package.price_cents,
        validity_days=package.validity_days,
        is_active=bool(package.is_active),
        items=[
            PackageItemRow(
                service_id=i.service_id,
                service_name=names.get(i.service_id, f"#{i.service_id}"),
                included_quantity=i.included_quantity,
            )
            for i in items
        ],
    )


@router.get(
    "/packages",
    response_model=list[PackageRow],
    dependencies=[
        Depends(features_svc.require_feature(features_svc.SERVICE_PACKAGES))
    ],
)
def list_service_packages(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """套票定義列表(含服務組成)。"""
    from saas_mvp.services import service_packages as packages_svc

    rows = packages_svc.list_packages(db, tenant_id=current_user.tenant_id)
    response.headers["X-Total-Count"] = str(len(rows))
    return [_package_row(db, current_user.tenant_id, p) for p in rows]


@router.post(
    "/packages",
    response_model=PackageRow,
    status_code=201,
    dependencies=[
        Depends(features_svc.require_feature(features_svc.SERVICE_PACKAGES))
    ],
)
def create_service_package(
    body: PackageCreateBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import service_packages as packages_svc

    _require_owner(actor)
    try:
        row = packages_svc.create_package(
            db,
            tenant_id=actor.user.tenant_id,
            name=body.name,
            description=body.description,
            price_cents=body.price_twd * 100,
            validity_days=body.validity_days,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="packages.create",
            target=f"package:{row.id}",
            detail={
                "price_cents": row.price_cents,
                "validity_days": row.validity_days,
            },
            request=request,
        )
        db.commit()
    except packages_svc.ServicePackageError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    return _package_row(db, actor.user.tenant_id, row)


@router.post(
    "/packages/{package_id}/items",
    response_model=PackageRow,
    dependencies=[
        Depends(features_svc.require_feature(features_svc.SERVICE_PACKAGES))
    ],
)
def upsert_service_package_item(
    package_id: int,
    body: PackageItemBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """新增或更新套票內某服務的次數(upsert)。"""
    from saas_mvp.services import service_packages as packages_svc

    _require_owner(actor)
    try:
        packages_svc.add_or_update_item(
            db,
            tenant_id=actor.user.tenant_id,
            package_id=package_id,
            service_id=body.service_id,
            included_quantity=body.included_quantity,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="packages.item.update",
            target=f"package:{package_id}",
            detail={
                "service_id": body.service_id,
                "quantity": body.included_quantity,
            },
            request=request,
        )
        db.commit()
    except packages_svc.PackageNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc))
    except packages_svc.ServicePackageError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    package = next(
        p
        for p in packages_svc.list_packages(db, tenant_id=actor.user.tenant_id)
        if p.id == package_id
    )
    return _package_row(db, actor.user.tenant_id, package)


@router.post(
    "/packages/{package_id}/active",
    response_model=PackageRow,
    dependencies=[
        Depends(features_svc.require_feature(features_svc.SERVICE_PACKAGES))
    ],
)
def set_service_package_active(
    package_id: int,
    body: PackageActiveBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import service_packages as packages_svc

    _require_owner(actor)
    try:
        row = packages_svc.set_active(
            db,
            tenant_id=actor.user.tenant_id,
            package_id=package_id,
            active=body.active,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="packages.active",
            target=f"package:{package_id}",
            detail={"active": body.active},
            request=request,
        )
        db.commit()
    except packages_svc.PackageNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc))
    except packages_svc.ServicePackageError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    return _package_row(db, actor.user.tenant_id, row)
