"""顧客諮詢表／同意書範本、自動派發與簽署快照。"""

from __future__ import annotations

import datetime
import json
import math
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.client_form import (
    ClientFormQuestion,
    ClientFormRequest,
    ClientFormTemplate,
)
from saas_mvp.models.reservation import RESERVATION_CANCELLED, Reservation
from saas_mvp.models.service import Service

FIELD_TYPES = frozenset({"text", "textarea", "number", "date", "select", "checkbox"})


class ClientFormError(ValueError):
    pass


class ClientFormNotFound(ClientFormError):
    pass


class ClientFormAlreadyCompleted(ClientFormError):
    pass


@dataclass(frozen=True)
class PublicForm:
    request: ClientFormRequest
    questions: list[dict]


def list_templates(db: Session, *, tenant_id: int) -> list[ClientFormTemplate]:
    return (
        db.execute(
            select(ClientFormTemplate)
            .where(ClientFormTemplate.tenant_id == tenant_id)
            .order_by(ClientFormTemplate.id.desc())
        )
        .scalars()
        .all()
    )


def questions(
    db: Session, *, tenant_id: int, template_id: int
) -> list[ClientFormQuestion]:
    return (
        db.execute(
            select(ClientFormQuestion)
            .where(
                ClientFormQuestion.tenant_id == tenant_id,
                ClientFormQuestion.template_id == template_id,
            )
            .order_by(ClientFormQuestion.sort_order, ClientFormQuestion.id)
        )
        .scalars()
        .all()
    )


def create_template(
    db: Session,
    *,
    tenant_id: int,
    name: str,
    intro: str,
    consent_text: str,
    service_id: int | None,
    require_signature: bool = True,
) -> ClientFormTemplate:
    name = (name or "").strip()
    consent = (consent_text or "").strip()
    if not name or len(name) > 128:
        raise ClientFormError("表單名稱為必填，最多 128 字。")
    intro = (intro or "").strip()
    if len(intro) > 4000:
        raise ClientFormError("填寫說明最多 4,000 字。")
    if len(consent) < 10 or len(consent) > 4000:
        raise ClientFormError("同意聲明需為 10～4,000 字。")
    if service_id is not None:
        owned = db.execute(
            select(Service).where(
                Service.id == service_id, Service.tenant_id == tenant_id
            )
        ).scalar_one_or_none()
        if owned is None:
            raise ClientFormError("綁定的服務不存在。")
    duplicate = db.execute(
        select(ClientFormTemplate).where(
            ClientFormTemplate.tenant_id == tenant_id, ClientFormTemplate.name == name
        )
    ).scalar_one_or_none()
    if duplicate:
        raise ClientFormError("已有同名表單。")
    row = ClientFormTemplate(
        tenant_id=tenant_id,
        name=name,
        intro=intro or None,
        consent_text=consent,
        service_id=service_id,
        require_signature=require_signature,
    )
    db.add(row)
    db.flush()
    return row


