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
from pydantic import BaseModel, Field
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
from saas_mvp.services.mailer import get_mailer as _get_mailer_dep
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


# ────────────────────── 房間／設備資源(R7-C3)──────────────────────
# 與 /ui/resources 共用 services/bookable_resources;五子實體
# (類型/資源/服務需求/每週可用/停用區間),mutation 限 owner。
# 分配引擎(allocate/reallocate)由預約流程觸發,不在本 API 範圍。

_RESOURCES_FEATURE = [
    Depends(features_svc.require_feature(features_svc.BOOKABLE_RESOURCES))
]


class ResourceTypeRow(BaseModel):
    id: int
    name: str
    description: str | None
    is_active: bool


class ResourceWindowRow(BaseModel):
    id: int
    weekday: int
    start_time: datetime.time
    end_time: datetime.time


class ResourceBlockRow(BaseModel):
    id: int
    starts_at: datetime.datetime
    ends_at: datetime.datetime
    reason: str | None


class BookableResourceRow(BaseModel):
    id: int
    resource_type_id: int
    location_id: int | None
    name: str
    description: str | None
    internal_code: str | None
    capacity: int
    is_active: bool
    available_from: datetime.date | None
    available_until: datetime.date | None
    windows: list[ResourceWindowRow]
    blocks: list[ResourceBlockRow]


class ResourceRequirementRow(BaseModel):
    id: int
    service_id: int
    service_name: str
    resource_type_id: int
    type_name: str
    quantity: int


class NameRow(BaseModel):
    id: int
    name: str


class ResourcesOverview(BaseModel):
    types: list[ResourceTypeRow]
    resources: list[BookableResourceRow]
    requirements: list[ResourceRequirementRow]
    services: list[NameRow]
    locations: list[NameRow]


class ResourceTypeBody(BaseModel):
    name: str
    description: str = ""


class ResourceBody(BaseModel):
    resource_type_id: int
    name: str
    description: str = ""
    internal_code: str = ""
    capacity: int = 1
    location_id: int | None = None
    available_from: datetime.date | None = None
    available_until: datetime.date | None = None


class ResourceUpdateBody(BaseModel):
    name: str
    description: str = ""
    internal_code: str = ""
    capacity: int = 1
    location_id: int | None = None
    available_from: datetime.date | None = None
    available_until: datetime.date | None = None


class ActiveBody(BaseModel):
    active: bool


class RequirementBody(BaseModel):
    service_id: int
    resource_type_id: int
    quantity: int = 1


class AvailabilityBody(BaseModel):
    weekday: int
    start_time: datetime.time
    end_time: datetime.time


class BlockBody(BaseModel):
    starts_at: datetime.datetime
    ends_at: datetime.datetime
    reason: str = ""


def _resources_overview(db, tenant_id: int) -> ResourcesOverview:
    from saas_mvp.models.location import Location
    from saas_mvp.models.service import Service
    from saas_mvp.services import bookable_resources as resources_svc

    types = resources_svc.list_types(db, tenant_id=tenant_id)
    resources = resources_svc.list_resources(db, tenant_id=tenant_id)
    windows_by_resource: dict[int, list] = {}
    for w in resources_svc.list_availability(db, tenant_id=tenant_id):
        windows_by_resource.setdefault(w.resource_id, []).append(w)
    blocks_by_resource: dict[int, list] = {}
    for b in resources_svc.list_blocks(db, tenant_id=tenant_id):
        blocks_by_resource.setdefault(b.resource_id, []).append(b)
    services = (
        db.query(Service)
        .filter(Service.tenant_id == tenant_id)
        .order_by(Service.id)
        .all()
    )
    locations = (
        db.query(Location)
        .filter(Location.tenant_id == tenant_id)
        .order_by(Location.id)
        .all()
    )
    service_names = {s.id: s.name for s in services}
    type_names = {t.id: t.name for t in types}
    return ResourcesOverview(
        types=[
            ResourceTypeRow(
                id=t.id,
                name=t.name,
                description=t.description,
                is_active=bool(t.is_active),
            )
            for t in types
        ],
        resources=[
            BookableResourceRow(
                id=r.id,
                resource_type_id=r.resource_type_id,
                location_id=r.location_id,
                name=r.name,
                description=r.description,
                internal_code=r.internal_code,
                capacity=r.capacity,
                is_active=bool(r.is_active),
                available_from=r.available_from,
                available_until=r.available_until,
                windows=[
                    ResourceWindowRow(
                        id=w.id,
                        weekday=w.weekday,
                        start_time=w.start_time,
                        end_time=w.end_time,
                    )
                    for w in windows_by_resource.get(r.id, [])
                ],
                blocks=[
                    ResourceBlockRow(
                        id=b.id,
                        starts_at=b.starts_at,
                        ends_at=b.ends_at,
                        reason=b.reason,
                    )
                    for b in blocks_by_resource.get(r.id, [])
                ],
            )
            for r in resources
        ],
        requirements=[
            ResourceRequirementRow(
                id=req.id,
                service_id=req.service_id,
                service_name=service_names.get(req.service_id, f"#{req.service_id}"),
                resource_type_id=req.resource_type_id,
                type_name=type_names.get(
                    req.resource_type_id, f"#{req.resource_type_id}"
                ),
                quantity=req.quantity,
            )
            for req in resources_svc.list_requirements(db, tenant_id=tenant_id)
        ],
        services=[NameRow(id=s.id, name=s.name) for s in services],
        locations=[NameRow(id=loc.id, name=loc.name) for loc in locations],
    )


def _resources_mutation(db, actor, request, action: str, target: str, fn):
    """共用外殼:owner 檢查 → service 呼叫 → audit → commit → 404/422 映射。"""
    from saas_mvp.services import bookable_resources as resources_svc

    _require_owner(actor)
    try:
        result = fn()
        audit_svc.record_from_actor(
            db, actor, action=action, target=target, request=request
        )
        db.commit()
        return result
    except resources_svc.ResourceNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc))
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))


