"""Email outbox：先持久化、立即嘗試，失敗由 scheduler 指數退避重試。"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from saas_mvp.models.email_delivery import EMAIL_FAILED, EMAIL_PENDING, EMAIL_SENT, EmailDelivery
from saas_mvp.services.mailer import Mailer, MailerError

MAX_ATTEMPTS = 5
_RETRY_SECONDS = (60, 300, 1800, 7200, 21600)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _safe_error(exc: Exception) -> str:
    return (str(exc) if isinstance(exc, MailerError) else type(exc).__name__)[:255]


def attempt(db: Session, row: EmailDelivery, mailer: Mailer, *, now=None) -> str:
    effective_now = now or _utcnow()
    try:
        mailer.send(to=row.recipient, subject=row.subject, body=row.body)
    except Exception as exc:  # noqa: BLE001 — outbox 必須攔截並排程重試
        row.attempt_count = (row.attempt_count or 0) + 1
        row.last_error = _safe_error(exc)
        row.updated_at = effective_now
        if row.attempt_count >= MAX_ATTEMPTS:
            row.status = EMAIL_FAILED
            row.next_attempt_at = None
        else:
            row.status = EMAIL_PENDING
            delay = _RETRY_SECONDS[min(row.attempt_count - 1, len(_RETRY_SECONDS) - 1)]
            row.next_attempt_at = effective_now + datetime.timedelta(seconds=delay)
        db.commit()
        return row.status
    row.status = EMAIL_SENT
    row.attempt_count = (row.attempt_count or 0) + 1
    row.last_error = None
    row.next_attempt_at = None
    row.sent_at = effective_now
    row.updated_at = effective_now
    db.commit()
    return EMAIL_SENT


def deliver_or_queue(
    db: Session,
    mailer: Mailer,
    *,
    user_id: int | None,
    category: str,
    recipient: str,
    subject: str,
    body: str,
) -> str:
    now = _utcnow()
    row = EmailDelivery(
        user_id=user_id,
        category=category,
        recipient=recipient,
        subject=subject,
        body=body,
        status=EMAIL_PENDING,
        # 留一分鐘窗口，避免立即寄送與 scheduler 同時取得同一筆。
        next_attempt_at=now + datetime.timedelta(minutes=1),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return attempt(db, row, mailer, now=now)


def customer_recipient(db: Session, customer_id: int | None) -> str | None:
    """顧客檔的 email 收件地址;無檔/無 email 回 None(R12-B 共用)。"""
    if customer_id is None:
        return None
    from saas_mvp.models.customer import Customer

    customer = db.get(Customer, customer_id)
    if customer is None or not customer.email:
        return None
    return customer.email


def due_ids(db: Session, *, now: datetime.datetime, limit: int = 100) -> list[int]:
    return list(db.execute(
        select(EmailDelivery.id)
        .where(EmailDelivery.status == EMAIL_PENDING, EmailDelivery.next_attempt_at <= now)
        .order_by(EmailDelivery.next_attempt_at, EmailDelivery.id)
        .limit(limit)
    ).scalars())


def recent(db: Session, *, limit: int = 30) -> list[EmailDelivery]:
    return list(db.execute(
        select(EmailDelivery).order_by(EmailDelivery.id.desc()).limit(limit)
    ).scalars())


def summary(db: Session) -> dict[str, int]:
    counts = dict(db.execute(
        select(EmailDelivery.status, func.count(EmailDelivery.id)).group_by(EmailDelivery.status)
    ).all())
    return {key: int(counts.get(key, 0)) for key in (EMAIL_PENDING, EMAIL_SENT, EMAIL_FAILED)}


def retry_unsent(db: Session) -> int:
    rows = list(db.execute(
        select(EmailDelivery).where(EmailDelivery.status.in_((EMAIL_PENDING, EMAIL_FAILED)))
    ).scalars())
    now = _utcnow()
    for row in rows:
        row.status = EMAIL_PENDING
        row.attempt_count = 0
        row.next_attempt_at = now
        row.last_error = None
        row.updated_at = now
    db.commit()
    return len(rows)