def add_question(
    db: Session,
    *,
    tenant_id: int,
    template_id: int,
    label: str,
    field_type: str,
    required: bool,
    options: str = "",
) -> ClientFormQuestion:
    template = db.execute(
        select(ClientFormTemplate)
        .where(
            ClientFormTemplate.id == template_id,
            ClientFormTemplate.tenant_id == tenant_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if template is None:
        raise ClientFormNotFound("表單不存在。")
    current_questions = questions(db, tenant_id=tenant_id, template_id=template_id)
    if len(current_questions) >= 100:
        raise ClientFormError("每份表單最多 100 題。")
    label = (label or "").strip()
    if not label or len(label) > 255:
        raise ClientFormError("問題文字為必填，最多 255 字。")
    if field_type not in FIELD_TYPES:
        raise ClientFormError("不支援的問題類型。")
    option_values = list(
        dict.fromkeys(x.strip() for x in (options or "").splitlines() if x.strip())
    )
    if field_type == "select" and len(option_values) < 2:
        raise ClientFormError("下拉選單至少需要兩個選項（每行一個）。")
    if len(option_values) > 50 or any(len(x) > 100 for x in option_values):
        raise ClientFormError("選項最多 50 個，每個最多 100 字。")
    row = ClientFormQuestion(
        tenant_id=tenant_id,
        template_id=template_id,
        label=label,
        field_type=field_type,
        is_required=required,
        options_json=json.dumps(option_values, ensure_ascii=False)
        if option_values
        else None,
        sort_order=(current_questions[-1].sort_order + 10) if current_questions else 10,
    )
    template.version += 1
    db.add(row)
    db.flush()
    return row


def set_active(
    db: Session, *, tenant_id: int, template_id: int, active: bool
) -> ClientFormTemplate:
    row = db.execute(
        select(ClientFormTemplate)
        .where(
            ClientFormTemplate.id == template_id,
            ClientFormTemplate.tenant_id == tenant_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ClientFormNotFound("表單不存在。")
    if active and not questions(db, tenant_id=tenant_id, template_id=template_id):
        raise ClientFormError("至少加入一題後才能啟用。")
    row.is_active = active
    return row


def _question_snapshot(row: ClientFormQuestion) -> dict:
    return {
        "id": row.id,
        "label": row.label,
        "type": row.field_type,
        "required": bool(row.is_required),
        "options": json.loads(row.options_json) if row.options_json else [],
    }


def attach_to_reservation(
    db: Session, *, reservation: Reservation
) -> list[ClientFormRequest]:
    """依服務自動派發；不 commit，與預約建單同一交易。"""
    templates = (
        db.execute(
            select(ClientFormTemplate)
            .where(
                ClientFormTemplate.tenant_id == reservation.tenant_id,
                ClientFormTemplate.is_active.is_(True),
                (ClientFormTemplate.service_id == reservation.service_id)
                | (ClientFormTemplate.service_id.is_(None)),
            )
            .order_by(ClientFormTemplate.id)
        )
        .scalars()
        .all()
    )
    created = []
    for template in templates:
        existing = db.execute(
            select(ClientFormRequest).where(
                ClientFormRequest.tenant_id == reservation.tenant_id,
                ClientFormRequest.template_id == template.id,
                ClientFormRequest.reservation_id == reservation.id,
            )
        ).scalar_one_or_none()
        if existing:
            created.append(existing)
            continue
        qs = questions(db, tenant_id=reservation.tenant_id, template_id=template.id)
        if not qs:
            continue
        row = ClientFormRequest(
            tenant_id=reservation.tenant_id,
            template_id=template.id,
            reservation_id=reservation.id,
            customer_id=reservation.customer_id,
            token=secrets.token_urlsafe(32),
            status="pending",
            template_name_snapshot=template.name,
            intro_snapshot=template.intro,
            consent_text_snapshot=template.consent_text,
            questions_json=json.dumps(
                [_question_snapshot(q) for q in qs], ensure_ascii=False
            ),
            template_version=template.version,
            require_signature_snapshot=template.require_signature,
        )
        db.add(row)
        db.flush()
        created.append(row)
    return created


def for_reservation(
    db: Session, *, tenant_id: int, reservation_id: int
) -> list[ClientFormRequest]:
    return (
        db.execute(
            select(ClientFormRequest)
            .where(
                ClientFormRequest.tenant_id == tenant_id,
                ClientFormRequest.reservation_id == reservation_id,
            )
            .order_by(ClientFormRequest.id)
        )
        .scalars()
        .all()
    )


def for_customer(
    db: Session, *, tenant_id: int, customer_id: int
) -> list[ClientFormRequest]:
    return (
        db.execute(
            select(ClientFormRequest)
            .where(
                ClientFormRequest.tenant_id == tenant_id,
                ClientFormRequest.customer_id == customer_id,
            )
            .order_by(ClientFormRequest.id.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )


def form_url(row: ClientFormRequest) -> str:
    return f"{settings.public_base_url.rstrip('/')}/client-forms/{row.token}"


def get_public(db: Session, token: str, *, lock: bool = False) -> PublicForm:
    stmt = select(ClientFormRequest).where(ClientFormRequest.token == token)
    if lock:
        stmt = stmt.with_for_update()
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        raise ClientFormNotFound("填寫連結不存在。")
    return PublicForm(row, json.loads(row.questions_json))


def submission_block_reason(db: Session, row: ClientFormRequest) -> str | None:
    reservation = db.execute(
        select(Reservation).where(
            Reservation.id == row.reservation_id,
            Reservation.tenant_id == row.tenant_id,
        )
    ).scalar_one_or_none()
    if reservation is None or reservation.status == RESERVATION_CANCELLED:
        return "此預約已取消，表單停止填寫。"
    return None


def submit(
    db: Session,
    *,
    token: str,
    answers: dict[str, str],
    signer_name: str,
    consent: bool,
    ip: str | None,
    user_agent: str | None,
) -> ClientFormRequest:
    public = get_public(db, token, lock=True)
    row = public.request
    if row.status == "completed":
        raise ClientFormAlreadyCompleted("此表單已完成，不能再次修改。")
    blocked = submission_block_reason(db, row)
    if blocked:
        raise ClientFormError(blocked)
    cleaned: dict[str, str | bool] = {}
    for question in public.questions:
        key = str(question["id"])
        raw = (answers.get(key) or "").strip()
        if question["type"] == "checkbox":
            value: str | bool = raw == "true"
            if question["required"] and not value:
                raise ClientFormError(f"請確認：{question['label']}")
        else:
            if question["required"] and not raw:
                raise ClientFormError(f"請填寫：{question['label']}")
            if len(raw) > 4000:
                raise ClientFormError(f"回答過長：{question['label']}")
            if question["type"] == "number" and raw:
                try:
                    number = float(raw)
                except ValueError as exc:
                    raise ClientFormError(f"請輸入數字：{question['label']}") from exc
                if not math.isfinite(number):
                    raise ClientFormError(f"請輸入有限數字：{question['label']}")
            if question["type"] == "date" and raw:
                try:
                    datetime.date.fromisoformat(raw)
                except ValueError as exc:
                    raise ClientFormError(
                        f"請輸入有效日期：{question['label']}"
                    ) from exc
            if question["type"] == "select" and raw and raw not in question["options"]:
                raise ClientFormError(f"選項無效：{question['label']}")
            value = raw
        cleaned[key] = value
    signer = (signer_name or "").strip()
    if row.require_signature_snapshot and not signer:
        raise ClientFormError("請輸入簽署人姓名。")
    if not consent:
        raise ClientFormError("請確認同意聲明後再提交。")
    now = datetime.datetime.now(datetime.timezone.utc)
    row.answers_json = json.dumps(cleaned, ensure_ascii=False)
    row.signer_name = signer[:128] or None
    row.signed_at = now
    row.completed_at = now
    row.status = "completed"
    row.submitted_ip = (ip or "")[:64] or None
    row.submitted_user_agent = (user_agent or "")[:255] or None
    db.commit()
    db.refresh(row)
    return row