@router.get(
    "/resources/overview",
    response_model=ResourcesOverview,
    dependencies=_RESOURCES_FEATURE,
)
def resources_overview(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """單次聚合:類型+資源(含每週可用/停用區間)+服務需求+表單選項。"""
    return _resources_overview(db, current_user.tenant_id)


@router.post(
    "/resources/types",
    response_model=ResourceTypeRow,
    status_code=201,
    dependencies=_RESOURCES_FEATURE,
)
def create_resource_type(
    body: ResourceTypeBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import bookable_resources as resources_svc

    row = _resources_mutation(
        db, actor, request, "resources.type.create", "resource_type:new",
        lambda: resources_svc.create_type(
            db,
            tenant_id=actor.user.tenant_id,
            name=body.name,
            description=body.description,
        ),
    )
    return ResourceTypeRow(
        id=row.id, name=row.name, description=row.description,
        is_active=bool(row.is_active),
    )


@router.post(
    "/resources/types/{resource_type_id}/active",
    response_model=ResourceTypeRow,
    dependencies=_RESOURCES_FEATURE,
)
def set_resource_type_active(
    resource_type_id: int,
    body: ActiveBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import bookable_resources as resources_svc

    row = _resources_mutation(
        db, actor, request,
        "resources.type.active", f"resource_type:{resource_type_id}",
        lambda: resources_svc.set_type_active(
            db,
            tenant_id=actor.user.tenant_id,
            resource_type_id=resource_type_id,
            active=body.active,
        ),
    )
    return ResourceTypeRow(
        id=row.id, name=row.name, description=row.description,
        is_active=bool(row.is_active),
    )


@router.post(
    "/resources/requirements",
    response_model=ResourcesOverview,
    dependencies=_RESOURCES_FEATURE,
)
def set_resource_requirement(
    body: RequirementBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """設定某服務對某資源類型的需求(upsert)。"""
    from saas_mvp.services import bookable_resources as resources_svc

    _resources_mutation(
        db, actor, request, "resources.requirement.set", "requirement:set",
        lambda: resources_svc.set_requirement(
            db,
            tenant_id=actor.user.tenant_id,
            service_id=body.service_id,
            resource_type_id=body.resource_type_id,
            quantity=body.quantity,
        ),
    )
    return _resources_overview(db, actor.user.tenant_id)


@router.delete(
    "/resources/requirements/{requirement_id}",
    response_model=ResourcesOverview,
    dependencies=_RESOURCES_FEATURE,
)
def delete_resource_requirement(
    requirement_id: int,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import bookable_resources as resources_svc

    _resources_mutation(
        db, actor, request,
        "resources.requirement.delete", f"requirement:{requirement_id}",
        lambda: resources_svc.remove_requirement(
            db, tenant_id=actor.user.tenant_id, requirement_id=requirement_id
        ),
    )
    return _resources_overview(db, actor.user.tenant_id)


@router.delete(
    "/resources/availability/{availability_id}",
    response_model=ResourcesOverview,
    dependencies=_RESOURCES_FEATURE,
)
def delete_resource_availability(
    availability_id: int,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import bookable_resources as resources_svc

    _resources_mutation(
        db, actor, request,
        "resources.availability.delete", f"availability:{availability_id}",
        lambda: resources_svc.remove_availability(
            db, tenant_id=actor.user.tenant_id, availability_id=availability_id
        ),
    )
    return _resources_overview(db, actor.user.tenant_id)


@router.delete(
    "/resources/blocks/{block_id}",
    response_model=ResourcesOverview,
    dependencies=_RESOURCES_FEATURE,
)
def delete_resource_block(
    block_id: int,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import bookable_resources as resources_svc

    _resources_mutation(
        db, actor, request, "resources.block.delete", f"block:{block_id}",
        lambda: resources_svc.remove_block(
            db, tenant_id=actor.user.tenant_id, block_id=block_id
        ),
    )
    return _resources_overview(db, actor.user.tenant_id)


@router.post(
    "/resources",
    response_model=ResourcesOverview,
    status_code=201,
    dependencies=_RESOURCES_FEATURE,
)
def create_bookable_resource(
    body: ResourceBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import bookable_resources as resources_svc

    _resources_mutation(
        db, actor, request, "resources.create", "resource:new",
        lambda: resources_svc.create_resource(
            db,
            tenant_id=actor.user.tenant_id,
            resource_type_id=body.resource_type_id,
            name=body.name,
            description=body.description,
            internal_code=body.internal_code,
            capacity=body.capacity,
            location_id=body.location_id,
            available_from=body.available_from,
            available_until=body.available_until,
        ),
    )
    return _resources_overview(db, actor.user.tenant_id)


@router.patch(
    "/resources/{resource_id}",
    response_model=ResourcesOverview,
    dependencies=_RESOURCES_FEATURE,
)
def update_bookable_resource(
    resource_id: int,
    body: ResourceUpdateBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import bookable_resources as resources_svc

    _resources_mutation(
        db, actor, request, "resources.update", f"resource:{resource_id}",
        lambda: resources_svc.update_resource(
            db,
            tenant_id=actor.user.tenant_id,
            resource_id=resource_id,
            name=body.name,
            description=body.description,
            internal_code=body.internal_code,
            capacity=body.capacity,
            location_id=body.location_id,
            available_from=body.available_from,
            available_until=body.available_until,
        ),
    )
    return _resources_overview(db, actor.user.tenant_id)


@router.post(
    "/resources/{resource_id}/active",
    response_model=ResourcesOverview,
    dependencies=_RESOURCES_FEATURE,
)
def set_bookable_resource_active(
    resource_id: int,
    body: ActiveBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import bookable_resources as resources_svc

    _resources_mutation(
        db, actor, request, "resources.active", f"resource:{resource_id}",
        lambda: resources_svc.set_resource_active(
            db,
            tenant_id=actor.user.tenant_id,
            resource_id=resource_id,
            active=body.active,
        ),
    )
    return _resources_overview(db, actor.user.tenant_id)


@router.post(
    "/resources/{resource_id}/availability",
    response_model=ResourcesOverview,
    status_code=201,
    dependencies=_RESOURCES_FEATURE,
)
def add_resource_availability(
    resource_id: int,
    body: AvailabilityBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import bookable_resources as resources_svc

    _resources_mutation(
        db, actor, request,
        "resources.availability.add", f"resource:{resource_id}",
        lambda: resources_svc.add_availability(
            db,
            tenant_id=actor.user.tenant_id,
            resource_id=resource_id,
            weekday=body.weekday,
            start_time=body.start_time,
            end_time=body.end_time,
        ),
    )
    return _resources_overview(db, actor.user.tenant_id)


@router.post(
    "/resources/{resource_id}/blocks",
    response_model=ResourcesOverview,
    status_code=201,
    dependencies=_RESOURCES_FEATURE,
)
def add_resource_block(
    resource_id: int,
    body: BlockBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import bookable_resources as resources_svc

    _resources_mutation(
        db, actor, request, "resources.block.add", f"resource:{resource_id}",
        lambda: resources_svc.add_block(
            db,
            tenant_id=actor.user.tenant_id,
            resource_id=resource_id,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            reason=body.reason,
        ),
    )
    return _resources_overview(db, actor.user.tenant_id)


# ──────────────────── 員工抽成／薪資結算(R7-C4)────────────────────
# 與 /ui/commissions 共用 services/commissions;金錢面全部限 owner
# (含唯讀,比照 /ui 頁本身就掛 require_ui_owner)。狀態機
# draft→finalized→paid 由 service 把關;金額輸入沿 /ui 慣例收字串
# (percent → 基點整數,金額 → cents),Decimal 轉換錯誤=422。

_COMMISSIONS_FEATURE = [
    Depends(features_svc.require_feature(features_svc.STAFF_COMMISSIONS))
]


class CommissionTierRow(BaseModel):
    threshold_cents: int
    value: int


class CommissionRuleRow(BaseModel):
    id: int
    staff_id: int
    item_type: str
    method: str
    structure: str
    value: int | None
    calculation_basis: str
    sales_period: str | None
    effective_from: datetime.date
    tiers: list[CommissionTierRow]


class GoalProgressRow(BaseModel):
    goal_id: int
    staff_id: int
    item_type: str
    target_cents: int
    sales_period: str
    actual_cents: int
    percent: float
    period_start: datetime.date
    period_end: datetime.date


class PayRunRow(BaseModel):
    id: int
    period_start: datetime.date
    period_end: datetime.date
    status: str
    total_cents: int
    created_at: datetime.datetime
    finalized_at: datetime.datetime | None
    paid_at: datetime.datetime | None


class PayRunItemRow(BaseModel):
    staff_id: int
    staff_name: str
    commission_cents: int
    tip_cents: int
    adjustment_cents: int
    adjustment_note: str | None
    total_cents: int


class PayRunDetail(BaseModel):
    run: PayRunRow
    items: list[PayRunItemRow]


class EarningRow(BaseModel):
    id: int
    staff_id: int
    item_type: str
    item_name_snapshot: str
    gross_cents: int
    net_cents: int
    commission_cents: int
    earned_at: datetime.datetime
    pay_run_id: int | None
    reversed: bool


class CommissionsOverview(BaseModel):
    staff: list[NameRow]
    rules: list[CommissionRuleRow]
    goals: list[GoalProgressRow]
    pay_runs: list[PayRunRow]
    recent_earnings: list[EarningRow]


class RuleBody(BaseModel):
    staff_id: int
    item_type: str
    method: str
    value: str
    calculation_basis: str = "net"
    effective_from: datetime.date


class TierBody(BaseModel):
    threshold_twd: str
    value: str


class TieredRuleBody(BaseModel):
    staff_id: int
    item_type: str
    method: str
    tiers: list[TierBody]
    calculation_basis: str = "net"
    sales_period: str = "monthly"
    effective_from: datetime.date


class GoalBody(BaseModel):
    staff_id: int
    item_type: str = "all"
    target_twd: str
    sales_period: str = "monthly"
    effective_from: datetime.date


class PayRunCreateBody(BaseModel):
    period_start: datetime.date
    period_end: datetime.date


class PayRunAdjustBody(BaseModel):
    staff_id: int
    adjustment_twd: str = "0"
    note: str = ""


def _money_cents(raw: str, *, allow_negative: bool = False) -> int:
    """沿 /ui 慣例:字串金額 → cents;格式錯誤丟 CommissionError。

    quantize 也包進 try:超過 Decimal context 精度(28 位)的巨大輸入
    會在 quantize 時拋 InvalidOperation,須映射 422 而非 500。
    """
    from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

    from saas_mvp.services import commissions as commissions_svc

    try:
        value = Decimal(raw.strip())
        if not value.is_finite():
            raise InvalidOperation
        if not allow_negative and value < 0:
            raise commissions_svc.CommissionError("金額不可為負數。")
        return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, AttributeError):
        raise commissions_svc.CommissionError("金額格式不正確。") from None


def _rule_value_units(method: str, raw: str) -> int:
    """percent → 基點整數(3.5% = 350);fixed → cents。"""
    from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

    from saas_mvp.services import commissions as commissions_svc

    if method == "percent":
        try:
            value = Decimal(raw.strip())
            if not value.is_finite():
                raise InvalidOperation
            return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except (InvalidOperation, AttributeError):
            raise commissions_svc.CommissionError("抽成數值格式不正確。") from None
    return _money_cents(raw)


def _pay_run_row(run) -> PayRunRow:
    return PayRunRow(
        id=run.id,
        period_start=run.period_start,
        period_end=run.period_end,
        status=run.status,
        total_cents=run.total_cents,
        created_at=run.created_at,
        finalized_at=run.finalized_at,
        paid_at=run.paid_at,
    )


def _commission_mutation(db, actor, request, action: str, target, fn, *, conflict=False):
    """owner 檢查 → service → audit → commit;CommissionError → 422/409。"""
    from saas_mvp.services import commissions as commissions_svc

    _require_owner(actor)
    try:
        result = fn()
        audit_svc.record_from_actor(
            db,
            actor,
            action=action,
            target=target(result) if callable(target) else target,
            request=request,
        )
        db.commit()
        return result
    except commissions_svc.CommissionError as exc:
        db.rollback()
        raise HTTPException(status_code=409 if conflict else 422, detail=str(exc))


@router.get(
    "/commissions/overview",
    response_model=CommissionsOverview,
    dependencies=_COMMISSIONS_FEATURE,
)
def commissions_overview(
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """抽成總覽:員工/現行規則(含級距)/目標進度/結算單/近期抽成明細。"""
    from saas_mvp.services import commissions as commissions_svc
    from saas_mvp.services import staff as staff_svc

    _require_owner(actor)
    tid = actor.user.tenant_id
    staff = staff_svc.list_staff(db, tenant_id=tid)
    rules = commissions_svc.latest_rules(db, tenant_id=tid)
    # UTC 記帳日(鏡射 /ui;目標期間邊界以 UTC 落庫的 paid_at 比對)
    today = datetime.datetime.now(datetime.timezone.utc).date()
    return CommissionsOverview(
        staff=[NameRow(id=s.id, name=s.name) for s in staff],
        rules=[
            CommissionRuleRow(
                id=rule.id,
                staff_id=rule.staff_id,
                item_type=rule.item_type,
                method=rule.method,
                structure=rule.structure,
                value=rule.value,
                calculation_basis=rule.calculation_basis,
                sales_period=rule.sales_period,
                effective_from=rule.effective_from,
                tiers=[
                    CommissionTierRow(
                        threshold_cents=t.threshold_cents, value=t.value
                    )
                    for t in (
                        commissions_svc.rule_tiers(
                            db, tenant_id=tid, rule_id=rule.id
                        )
                        if rule.structure == "tiered"
                        else []
                    )
                ],
            )
            for rule in rules.values()
        ],
        goals=[
            GoalProgressRow(
                goal_id=row["goal"].id,
                staff_id=row["goal"].staff_id,
                item_type=row["goal"].item_type,
                target_cents=row["goal"].target_cents,
                sales_period=row["goal"].sales_period,
                actual_cents=row["actual_cents"],
                percent=row["percent"],
                period_start=row["period_start"],
                period_end=row["period_end"],
            )
            for row in commissions_svc.sales_goal_progress(
                db, tenant_id=tid, on_date=today
            )
        ],
        pay_runs=[
            _pay_run_row(r)
            for r in commissions_svc.list_pay_runs(db, tenant_id=tid)
        ],
        recent_earnings=[
            EarningRow(
                id=e.id,
                staff_id=e.staff_id,
                item_type=e.item_type,
                item_name_snapshot=e.item_name_snapshot,
                gross_cents=e.gross_cents,
                net_cents=e.net_cents,
                commission_cents=e.commission_cents,
                earned_at=e.earned_at,
                pay_run_id=e.pay_run_id,
                reversed=e.reversed_at is not None,
            )
            for e in commissions_svc.recent_earnings(db, tenant_id=tid)
        ],
    )


@router.post(
    "/commissions/rules",
    status_code=201,
    dependencies=_COMMISSIONS_FEATURE,
)
def create_commission_rule(
    body: RuleBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import commissions as commissions_svc

    row = _commission_mutation(
        db, actor, request, "commissions.rule.create",
        lambda r: f"commission_rule:{r.id}",
        lambda: commissions_svc.save_rule(
            db,
            tenant_id=actor.user.tenant_id,
            staff_id=body.staff_id,
            item_type=body.item_type,
            method=body.method,
            value=_rule_value_units(body.method, body.value),
            calculation_basis=body.calculation_basis,
            effective_from=body.effective_from,
            actor_user_id=actor.user.id,
        ),
    )
    return {"id": row.id}


@router.post(
    "/commissions/tiered-rules",
    status_code=201,
    dependencies=_COMMISSIONS_FEATURE,
)
def create_commission_tiered_rule(
    body: TieredRuleBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import commissions as commissions_svc

    def _build():
        tiers = [
            (
                _money_cents(t.threshold_twd),
                _rule_value_units(body.method, t.value),
            )
            for t in body.tiers
        ]
        return commissions_svc.save_tiered_rule(
            db,
            tenant_id=actor.user.tenant_id,
            staff_id=body.staff_id,
            item_type=body.item_type,
            method=body.method,
            tiers=tiers,
            calculation_basis=body.calculation_basis,
            sales_period=body.sales_period,
            effective_from=body.effective_from,
            actor_user_id=actor.user.id,
        )

    row = _commission_mutation(
        db, actor, request, "commissions.tiered_rule.create",
        lambda r: f"commission_rule:{r.id}", _build,
    )
    return {"id": row.id}


@router.post(
    "/commissions/goals",
    status_code=201,
    dependencies=_COMMISSIONS_FEATURE,
)
def create_commission_goal(
    body: GoalBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import commissions as commissions_svc

    row = _commission_mutation(
        db, actor, request, "commissions.goal.create",
        lambda r: f"staff_sales_goal:{r.id}",
        lambda: commissions_svc.save_sales_goal(
            db,
            tenant_id=actor.user.tenant_id,
            staff_id=body.staff_id,
            item_type=body.item_type,
            target_cents=_money_cents(body.target_twd),
            sales_period=body.sales_period,
            effective_from=body.effective_from,
            actor_user_id=actor.user.id,
        ),
    )
    return {"id": row.id}


def _pay_run_detail(db, tenant_id: int, pay_run_id: int) -> PayRunDetail:
    from saas_mvp.services import commissions as commissions_svc

    run, items = commissions_svc.pay_run_export_data(
        db, tenant_id=tenant_id, pay_run_id=pay_run_id
    )
    return PayRunDetail(
        run=_pay_run_row(run),
        items=[
            PayRunItemRow(
                staff_id=item.staff_id,
                staff_name=staff.name if staff else f"員工 #{item.staff_id}",
                commission_cents=item.commission_cents,
                tip_cents=item.tip_cents,
                adjustment_cents=item.adjustment_cents,
                adjustment_note=item.adjustment_note,
                total_cents=item.total_cents,
            )
            for item, staff in items
        ],
    )


@router.get(
    "/commissions/pay-runs/{pay_run_id}",
    response_model=PayRunDetail,
    dependencies=_COMMISSIONS_FEATURE,
)
def get_commission_pay_run(
    pay_run_id: int,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import commissions as commissions_svc

    _require_owner(actor)
    try:
        return _pay_run_detail(db, actor.user.tenant_id, pay_run_id)
    except commissions_svc.CommissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post(
    "/commissions/pay-runs",
    response_model=PayRunDetail,
    status_code=201,
    dependencies=_COMMISSIONS_FEATURE,
)
def create_commission_pay_run(
    body: PayRunCreateBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import commissions as commissions_svc

    run = _commission_mutation(
        db, actor, request, "commissions.pay_run.create",
        lambda r: f"pay_run:{r.id}",
        lambda: commissions_svc.create_pay_run(
            db,
            tenant_id=actor.user.tenant_id,
            period_start=body.period_start,
            period_end=body.period_end,
            actor_user_id=actor.user.id,
        ),
    )
    return _pay_run_detail(db, actor.user.tenant_id, run.id)


@router.post(
    "/commissions/pay-runs/{pay_run_id}/adjust",
    response_model=PayRunDetail,
    dependencies=_COMMISSIONS_FEATURE,
)
def adjust_commission_pay_run(
    pay_run_id: int,
    body: PayRunAdjustBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import commissions as commissions_svc

    _require_owner(actor)
    # 先分類錯誤面:找不到 → 404、非草稿 → 409(與轉移端點一致),
    # 其後 service 內的同名守衛仍把關實際寫入(雙保險)。
    try:
        run = commissions_svc.get_pay_run(
            db, tenant_id=actor.user.tenant_id, pay_run_id=pay_run_id
        )
    except commissions_svc.CommissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if run.status != "draft":
        raise HTTPException(status_code=409, detail="只有草稿結算單可以調整。")
    _commission_mutation(
        db, actor, request, "commissions.pay_run.adjust",
        f"pay_run:{pay_run_id}",
        lambda: commissions_svc.update_adjustment(
            db,
            tenant_id=actor.user.tenant_id,
            pay_run_id=pay_run_id,
            staff_id=body.staff_id,
            adjustment_cents=_money_cents(body.adjustment_twd, allow_negative=True),
            note=body.note,
        ),
    )
    return _pay_run_detail(db, actor.user.tenant_id, pay_run_id)


def _pay_run_transition_api(db, actor, request, pay_run_id: int, action: str):
    """finalize/paid/delete 三轉移;非法轉移 → 409(比照 /ui)。"""
    from saas_mvp.services import commissions as commissions_svc

    def _run():
        if action == "finalize":
            return commissions_svc.finalize_pay_run(
                db,
                tenant_id=actor.user.tenant_id,
                pay_run_id=pay_run_id,
                actor_user_id=actor.user.id,
            )
        if action == "paid":
            return commissions_svc.mark_pay_run_paid(
                db,
                tenant_id=actor.user.tenant_id,
                pay_run_id=pay_run_id,
                actor_user_id=actor.user.id,
            )
        return commissions_svc.delete_draft(
            db, tenant_id=actor.user.tenant_id, pay_run_id=pay_run_id
        )

    _commission_mutation(
        db, actor, request, f"commissions.pay_run.{action}",
        f"pay_run:{pay_run_id}", _run, conflict=True,
    )


@router.post(
    "/commissions/pay-runs/{pay_run_id}/finalize",
    response_model=PayRunDetail,
    dependencies=_COMMISSIONS_FEATURE,
)
def finalize_commission_pay_run(
    pay_run_id: int,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    _pay_run_transition_api(db, actor, request, pay_run_id, "finalize")
    return _pay_run_detail(db, actor.user.tenant_id, pay_run_id)


@router.post(
    "/commissions/pay-runs/{pay_run_id}/paid",
    response_model=PayRunDetail,
    dependencies=_COMMISSIONS_FEATURE,
)
def pay_commission_pay_run(
    pay_run_id: int,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    _pay_run_transition_api(db, actor, request, pay_run_id, "paid")
    return _pay_run_detail(db, actor.user.tenant_id, pay_run_id)


@router.post(
    "/commissions/pay-runs/{pay_run_id}/delete",
    dependencies=_COMMISSIONS_FEATURE,
)
def delete_commission_pay_run(
    pay_run_id: int,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """僅 draft 可刪(service 把關);釋放明細回未結算池。

    回 JSON 而非 204:console 的 postJson 一律 response.json(),且
    Next proxy 對 204 需特判;比照 R7-C3 delete 端點回有體回應。
    """
    _pay_run_transition_api(db, actor, request, pay_run_id, "delete")
    return {"ok": True, "deleted_pay_run_id": pay_run_id}


def _commission_csv(rows: list[list], filename: str) -> Response:
    import csv as _csv
    import io as _io

    def safe_cell(value):
        # 避免員工名稱/商品名稱被 Excel 當成公式執行。
        if isinstance(value, str) and value.startswith(
            ("=", "+", "-", "@", "\t", "\r")
        ):
            return "'" + value
        return value

    output = _io.StringIO(newline="")
    writer = _csv.writer(output)
    writer.writerows([[safe_cell(cell) for cell in row] for row in rows])
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/commissions/pay-runs/{pay_run_id}/export.csv",
    dependencies=_COMMISSIONS_FEATURE,
)
def export_commission_pay_run(
    pay_run_id: int,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import commissions as commissions_svc

    _require_owner(actor)
    try:
        run, data = commissions_svc.pay_run_export_data(
            db, tenant_id=actor.user.tenant_id, pay_run_id=pay_run_id
        )
    except commissions_svc.CommissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    rows: list[list] = [[
        "結算單", "期間開始", "期間結束", "狀態", "員工",
        "抽成", "小費", "加減項", "應付", "說明",
    ]]
    status_labels = {"draft": "草稿", "finalized": "已確認", "paid": "已付款"}
    for item, staff in data:
        rows.append([
            run.id,
            run.period_start.isoformat(),
            run.period_end.isoformat(),
            status_labels.get(run.status, run.status),
            staff.name if staff else f"員工 #{item.staff_id}",
            f"{item.commission_cents / 100:.2f}",
            f"{item.tip_cents / 100:.2f}",
            f"{item.adjustment_cents / 100:.2f}",
            f"{item.total_cents / 100:.2f}",
            item.adjustment_note or "",
        ])
    return _commission_csv(rows, f"pay-run-{run.id}.csv")


@router.get(
    "/commissions/activity.csv",
    dependencies=_COMMISSIONS_FEATURE,
)
def export_commission_activity(
    period_start: datetime.date = Query(...),
    period_end: datetime.date = Query(...),
    staff_id: int | None = Query(None),
    item_type: str | None = Query(None),
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import commissions as commissions_svc
    from saas_mvp.services import staff as staff_svc

    _require_owner(actor)
    try:
        earnings = commissions_svc.activity_export_data(
            db,
            tenant_id=actor.user.tenant_id,
            period_start=period_start,
            period_end=period_end,
            staff_id=staff_id,
            item_type=item_type,
        )
    except commissions_svc.CommissionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    staff_by_id = {
        row.id: row.name
        for row in staff_svc.list_staff(db, tenant_id=actor.user.tenant_id)
    }
    rows: list[list] = [[
        "成交時間", "員工", "類型", "項目", "原價",
        "淨額", "抽成／小費", "結算單", "沖銷狀態",
    ]]
    for earning in earnings:
        rows.append([
            earning.earned_at.isoformat(),
            staff_by_id.get(earning.staff_id, f"員工 #{earning.staff_id}"),
            earning.item_type,
            earning.item_name_snapshot,
            f"{earning.gross_cents / 100:.2f}",
            f"{earning.net_cents / 100:.2f}",
            f"{earning.commission_cents / 100:.2f}",
            earning.pay_run_id or "",
            "已沖銷" if earning.reversed_at else "",
        ])
    return _commission_csv(
        rows,
        f"commission-activity-{period_start.isoformat()}-{period_end.isoformat()}.csv",
    )


# ──────────────────── LINE 自動回覆規則(R8-1)────────────────────
# 與 /ui/auto-reply 共用 services/auto_reply;比照 /ui:任一租戶成員可管理
# (無 owner 限制、無 feature 閘門、無 audit)。service 自帶 commit 與
# HTTPException(404/422),端點薄轉即可。


class AutoReplyRuleRow(BaseModel):
    id: int
    keyword: str
    match_type: str
    reply_type: str
    reply_text: str | None
    flex_menu_id: int | None
    priority: int
    is_active: bool


class AutoReplyEnvelope(BaseModel):
    rules: list[AutoReplyRuleRow]
    flex_menus: list[NameRow]
    bot_mode: str


class AutoReplyRuleBody(BaseModel):
    keyword: str
    match_type: str = "contains"
    reply_type: str = "text"
    reply_text: str | None = None
    flex_menu_id: int | None = None
    priority: int = 0
    is_active: bool = True


def _auto_reply_rule_row(rule) -> AutoReplyRuleRow:
    return AutoReplyRuleRow(
        id=rule.id,
        keyword=rule.keyword,
        match_type=rule.match_type,
        reply_type=rule.reply_type,
        reply_text=rule.reply_text,
        flex_menu_id=rule.flex_menu_id,
        priority=rule.priority,
        is_active=bool(rule.is_active),
    )


@router.get("/auto-reply", response_model=AutoReplyEnvelope)
def list_auto_reply_rules(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """規則列表 + flex 選單下拉 + bot_mode(規則僅在 auto_reply 模式生效)。"""
    from fastapi import HTTPException as _HTTPException

    from saas_mvp.models.flex_menu import FlexMenu
    from saas_mvp.services import auto_reply as auto_reply_svc
    from saas_mvp.services import line_config as line_config_svc

    tid = current_user.tenant_id
    try:
        cfg = line_config_svc.get_line_config(db, tid)
        bot_mode = cfg.get("bot_mode", "translation")
    except _HTTPException as exc:
        if exc.status_code != 404:
            raise
        bot_mode = "translation"
    menus = (
        db.query(FlexMenu)
        .filter(FlexMenu.tenant_id == tid)
        .order_by(FlexMenu.id)
        .all()
    )
    return AutoReplyEnvelope(
        rules=[
            _auto_reply_rule_row(r)
            for r in auto_reply_svc.list_rules(db, tenant_id=tid)
        ],
        flex_menus=[NameRow(id=m.id, name=m.title or f"(未命名 #{m.id})") for m in menus],
        bot_mode=bot_mode,
    )


@router.post("/auto-reply", response_model=AutoReplyRuleRow, status_code=201)
def create_auto_reply_rule(
    body: AutoReplyRuleBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import auto_reply as auto_reply_svc

    rule = auto_reply_svc.create_rule(
        db,
        tenant_id=current_user.tenant_id,
        keyword=body.keyword,
        match_type=body.match_type,
        reply_type=body.reply_type,
        reply_text=body.reply_text,
        flex_menu_id=body.flex_menu_id,
        priority=body.priority,
        is_active=body.is_active,
    )
    return _auto_reply_rule_row(rule)


@router.put("/auto-reply/{rule_id}", response_model=AutoReplyRuleRow)
def update_auto_reply_rule(
    rule_id: int,
    body: AutoReplyRuleBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """整筆更新(console 表單總是送完整欄位;toggle 亦走此端點)。"""
    from saas_mvp.services import auto_reply as auto_reply_svc

    rule = auto_reply_svc.update_rule(
        db,
        tenant_id=current_user.tenant_id,
        rule_id=rule_id,
        keyword=body.keyword,
        match_type=body.match_type,
        reply_type=body.reply_type,
        reply_text=body.reply_text,
        flex_menu_id=body.flex_menu_id,
        priority=body.priority,
        is_active=body.is_active,
    )
    return _auto_reply_rule_row(rule)


@router.delete("/auto-reply/{rule_id}", response_model=AutoReplyEnvelope)
def delete_auto_reply_rule(
    rule_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import auto_reply as auto_reply_svc

    auto_reply_svc.delete_rule(db, tenant_id=current_user.tenant_id, rule_id=rule_id)
    return list_auto_reply_rules(current_user=current_user, db=db)


# ──────────────────────── 成員管理(R8-3)────────────────────────
# 與 /ui/members 共用 services/members;全程 owner 限定。service 自帶
# commit,route 只補 audit(第二次 commit,同 /ui 既有雙 commit 模式);
# MemberActionError(中文文案)→ 422。受邀者公開 join 流留在 /ui/join。


class MemberRow(BaseModel):
    id: int
    email: str
    role: str
    disabled: bool
    is_self: bool


class MemberRoleBody(BaseModel):
    role: str


class InviteResult(BaseModel):
    invite_url: str


def _member_rows(db, actor: Actor) -> list[MemberRow]:
    from saas_mvp.services import members as members_svc

    return [
        MemberRow(
            id=u.id,
            email=u.email,
            role=u.role or "owner",
            disabled=u.disabled_at is not None,
            is_self=u.id == actor.user.id,
        )
        for u in members_svc.list_members(db, actor.user.tenant_id)
    ]


@router.get("/members", response_model=list[MemberRow])
def list_tenant_members(
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    _require_owner(actor)
    return _member_rows(db, actor)


@router.post("/members/invite", response_model=InviteResult, status_code=201)
def invite_tenant_member(
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """產生一次性邀請連結(7 天效期;受邀者自設 email/密碼建為 staff)。"""
    import datetime as _dt

    from saas_mvp.config import settings
    from saas_mvp.models.email_token import (
        PURPOSE_INVITE,
        EmailToken,
        generate_token,
        hash_token,
    )

    _require_owner(actor)
    token = generate_token()
    db.add(
        EmailToken(
            user_id=actor.user.id,
            purpose=PURPOSE_INVITE,
            token_hash=hash_token(token),
            expires_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=7),
        )
    )
    db.commit()
    audit_svc.record_from_actor(
        db,
        actor,
        action="member.invite",
        target=f"tenant:{actor.user.tenant_id}",
        request=request,
    )
    db.commit()
    base = settings.public_base_url.rstrip("/") or ""
    return InviteResult(invite_url=f"{base}/ui/join/{token}")


def _member_mutation(db, actor, request, action: str, user_id: int, fn):
    from saas_mvp.services import members as members_svc

    _require_owner(actor)
    try:
        fn()
    except members_svc.MemberActionError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    audit_svc.record_from_actor(
        db, actor, action=action, target=f"user:{user_id}", request=request
    )
    db.commit()
    return _member_rows(db, actor)


@router.post("/members/{user_id}/role", response_model=list[MemberRow])
def set_tenant_member_role(
    user_id: int,
    body: MemberRoleBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import members as members_svc

    return _member_mutation(
        db, actor, request, "member.role", user_id,
        lambda: members_svc.set_role(db, actor.user, user_id, body.role),
    )


@router.post("/members/{user_id}/disable", response_model=list[MemberRow])
def disable_tenant_member(
    user_id: int,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """停用成員:token_version+1 立即撤銷其所有票與 API key。"""
    from saas_mvp.services import members as members_svc

    return _member_mutation(
        db, actor, request, "member.disable", user_id,
        lambda: members_svc.disable_member(db, actor.user, user_id),
    )


@router.post("/members/{user_id}/enable", response_model=list[MemberRow])
def enable_tenant_member(
    user_id: int,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import members as members_svc

    return _member_mutation(
        db, actor, request, "member.enable", user_id,
        lambda: members_svc.enable_member(db, actor.user, user_id),
    )


@router.delete("/members/{user_id}", response_model=list[MemberRow])
def remove_tenant_member(
    user_id: int,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """永久移除成員(audit 於 service 成功後記——email 刪後不可查)。"""
    from saas_mvp.services import members as members_svc

    return _member_mutation(
        db, actor, request, "member.remove", user_id,
        lambda: members_svc.remove_member(db, actor.user, user_id),
    )


# ──────────────────────── 帳號設定(R8-3)────────────────────────
# 與 /ui/account 共用 services/totp、members.logout_all_devices。
# ★改密碼/登出所有裝置會 token_version+1 撤銷含當下這顆票——端點回新
# access_token,由 Next session route(/api/session/*)重種 cookie 三顆,
# 操作者本裝置不掉線。OAuth 連結流程(authorize redirect)留 server 端。


class AccountSummary(BaseModel):
    email: str
    email_verified: bool
    last_login_at: datetime.datetime | None
    last_login_ip: str | None
    totp_enabled: bool
    remaining_recovery_codes: int
    oauth_provider: str | None
    line_login_configured: bool


class PasswordChangeBody(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class TokenResult(BaseModel):
    access_token: str


class TotpStartResult(BaseModel):
    qr_svg: str
    secret: str
    otpauth_uri: str


class OtpBody(BaseModel):
    otp: str


class RecoveryCodesResult(BaseModel):
    recovery_codes: list[str]


@router.get("/account", response_model=AccountSummary)
def account_summary(
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.config import settings
    from saas_mvp.services import oauth as oauth_svc
    from saas_mvp.services import totp as totp_svc

    user = actor.user
    return AccountSummary(
        email=user.email,
        email_verified=user.email_verified_at is not None,
        last_login_at=user.last_login_at,
        last_login_ip=user.last_login_ip,
        totp_enabled=bool(user.totp_enabled),
        remaining_recovery_codes=(
            totp_svc.remaining_recovery_codes(db, user) if user.totp_enabled else 0
        ),
        oauth_provider=user.oauth_provider,
        line_login_configured=oauth_svc.provider_credentials_present(
            "line", settings=settings, db=db
        ),
    )


@router.post("/account/password", response_model=TokenResult)
def account_change_password(
    body: PasswordChangeBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """變更密碼:token_version+1 撤銷所有既有票,回新票供本裝置續留。"""
    from saas_mvp.auth.security import (
        create_access_token,
        hash_password,
        verify_password,
    )

    user = db.get(User, actor.user.id)
    if not verify_password(body.current_password, user.hashed_password):
        raise HTTPException(status_code=422, detail="目前密碼不正確。")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="新密碼至少需 8 個字元。")
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=422, detail="兩次輸入的新密碼不一致。")
    if verify_password(body.new_password, user.hashed_password):
        raise HTTPException(status_code=422, detail="新密碼不可與目前密碼相同。")
    user.hashed_password = hash_password(body.new_password)
    user.token_version = (user.token_version or 0) + 1
    db.add(user)
    audit_svc.record_from_actor(
        db, actor, action="auth.password_change", request=request
    )
    db.commit()
    return TokenResult(
        access_token=create_access_token(
            user_id=user.id,
            tenant_id=user.tenant_id,
            token_version=user.token_version,
        )
    )


@router.post("/account/logout-all", response_model=TokenResult)
def account_logout_all(
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """登出所有裝置:token_version+1;回新票供本裝置續留。"""
    from saas_mvp.auth.security import create_access_token
    from saas_mvp.services import members as members_svc

    user = db.get(User, actor.user.id)
    members_svc.logout_all_devices(db, user)
    audit_svc.record_from_actor(db, actor, action="auth.logout_all", request=request)
    db.commit()
    return TokenResult(
        access_token=create_access_token(
            user_id=user.id,
            tenant_id=user.tenant_id,
            token_version=user.token_version,
        )
    )


@router.post("/account/totp/start", response_model=TotpStartResult)
def account_totp_start(
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """產生 TOTP secret(重呼覆蓋);QR 為伺服器端 inline SVG。"""
    from saas_mvp.services import totp as totp_svc

    if actor.user.totp_enabled:
        raise HTTPException(status_code=409, detail="兩步驟驗證已啟用。")
    secret = totp_svc.start_enrollment(db, actor.user)
    uri = totp_svc.provisioning_uri(actor.user, secret)
    return TotpStartResult(qr_svg=totp_svc.qr_svg(uri), secret=secret, otpauth_uri=uri)


@router.post("/account/totp/confirm", response_model=RecoveryCodesResult)
def account_totp_confirm(
    body: OtpBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """確認啟用;恢復碼明文僅此回應出現一次,前端必須立即顯示。"""
    from saas_mvp.services import totp as totp_svc

    if actor.user.totp_enabled:
        raise HTTPException(status_code=409, detail="兩步驟驗證已啟用。")
    if not actor.user.totp_secret_enc:
        raise HTTPException(status_code=422, detail="請先產生 QR code。")
    codes = totp_svc.confirm_enrollment(db, actor.user, body.otp)
    if codes is None:
        raise HTTPException(
            status_code=422, detail="驗證碼錯誤,請確認 App 時間同步後重試。"
        )
    audit_svc.record_from_actor(db, actor, action="auth.mfa.enable", request=request)
    db.commit()
    return RecoveryCodesResult(recovery_codes=codes)


@router.post("/account/totp/disable")
def account_totp_disable(
    body: OtpBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.auth.ratelimit import otp_limiter
    from saas_mvp.config import settings
    from saas_mvp.services import totp as totp_svc

    if not actor.user.totp_enabled:
        raise HTTPException(status_code=409, detail="兩步驟驗證未啟用。")
    if settings.rate_limit_enabled:
        otp_limiter._check_rate_limit(f"user:{actor.user.id}")  # 429 直接透傳
    if not totp_svc.disable(db, actor.user, body.otp):
        raise HTTPException(status_code=422, detail="驗證碼錯誤,未停用。")
    audit_svc.record_from_actor(db, actor, action="auth.mfa.disable", request=request)
    db.commit()
    return {"ok": True}


@router.post("/account/oauth/unlink")
def account_oauth_unlink(
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """解除社群帳號連結(使用者仍有密碼登入,不致被鎖在門外)。"""
    user = db.get(User, actor.user.id)
    provider = user.oauth_provider
    user.oauth_provider = None
    user.oauth_subject = None
    audit_svc.record_from_actor(
        db,
        actor,
        action="auth.oauth.unlink",
        detail={"provider": provider},
        request=request,
    )
    db.commit()
    return {"ok": True}


@router.post("/account/resend-verification")
def account_resend_verification(
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
    mailer=Depends(_get_mailer_dep),
):
    from saas_mvp.auth.ratelimit import email_user_limiter
    from saas_mvp.config import settings
    from saas_mvp.services import account_email as account_email_svc

    if actor.user.email_verified_at is not None:
        raise HTTPException(status_code=409, detail="Email 已完成驗證。")
    if settings.rate_limit_enabled:
        email_user_limiter._check_rate_limit(f"user:{actor.user.id}")
    outcome = account_email_svc.send_verification_email(db, actor.user, mailer)
    return {"outcome": outcome}


# ──────────────────── 方案/帳單/進階功能(R8-4)────────────────────
# 與 /ui/plan、/ui/billing、/ui/features 共用 services/billing、plans、
# features、tenant_einvoice、invoice_profiles。金錢面 mutation 限 owner;
# ecpay 模式回 checkout_url(絕對網址,console 開新視窗完成綠界授權)。
# 公開定價頁 /ui/pricing 留 Jinja。


class PlanCard(BaseModel):
    key: str
    label: str
    monthly_price_cents: int
    feature_labels: list[str]
    is_current: bool


class PlanInfo(BaseModel):
    effective: str
    effective_label: str
    paid: str
    paid_label: str
    trial_active: bool
    trial_days_left: int | None


class PlanEnvelope(BaseModel):
    plan_info: PlanInfo
    plans: list[PlanCard]


class SubscribeResultOut(BaseModel):
    mode: str
    enabled: bool
    payment_id: str | None
    checkout_url: str | None


def _plan_envelope(db, tenant_id: int) -> PlanEnvelope:
    from saas_mvp.models.tenant import Tenant
    from saas_mvp.routers.ui.billing import _plan_info
    from saas_mvp.services import plans as plans_svc

    tenant = db.get(Tenant, tenant_id)
    info = _plan_info(tenant)
    return PlanEnvelope(
        plan_info=PlanInfo(**info),
        plans=[
            PlanCard(
                key=p["key"],
                label=p["label"],
                monthly_price_cents=p["monthly_price_cents"],
                feature_labels=p["feature_labels"],
                is_current=p["is_current"],
            )
            for p in plans_svc.list_plans(current=info["effective"])
        ],
    )


@router.get("/plan", response_model=PlanEnvelope)
def plan_overview(
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    _require_owner(actor)
    return _plan_envelope(db, actor.user.tenant_id)


@router.post("/plan/{plan}/subscribe", response_model=SubscribeResultOut)
def plan_subscribe(
    plan: str,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """訂閱方案。stub 立即生效;ecpay 回 checkout_url(完成首期授權後生效)。"""
    from saas_mvp.models.tenant import Tenant
    from saas_mvp.services import billing as billing_svc

    _require_owner(actor)
    bundle_key = {v: k for k, v in features_svc.BUNDLE_TO_PLAN.items()}.get(plan)
    if bundle_key is None:
        raise HTTPException(status_code=422, detail="未知的方案。")
    tenant = db.get(Tenant, actor.user.tenant_id)
    result = billing_svc.subscribe_bundle(db, tenant, bundle_key, actor.user.id)
    audit_svc.record_from_actor(
        db,
        actor,
        action="billing.plan.subscribe",
        target=f"tenant:{tenant.id}",
        detail={"plan": plan, "mode": result.mode},
        request=request,
    )
    db.commit()
    return SubscribeResultOut(
        mode=result.mode,
        enabled=result.enabled,
        payment_id=result.payment_id,
        checkout_url=result.checkout_url,
    )


@router.post("/plan/unsubscribe", response_model=PlanEnvelope)
def plan_unsubscribe(
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """退訂 → 降 free(已付費者保留原方案至最後扣款日+31 天)。"""
    from saas_mvp.models.tenant import Tenant
    from saas_mvp.services import billing as billing_svc

    _require_owner(actor)
    tenant = db.get(Tenant, actor.user.tenant_id)
    billing_svc.unsubscribe_bundle(db, tenant, actor.user.id)
    audit_svc.record_from_actor(
        db,
        actor,
        action="billing.plan.unsubscribe",
        target=f"tenant:{tenant.id}",
        request=request,
    )
    db.commit()
    return _plan_envelope(db, actor.user.tenant_id)


# ── 帳單 ──


class ChargeRow(BaseModel):
    id: int
    period_no: int
    success: bool
    amount_cents: int
    charged_at: datetime.datetime | None
    rtn_msg: str | None
    invoice_status: str | None
    invoice_no: str | None


class BundleSubRow(BaseModel):
    feature: str
    label: str
    status: str
    period_amount_cents: int
    next_charge_at: datetime.datetime | None


class EinvoiceConfigOut(BaseModel):
    configured: bool
    merchant_id: str
    environment: str
    enabled: bool
    has_hash_key: bool
    has_hash_iv: bool


class InvoiceProfileOut(BaseModel):
    configured: bool
    mode: str | None
    buyer_name: str | None
    buyer_identifier: str | None
    carrier_type: str | None
    has_carrier_number: bool
    masked_carrier: str | None
    donation_code: str | None


class BillingEnvelope(BaseModel):
    plan_info: PlanInfo
    subscription: BundleSubRow | None
    charges: list[ChargeRow]
    einvoice_config: EinvoiceConfigOut
    invoice_profile: InvoiceProfileOut


class EinvoiceConfigBody(BaseModel):
    # 長度上限鏡射 /ui Form(max_length=...):merchant_id 欄位 String(20),
    # 超長不擋會在 PG 撞 StringDataRightTruncation → 500(SQLite 測試看不到)
    merchant_id: str = Field("", max_length=20)
    hash_key: str = Field("", max_length=64)
    hash_iv: str = Field("", max_length=64)
    environment: str = Field("stage", max_length=8)
    enabled: bool = False


class InvoiceProfileBody(BaseModel):
    mode: str
    buyer_name: str = ""
    buyer_identifier: str = ""
    carrier_type: str = "ecpay"
    carrier_number: str = ""
    donation_code: str = ""


def _billing_envelope(db, actor: Actor) -> BillingEnvelope:
    import datetime as _dt

    from sqlalchemy import select as _select

    from saas_mvp.models.feature_subscription import FeatureSubscription
    from saas_mvp.models.invoice import Invoice
    from saas_mvp.models.subscription_charge import SubscriptionCharge
    from saas_mvp.models.tenant import Tenant
    from saas_mvp.routers.ui.billing import _plan_info
    from saas_mvp.services import invoice_profiles as invoice_profiles_svc
    from saas_mvp.services import tenant_einvoice as einvoice_svc

    tid = actor.user.tenant_id
    tenant = db.get(Tenant, tid)
    sub = (
        db.execute(
            _select(FeatureSubscription)
            .where(
                FeatureSubscription.tenant_id == tid,
                FeatureSubscription.feature.in_(features_svc.VALID_BUNDLES),
            )
            .order_by(FeatureSubscription.id.desc())
        )
        .scalars()
        .first()
    )
    charges: list = []
    next_charge_at = None
    if sub is not None:
        charges = (
            db.execute(
                _select(SubscriptionCharge)
                .where(SubscriptionCharge.subscription_id == sub.id)
                .order_by(SubscriptionCharge.id.desc())
                .limit(24)
            )
            .scalars()
            .all()
        )
        if sub.status == "active" and sub.last_charged_at is not None:
            next_charge_at = sub.last_charged_at + _dt.timedelta(days=30)
    invoice_by_charge: dict = {}
    if charges:
        rows = (
            db.execute(
                _select(Invoice).where(
                    Invoice.subscription_charge_id.in_([c.id for c in charges])
                )
            )
            .scalars()
            .all()
        )
        invoice_by_charge = {r.subscription_charge_id: r for r in rows}
    cfg = einvoice_svc.get_config(db, tid)
    profile = invoice_profiles_svc.profile_status(db, tid)
    return BillingEnvelope(
        plan_info=PlanInfo(**_plan_info(tenant)),
        subscription=(
            BundleSubRow(
                feature=sub.feature,
                label=features_svc.BUNDLE_LABELS.get(sub.feature, sub.feature),
                status=sub.status,
                period_amount_cents=sub.period_amount_cents,
                next_charge_at=next_charge_at,
            )
            if sub
            else None
        ),
        charges=[
            ChargeRow(
                id=c.id,
                period_no=c.period_no,
                success=bool(c.success),
                amount_cents=c.amount_cents,
                charged_at=c.charged_at,
                rtn_msg=c.rtn_msg,
                invoice_status=(
                    invoice_by_charge[c.id].status if c.id in invoice_by_charge else None
                ),
                invoice_no=(
                    invoice_by_charge[c.id].invoice_no
                    if c.id in invoice_by_charge
                    else None
                ),
            )
            for c in charges
        ],
        # 憑證絕不外流:只回布林/遮罩
        einvoice_config=EinvoiceConfigOut(
            configured=cfg is not None,
            merchant_id=cfg.merchant_id if cfg else "",
            environment=cfg.environment if cfg else "stage",
            enabled=bool(cfg.enabled) if cfg else False,
            has_hash_key=bool(cfg.hash_key_enc) if cfg else False,
            has_hash_iv=bool(cfg.hash_iv_enc) if cfg else False,
        ),
        invoice_profile=InvoiceProfileOut(**profile),
    )


@router.get("/billing", response_model=BillingEnvelope)
def billing_overview(
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    _require_owner(actor)
    return _billing_envelope(db, actor)


@router.post("/billing/einvoice-config", response_model=BillingEnvelope)
def billing_einvoice_config_save(
    body: EinvoiceConfigBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """店家自有電子發票憑證(opt-in);HashKey/IV 留空=沿用既有值。"""
    from saas_mvp.services import tenant_einvoice as einvoice_svc

    _require_owner(actor)
    try:
        einvoice_svc.save_config(
            db,
            tenant_id=actor.user.tenant_id,
            merchant_id=body.merchant_id,
            hash_key=body.hash_key,
            hash_iv=body.hash_iv,
            environment=body.environment,
            enabled=body.enabled,
            updated_by_user_id=actor.user.id,
        )
    except einvoice_svc.EinvoiceConfigError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    audit_svc.record_from_actor(
        db,
        actor,
        action="billing.einvoice_config.update",
        target="einvoice:config",
        detail={"enabled": body.enabled, "environment": body.environment},
        request=request,
    )
    db.commit()
    return _billing_envelope(db, actor)


@router.post("/billing/invoice-profile", response_model=BillingEnvelope)
def billing_invoice_profile_save(
    body: InvoiceProfileBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """平台開給店家的發票買受資訊;載具號碼加密保存、留空=沿用。"""
    from saas_mvp.services import invoice_profiles as invoice_profiles_svc

    _require_owner(actor)
    try:
        row = invoice_profiles_svc.save_profile(
            db,
            tenant_id=actor.user.tenant_id,
            mode=body.mode,
            buyer_name=body.buyer_name,
            buyer_identifier=body.buyer_identifier,
            carrier_type=body.carrier_type,
            carrier_number=body.carrier_number,
            donation_code=body.donation_code,
            actor_user_id=actor.user.id,
        )
    except invoice_profiles_svc.InvoiceProfileError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    audit_svc.record_from_actor(
        db,
        actor,
        action="billing.invoice_profile.update",
        target=f"tenant:{actor.user.tenant_id}",
        detail={
            "mode": row.mode,
            "carrier_type": row.carrier_type,
            "has_identifier": bool(row.buyer_identifier),
            "has_donation_code": bool(row.donation_code),
        },
        request=request,
    )
    db.commit()
    return _billing_envelope(db, actor)


# ── 進階功能 ──


class FeatureSubInfo(BaseModel):
    status: str
    total_success_times: int
    last_charged_at: datetime.datetime | None


class FeatureCard(BaseModel):
    key: str
    label: str
    monthly_price_cents: int
    enabled: bool
    subscription: FeatureSubInfo | None


class FeatureChargeRow(BaseModel):
    id: int
    feature: str
    feature_label: str
    period_no: int
    success: bool
    amount_cents: int
    charged_at: datetime.datetime | None


class FeaturesEnvelope(BaseModel):
    features: list[FeatureCard]
    charges: list[FeatureChargeRow]
    is_owner: bool


def _features_envelope(db, actor: Actor) -> FeaturesEnvelope:
    from saas_mvp.models.feature_subscription import FeatureSubscription
    from saas_mvp.models.subscription_charge import SubscriptionCharge

    tid = actor.user.tenant_id
    charges = (
        db.query(SubscriptionCharge, FeatureSubscription.feature)
        .join(
            FeatureSubscription,
            SubscriptionCharge.subscription_id == FeatureSubscription.id,
        )
        .filter(SubscriptionCharge.tenant_id == tid)
        .order_by(SubscriptionCharge.id.desc())
        .limit(20)
        .all()
    )
    # 扣款列可能含 bundle(BUNDLE_*),標籤合併 BUNDLE_LABELS
    labels = {**features_svc._FEATURE_LABELS, **features_svc.BUNDLE_LABELS}
    role = getattr(actor.user, "role", None) or "owner"
    return FeaturesEnvelope(
        features=[
            FeatureCard(
                key=f["key"],
                label=f["label"],
                monthly_price_cents=f["monthly_price_cents"],
                enabled=f["enabled"],
                subscription=(
                    FeatureSubInfo(
                        status=f["subscription"]["status"],
                        total_success_times=f["subscription"]["total_success_times"],
                        last_charged_at=f["subscription"]["last_charged_at"],
                    )
                    if f["subscription"]
                    else None
                ),
            )
            for f in features_svc.list_for_tenant(db, tid)
        ],
        charges=[
            FeatureChargeRow(
                id=c.id,
                feature=feature,
                feature_label=labels.get(feature, feature),
                period_no=c.period_no,
                success=bool(c.success),
                amount_cents=c.amount_cents,
                charged_at=c.charged_at,
            )
            for c, feature in charges
        ],
        is_owner=(role == "owner" or actor.user.is_admin),
    )


@router.get("/features", response_model=FeaturesEnvelope)
def features_overview(
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """功能清單任一成員可看(比照 /ui);mutation 才限 owner。"""
    return _features_envelope(db, actor)


@router.post("/features/{feature}/subscribe", response_model=SubscribeResultOut)
def features_subscribe(
    feature: str,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.models.tenant import Tenant
    from saas_mvp.services import billing as billing_svc

    _require_owner(actor)
    try:
        features_svc.validate_feature(feature)
    except features_svc.UnknownFeatureError:
        raise HTTPException(status_code=422, detail="未知的功能。")
    tenant = db.get(Tenant, actor.user.tenant_id)
    result = billing_svc.subscribe_feature(db, tenant, feature, actor.user.id)
    audit_svc.record_from_actor(
        db,
        actor,
        action="billing.feature.subscribe",
        target=f"tenant:{tenant.id}",
        detail={"feature": feature},
        request=request,
    )
    db.commit()
    return SubscribeResultOut(
        mode=result.mode,
        enabled=result.enabled,
        payment_id=result.payment_id,
        checkout_url=result.checkout_url,
    )


@router.post("/features/{feature}/unsubscribe", response_model=FeaturesEnvelope)
def features_unsubscribe(
    feature: str,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.models.tenant import Tenant
    from saas_mvp.services import billing as billing_svc

    _require_owner(actor)
    try:
        features_svc.validate_feature(feature)
    except features_svc.UnknownFeatureError:
        raise HTTPException(status_code=422, detail="未知的功能。")
    tenant = db.get(Tenant, actor.user.tenant_id)
    billing_svc.unsubscribe_feature(db, tenant, feature, actor.user.id)
    audit_svc.record_from_actor(
        db,
        actor,
        action="billing.feature.unsubscribe",
        target=f"tenant:{tenant.id}",
        detail={"feature": feature},
        request=request,
    )
    db.commit()
    return _features_envelope(db, actor)


# ──────────────── 禮物卡線上販售設定(R11-A)────────────────
# owner 專屬;啟用時 service 嚴格驗證(面額+履約保障),
# 避免「收錢後發卡才炸」。public_url 供店家複製宣傳。


class GiftCardOnlineConfigOut(BaseModel):
    enabled: bool
    denominations: list[int]
    fulfillment_guarantee: str
    public_url: str | None


class GiftCardOnlineConfigBody(BaseModel):
    enabled: bool
    denominations: list[int]
    fulfillment_guarantee: str = Field("", max_length=2000)


def _gift_card_online_config_out(db, tenant_id: int) -> GiftCardOnlineConfigOut:
    from saas_mvp.config import settings as app_settings
    from saas_mvp.services import gift_card_sales as sales_svc
    from saas_mvp.services import profile as profile_svc

    config = sales_svc.get_config(db, tenant_id)
    prof = profile_svc.get_by_tenant(db, tenant_id)
    public_url = None
    if prof is not None and prof.slug and sales_svc.sale_available(db, tenant_id):
        base = app_settings.public_base_url.rstrip("/")
        public_url = f"{base}/p/{prof.slug}/gift-cards"
    return GiftCardOnlineConfigOut(
        enabled=bool(config.online_sale_enabled) if config else False,
        denominations=sales_svc.denominations_of(config),
        fulfillment_guarantee=(config.fulfillment_guarantee or "") if config else "",
        public_url=public_url,
    )


@router.get(
    "/gift-cards/online-config",
    response_model=GiftCardOnlineConfigOut,
    dependencies=[Depends(features_svc.require_feature(features_svc.GIFT_CARDS))],
)
def get_gift_card_online_config(
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    _require_owner(actor)
    return _gift_card_online_config_out(db, actor.user.tenant_id)


@router.put(
    "/gift-cards/online-config",
    response_model=GiftCardOnlineConfigOut,
    dependencies=[Depends(features_svc.require_feature(features_svc.GIFT_CARDS))],
)
def save_gift_card_online_config(
    body: GiftCardOnlineConfigBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import gift_card_sales as sales_svc

    _require_owner(actor)
    try:
        sales_svc.save_config(
            db,
            tenant_id=actor.user.tenant_id,
            online_sale_enabled=body.enabled,
            denominations=body.denominations,
            fulfillment_guarantee=body.fulfillment_guarantee,
            updated_by_user_id=actor.user.id,
        )
    except sales_svc.GiftCardSaleError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    audit_svc.record_from_actor(
        db,
        actor,
        action="gift_cards.online_config.update",
        target=f"tenant:{actor.user.tenant_id}",
        detail={"enabled": body.enabled, "denominations": body.denominations},
        request=request,
    )
    db.commit()
    return _gift_card_online_config_out(db, actor.user.tenant_id)


# ────────────────────────── R12-C1:金流能力補齊 ──────────────────────────
# /ui 退役後,訂單退款/定金退款/候補管理原本只存在於被重導的 /ui 頁
# (能力缺口審計揪出)。此處以 console JSON 端點補齊,語意鏡像原 /ui
# handler:owner 限定、audit、可部分退款(amount_twd 省略=全額)。


class OrderRow(BaseModel):
    id: int
    status: str
    total_cents: int
    refund_status: str | None
    refunded_cents: int
    created_at: datetime.datetime | None

    model_config = {"from_attributes": True}


class RefundBody(BaseModel):
    amount_twd: int | None = Field(default=None, ge=1)


class ManualRefundBody(BaseModel):
    note: str = Field(min_length=2, max_length=200)
    amount_twd: int | None = Field(default=None, ge=1)


@router.get(
    "/orders",
    response_model=list[OrderRow],
    dependencies=[
        Depends(features_svc.require_feature(features_svc.PRODUCT_SALES))
    ],
)
def console_list_orders(
    response: Response,
    status_filter: str | None = Query(default=None, alias="status"),
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import shop as shop_svc

    rows = shop_svc.list_orders(
        db, tenant_id=actor.user.tenant_id, status_filter=status_filter
    )
    response.headers["X-Total-Count"] = str(len(rows))
    return [OrderRow.model_validate(o) for o in rows]


@router.post(
    "/orders/{order_id}/refund",
    response_model=OrderRow,
    dependencies=[
        Depends(features_svc.require_feature(features_svc.PRODUCT_SALES))
    ],
)
def console_refund_order(
    order_id: int,
    body: RefundBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """已付訂單閘道退款(可部分,預設全額餘額);owner 限定、服務層鎖列防重。"""
    from saas_mvp.services import order_refund as order_refund_svc

    _require_owner(actor)
    try:
        order = order_refund_svc.request_order_refund(
            db,
            tenant_id=actor.user.tenant_id,
            order_id=order_id,
            actor_user_id=actor.user.id,
            amount_cents=body.amount_twd * 100 if body.amount_twd else None,
        )
    except order_refund_svc.OrderRefundError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    audit_svc.record_from_actor(
        db, actor, action="shop.order.refund", target=f"order:{order_id}",
        detail={"refunded_twd": (order.refunded_cents or 0) // 100},
        request=request,
    )
    db.commit()
    return OrderRow.model_validate(order)


@router.post(
    "/orders/{order_id}/refund/manual",
    response_model=OrderRow,
    dependencies=[
        Depends(features_svc.require_feature(features_svc.PRODUCT_SALES))
    ],
)
def console_manual_refund_order(
    order_id: int,
    body: ManualRefundBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """外部金流後台已退款後的人工對帳(不呼叫金流、不重複退刷);owner 限定。"""
    from saas_mvp.services import order_refund as order_refund_svc

    _require_owner(actor)
    try:
        order = order_refund_svc.confirm_manual_refund(
            db,
            tenant_id=actor.user.tenant_id,
            order_id=order_id,
            actor_user_id=actor.user.id,
            note=body.note,
            amount_cents=body.amount_twd * 100 if body.amount_twd else None,
        )
    except order_refund_svc.OrderRefundError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    audit_svc.record_from_actor(
        db, actor, action="shop.order.refund.manual", target=f"order:{order_id}",
        detail={
            "refunded_twd": (order.refunded_cents or 0) // 100,
            "note": body.note[:200],
        },
        request=request,
    )
    db.commit()
    return OrderRow.model_validate(order)


class DepositRefundOut(BaseModel):
    id: int
    deposit_status: str | None
    deposit_cents: int | None
    deposit_refunded_cents: int | None

    model_config = {"from_attributes": True}


@router.post("/reservations/{reservation_id}/deposit-refund", response_model=DepositRefundOut)
def console_refund_deposit(
    reservation_id: int,
    body: RefundBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """退還已付定金(可部分,預設全額);owner 限定、服務層鎖列防重。"""
    from saas_mvp.services import deposit as deposit_svc

    _require_owner(actor)
    try:
        row = deposit_svc.request_full_refund(
            db,
            tenant_id=actor.user.tenant_id,
            reservation_id=reservation_id,
            actor_user_id=actor.user.id,
            amount_cents=body.amount_twd * 100 if body.amount_twd else None,
        )
    except deposit_svc.DepositRefundError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    audit_svc.record_from_actor(
        db, actor, action="booking.deposit.refund",
        target=f"reservation:{reservation_id}",
        detail={
            "result": "refunded",
            "amount_twd": (row.deposit_refunded_cents or row.deposit_cents or 0) // 100,
        },
        request=request,
    )
    db.commit()
    return DepositRefundOut.model_validate(row)


@router.post(
    "/reservations/{reservation_id}/deposit-refund/manual",
    response_model=DepositRefundOut,
)
def console_manual_refund_deposit(
    reservation_id: int,
    body: ManualRefundBody,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """外部金流後台已退定金後的人工對帳(不呼叫金流);owner 限定。"""
    from saas_mvp.services import deposit as deposit_svc

    _require_owner(actor)
    try:
        row = deposit_svc.confirm_manual_refund(
            db,
            tenant_id=actor.user.tenant_id,
            reservation_id=reservation_id,
            actor_user_id=actor.user.id,
            note=body.note,
            amount_cents=body.amount_twd * 100 if body.amount_twd else None,
        )
    except deposit_svc.DepositRefundError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    audit_svc.record_from_actor(
        db, actor, action="booking.deposit.refund.manual",
        target=f"reservation:{reservation_id}",
        detail={"note": body.note[:200]},
        request=request,
    )
    db.commit()
    return DepositRefundOut.model_validate(row)


class WaitlistRow(BaseModel):
    id: int
    slot_id: int
    status: str
    party_size: int
    display_name: str | None
    line_user_id: str | None
    created_at: datetime.datetime | None
    slot_start: datetime.datetime | None = None

    model_config = {"from_attributes": True}


@router.get("/waitlist", response_model=list[WaitlistRow])
def console_list_waitlist(
    response: Response,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """候補清單(新到舊),附時段起始時間。"""
    from saas_mvp.models.booking_slot import BookingSlot
    from saas_mvp.services import waitlist as waitlist_svc

    rows = waitlist_svc.list_waitlist(db, tenant_id=actor.user.tenant_id)
    slot_ids = {r.slot_id for r in rows}
    slots = {
        s.id: s
        for s in tenant_query(db, BookingSlot, actor.user.tenant_id)
        .filter(BookingSlot.id.in_(slot_ids))
        .all()
    } if slot_ids else {}
    out = []
    for r in rows:
        row = WaitlistRow.model_validate(r)
        slot = slots.get(r.slot_id)
        row.slot_start = slot.slot_start if slot else None
        out.append(row)
    response.headers["X-Total-Count"] = str(len(out))
    return out


@router.post("/waitlist/{entry_id}/cancel", response_model=WaitlistRow)
def console_cancel_waitlist(
    entry_id: int,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    """店家取消候補(釋放 notified 保留的名額)。"""
    from saas_mvp.services import waitlist as waitlist_svc

    try:
        entry = waitlist_svc.cancel_waitlist_by_staff(
            db, tenant_id=actor.user.tenant_id, entry_id=entry_id
        )
    except waitlist_svc.WaitlistEntryNotFound:
        db.rollback()
        raise HTTPException(status_code=404, detail="候補紀錄不存在。")
    audit_svc.record_from_actor(
        db, actor, action="booking.waitlist.cancel",
        target=f"waitlist:{entry_id}", detail={}, request=request,
    )
    db.commit()
    return WaitlistRow.model_validate(entry)
