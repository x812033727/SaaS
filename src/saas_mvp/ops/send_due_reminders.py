"""派送到期的預約提醒（cron 執行；單實例去重）。

Usage:
    python -m saas_mvp.ops.send_due_reminders --dry-run
    python -m saas_mvp.ops.send_due_reminders --apply --limit 200

設計（比照 ops/backfill_line_bot_user_id.py）：
  * argparse --dry-run（預設）/ --apply；session_factory / push_client 可注入供測試。
  * 兩階段：先讀候選 reminder id，再逐筆獨立 session 處理（per-row 例外隔離）。
  * 冪等去重：逐筆 SELECT … FOR UPDATE 鎖定 reminder 列後**重驗 status=='pending'**，
    並發掃描或重跑都不會重送（配合 UniqueConstraint(reservation_id, kind)）。
  * 不走翻譯 quota；以 --limit（預設 settings.reminder_max_per_run）控制批量。
  * 跳過：reservation 已取消、租戶非 booking 模式、無 LINE 設定。
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from saas_mvp.config import settings
from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.line_client import HttpLinePushClient, LinePushClient
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.models.reservation_reminder import (
    REMINDER_FAILED,
    REMINDER_PENDING,
    REMINDER_SENT,
    REMINDER_SKIPPED,
    ReservationReminder,
)
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import features as features_svc
from saas_mvp.services import push_quota as push_quota_svc
from saas_mvp.services.reminders import build_reminder_text


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass(frozen=True)
class ReminderResult:
    reminder_id: int
    status: str       # sent | skipped | failed | would_send
    reason: str
    error_type: str | None = None

    def to_line(self) -> str:
        parts = [
            f"reminder_id={self.reminder_id}",
            f"status={self.status}",
            f"reason={self.reason}",
        ]
        if self.error_type:
            parts.append(f"error_type={self.error_type}")
        return " ".join(parts)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _due_reminder_ids(
    db: Session, *, now: datetime.datetime, limit: int
) -> list[int]:
    stmt = (
        select(ReservationReminder.id)
        .where(
            ReservationReminder.status == REMINDER_PENDING,
            ReservationReminder.remind_at <= now,
        )
        .order_by(ReservationReminder.remind_at.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars())


_log = logging.getLogger(__name__)


def _clamp_sms(text: str, limit: int = 300) -> str:
    """SMS 300 字截斷,但**絕不砍斷結尾的 portal 連結行**(R5-B2)。

    連結是 SMS 收件者唯一的自助互動手段;超長時犧牲前段內文保住連結。
    """
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    if lines and lines[-1].startswith("管理預約:"):
        tail = "\n" + lines[-1]
        head = "\n".join(lines[:-1])
        return head[: max(0, limit - len(tail))] + tail
    return text[:limit]


def _sms_fallback(db, tenant_id: int, line_user_id: str | None, text: str) -> bool:
    """LINE 推播失敗時的簡訊補送(E3)。旗標關/查無手機 → no-op;失敗只記 log。

    回傳是否真的送出(R5-B3:False 時輪到 email 第三段 fallback)。
    """
    from saas_mvp.config import settings

    if not settings.sms_fallback_enabled or not line_user_id:
        return False
    try:
        from saas_mvp.models.customer import Customer
        from saas_mvp.services.sms import get_sms_provider

        customer = db.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.line_user_id == line_user_id,
            )
        ).scalar_one_or_none()
        if customer is None or not customer.phone:
            return False
        get_sms_provider().send(to=customer.phone, body=_clamp_sms(text))
        return True
    except Exception:  # noqa: BLE001 — fallback 絕不影響批次
        _log.warning("sms fallback failed", exc_info=True)
        return False


def _email_fallback(db, tenant_id: int, customer_id: int | None, text: str) -> None:
    """第三段 fallback(R5-B3):LINE 失敗且 SMS 未送出時,寄 email 提醒。

    走 email_delivery 可靠佇列(失敗自動重試);顧客無 email → no-op。
    deliver_or_queue 自帶 commit——僅在推播失敗的收尾階段呼叫,
    不在任何未完成交易中途。
    """
    if customer_id is None:
        return
    try:
        from saas_mvp.models.customer import Customer
        from saas_mvp.services import email_delivery as email_svc
        from saas_mvp.services.mailer import get_mailer

        customer = db.get(Customer, customer_id)
        if customer is None or not customer.email:
            return
        email_svc.deliver_or_queue(
            db,
            get_mailer(db),
            user_id=None,
            category="booking_reminder",
            recipient=customer.email,
            subject="【預約提醒】您有即將到來的預約",
            body=text,
        )
    except Exception:  # noqa: BLE001 — fallback 絕不影響批次
        _log.warning("email fallback failed", exc_info=True)


def _process_one(
    session_factory: sessionmaker,
    push_client: LinePushClient,
    *,
    reminder_id: int,
    apply: bool,
    now: datetime.datetime,
) -> ReminderResult:
    with session_factory() as db:
        # 鎖定 reminder 列並重驗 pending（並發/重跑去重的關鍵）
        rem = db.execute(
            select(ReservationReminder)
            .where(ReservationReminder.id == reminder_id)
            .with_for_update()
        ).scalar_one_or_none()
        if rem is None or rem.status != REMINDER_PENDING:
            db.rollback()
            return ReminderResult(reminder_id, "skipped", "not_pending")

        resv = db.get(Reservation, rem.reservation_id)
        if resv is None or resv.status != RESERVATION_CONFIRMED:
            rem.status = REMINDER_SKIPPED
            rem.updated_at = now
            db.commit()
            return ReminderResult(reminder_id, "skipped", "reservation_inactive")

        cfg = db.execute(
            select(LineChannelConfig).where(
                LineChannelConfig.tenant_id == rem.tenant_id
            )
        ).scalar_one_or_none()
        if cfg is None or (cfg.bot_mode or "translation") != "booking":
            rem.status = REMINDER_SKIPPED
            rem.updated_at = now
            db.commit()
            return ReminderResult(reminder_id, "skipped", "not_booking_mode")

        # 進階功能閘門：租戶若關閉 AUTO_REMINDER（含已入列後退訂），不再派送。
        if not features_svc.is_enabled(db, rem.tenant_id, features_svc.AUTO_REMINDER):
            rem.status = REMINDER_SKIPPED
            rem.updated_at = now
            db.commit()
            return ReminderResult(reminder_id, "skipped", "feature_disabled")

        slot = db.get(BookingSlot, resv.slot_id)
        tenant = db.get(Tenant, rem.tenant_id)
        store_name = tenant.name if tenant is not None else ""
        # R5-B2:顧客 portal 連結(read-only;book_slot 建單時已補發 token)。
        portal_url = None
        if resv.customer_id is not None:
            from saas_mvp.models.customer import Customer
            from saas_mvp.services import customer_portal as portal_svc

            customer = db.get(Customer, resv.customer_id)
            if customer is not None:
                portal_url = portal_svc.portal_url(customer)
        text = build_reminder_text(
            slot=slot, reservation=resv, store_name=store_name,
            portal_url=portal_url,
        )

        if not apply:
            db.rollback()
            return ReminderResult(reminder_id, "would_send", "dry_run")

        # E3 fallback 用:rollback 會 expire ORM 物件,先抓純值
        fallback_tenant_id, fallback_user_id = rem.tenant_id, rem.line_user_id
        fallback_customer_id = resv.customer_id

        # 月度推播額度閘門：超出本月額度則跳過（不推播、標 skipped），
        # 同租戶超額不影響其他列（per-row isolation）。
        if not push_quota_svc.has_push_quota(db, rem.tenant_id, now=now):
            rem.status = REMINDER_SKIPPED
            rem.last_error = "push allowance exceeded"
            rem.updated_at = now
            db.commit()
            return ReminderResult(
                reminder_id, "skipped", "push_allowance_exceeded"
            )

        try:
            access_token = cfg.access_token  # Fernet 解密
            # 提醒附「確認出席 / 取消預約」互動按鈕（postback 走既有
            # confirm / cancel 對話分支；擁有者驗證在服務層）。
            push_client.push(
                rem.line_user_id,
                text,
                access_token=access_token,
                quick_reply=[
                    (
                        "確認出席",
                        f"action=confirm&reservation_id={rem.reservation_id}",
                    ),
                    (
                        "取消預約",
                        f"action=cancel&reservation_id={rem.reservation_id}",
                    ),
                ],
            )
        except Exception as exc:  # noqa: BLE001 - per-row failure must not stop batch
            db.rollback()
            # 在新交易標 failed（rollback 後 rem 已過期，重新鎖定）
            rem2 = db.execute(
                select(ReservationReminder)
                .where(ReservationReminder.id == reminder_id)
                .with_for_update()
            ).scalar_one_or_none()
            if rem2 is not None:
                rem2.status = REMINDER_FAILED
                rem2.attempt_count = (rem2.attempt_count or 0) + 1
                rem2.last_error = type(exc).__name__[:255]
                rem2.updated_at = now
                db.commit()
            # E3:簡訊 fallback(旗標開 + 顧客有手機才送;best-effort 永不拋);
            # R5-B3:SMS 未送出 → email 第三段(可靠佇列,自動重試)。
            if not _sms_fallback(db, fallback_tenant_id, fallback_user_id, text):
                _email_fallback(db, fallback_tenant_id, fallback_customer_id, text)
            return ReminderResult(
                reminder_id, "failed", "push_error", error_type=type(exc).__name__
            )

        rem.status = REMINDER_SENT
        rem.sent_at = now
        rem.attempt_count = (rem.attempt_count or 0) + 1
        rem.updated_at = now
        # 後扣：推播成功後才計量本月推播額度（只計實際送出者）；
        # 與標 sent 同交易單一 commit（每筆 2 commits → 1）。
        push_quota_svc.consume_push_in_txn(db, rem.tenant_id, now=now)
        db.commit()
        return ReminderResult(reminder_id, "sent", "pushed")


def send_due_reminders(
    *,
    session_factory: sessionmaker = SessionLocal,
    push_client: LinePushClient | None = None,
    apply: bool = False,
    limit: int | None = None,
    now: datetime.datetime | None = None,
) -> list[ReminderResult]:
    """掃描到期提醒並（apply 時）推播；回傳每筆結果。"""
    client = push_client or HttpLinePushClient()
    effective_now = now or _utcnow()
    effective_limit = limit if limit is not None else settings.reminder_max_per_run
    with session_factory() as db:
        ids = _due_reminder_ids(db, now=effective_now, limit=effective_limit)
    return [
        _process_one(
            session_factory,
            client,
            reminder_id=rid,
            apply=apply,
            now=effective_now,
        )
        for rid in ids
    ]


def write_report(
    results: list[ReminderResult], *, apply: bool, out: TextIO
) -> None:
    mode = "apply" if apply else "dry_run"
    print(f"mode={mode}", file=out)
    for result in results:
        print(result.to_line(), file=out)
    counts = Counter(result.status for result in results)
    print(
        "summary "
        f"total={len(results)} "
        f"sent={counts['sent']} "
        f"would_send={counts['would_send']} "
        f"skipped={counts['skipped']} "
        f"failed={counts['failed']}",
        file=out,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send due booking reservation reminders via LINE push."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_false",
        dest="apply",
        help="Report what would send; do not push or commit. Default.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        dest="apply",
        help="Push reminders and mark them sent.",
    )
    parser.set_defaults(apply=False)
    parser.add_argument(
        "--limit", type=_positive_int, help="Max due reminders to process."
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    session_factory: sessionmaker = SessionLocal,
    push_client: LinePushClient | None = None,
    stdout: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    # 確保 SQLAlchemy registry 完整：standalone（python -m / cron）執行時
    # 各 model 未必都被 import，relationship 字串（如 'Tenant'）會解析失敗。
    import_all_models()
    results = send_due_reminders(
        session_factory=session_factory,
        push_client=push_client,
        apply=args.apply,
        limit=args.limit,
    )
    write_report(results, apply=args.apply, out=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
