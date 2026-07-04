"""派送到期的預約異動通知（cron 執行；單實例去重）。

Usage:
    python -m saas_mvp.ops.send_due_notifications --dry-run
    python -m saas_mvp.ops.send_due_notifications --apply --limit 200

設計（比照 ops/send_due_reminders.py）：
  * argparse --dry-run（預設）/ --apply；session_factory / push_client 可注入供測試。
  * 兩階段：先讀候選 notification id，再逐筆獨立 session 處理（per-row 例外隔離）。
  * 冪等去重：逐筆 SELECT … FOR UPDATE 鎖定列後**重驗 status=='pending'**，
    並發掃描或重跑都不會重送（配合 UniqueConstraint(reservation_id, kind)）。
  * 不走翻譯 quota；以 --limit（預設 settings.notification_max_per_run）控制批量。
  * 跳過：reservation 不存在、租戶非 booking 模式、無 LINE 設定、未開通 BOOKING_NOTIFY。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from collections import Counter
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from saas_mvp.config import settings
from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.line_client import HttpLinePushClient, LinePushClient
from saas_mvp.models.booking_notification import (
    NOTIFY_FAILED,
    NOTIFY_PENDING,
    NOTIFY_SENT,
    NOTIFY_SKIPPED,
    BookingNotification,
)
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.reservation import Reservation
from saas_mvp.services import features as features_svc
from saas_mvp.services import push_quota as push_quota_svc


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass(frozen=True)
class NotificationResult:
    notification_id: int
    status: str       # sent | skipped | failed | would_send
    reason: str
    error_type: str | None = None

    def to_line(self) -> str:
        parts = [
            f"notification_id={self.notification_id}",
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


def _due_notification_ids(
    db: Session, *, now: datetime.datetime, limit: int
) -> list[int]:
    stmt = (
        select(BookingNotification.id)
        .where(
            BookingNotification.status == NOTIFY_PENDING,
            BookingNotification.send_after <= now,
        )
        .order_by(BookingNotification.send_after.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars())


def _process_one(
    session_factory: sessionmaker,
    push_client: LinePushClient,
    *,
    notification_id: int,
    apply: bool,
    now: datetime.datetime,
) -> NotificationResult:
    with session_factory() as db:
        # 鎖定列並重驗 pending（並發/重跑去重的關鍵）
        notif = db.execute(
            select(BookingNotification)
            .where(BookingNotification.id == notification_id)
            .with_for_update()
        ).scalar_one_or_none()
        if notif is None or notif.status != NOTIFY_PENDING:
            db.rollback()
            return NotificationResult(notification_id, "skipped", "not_pending")

        resv = db.get(Reservation, notif.reservation_id)
        if resv is None:
            notif.status = NOTIFY_SKIPPED
            notif.updated_at = now
            db.commit()
            return NotificationResult(
                notification_id, "skipped", "reservation_missing"
            )

        cfg = db.execute(
            select(LineChannelConfig).where(
                LineChannelConfig.tenant_id == notif.tenant_id
            )
        ).scalar_one_or_none()
        if cfg is None or (cfg.bot_mode or "translation") != "booking":
            notif.status = NOTIFY_SKIPPED
            notif.updated_at = now
            db.commit()
            return NotificationResult(
                notification_id, "skipped", "not_booking_mode"
            )

        # 進階功能閘門：租戶若關閉 BOOKING_NOTIFY（含已入列後退訂），不再派送。
        if not features_svc.is_enabled(
            db, notif.tenant_id, features_svc.BOOKING_NOTIFY
        ):
            notif.status = NOTIFY_SKIPPED
            notif.updated_at = now
            db.commit()
            return NotificationResult(
                notification_id, "skipped", "feature_disabled"
            )

        text = notif.payload_text

        if not apply:
            db.rollback()
            return NotificationResult(notification_id, "would_send", "dry_run")

        # 月度推播額度閘門：超出本月額度則跳過（不推播、標 skipped）。
        if not push_quota_svc.has_push_quota(db, notif.tenant_id, now=now):
            notif.status = NOTIFY_SKIPPED
            notif.last_error = "push allowance exceeded"
            notif.updated_at = now
            db.commit()
            return NotificationResult(
                notification_id, "skipped", "push_allowance_exceeded"
            )

        try:
            access_token = cfg.access_token  # Fernet 解密
            push_client.push(notif.line_user_id, text, access_token=access_token)
        except Exception as exc:  # noqa: BLE001 - per-row failure must not stop batch
            db.rollback()
            # 在新交易標 failed（rollback 後 notif 已過期，重新鎖定）
            notif2 = db.execute(
                select(BookingNotification)
                .where(BookingNotification.id == notification_id)
                .with_for_update()
            ).scalar_one_or_none()
            if notif2 is not None:
                notif2.status = NOTIFY_FAILED
                notif2.attempt_count = (notif2.attempt_count or 0) + 1
                notif2.last_error = type(exc).__name__[:255]
                notif2.updated_at = now
                db.commit()
            return NotificationResult(
                notification_id, "failed", "push_error",
                error_type=type(exc).__name__,
            )

        notif.status = NOTIFY_SENT
        notif.sent_at = now
        notif.attempt_count = (notif.attempt_count or 0) + 1
        notif.updated_at = now
        # 後扣：推播成功後才計量本月推播額度（只計實際送出者）；
        # 與標 sent 同交易單一 commit（每筆 2 commits → 1）。
        push_quota_svc.consume_push_in_txn(db, notif.tenant_id, now=now)
        db.commit()
        return NotificationResult(notification_id, "sent", "pushed")


def send_due_notifications(
    *,
    session_factory: sessionmaker = SessionLocal,
    push_client: LinePushClient | None = None,
    apply: bool = False,
    limit: int | None = None,
    now: datetime.datetime | None = None,
) -> list[NotificationResult]:
    """掃描到期通知並（apply 時）推播；回傳每筆結果。"""
    client = push_client or HttpLinePushClient()
    effective_now = now or _utcnow()
    effective_limit = (
        limit if limit is not None else settings.notification_max_per_run
    )
    with session_factory() as db:
        ids = _due_notification_ids(db, now=effective_now, limit=effective_limit)
    return [
        _process_one(
            session_factory,
            client,
            notification_id=nid,
            apply=apply,
            now=effective_now,
        )
        for nid in ids
    ]


def write_report(
    results: list[NotificationResult], *, apply: bool, out: TextIO
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
        description="Send due booking-change notifications via LINE push."
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
        help="Push notifications and mark them sent.",
    )
    parser.set_defaults(apply=False)
    parser.add_argument(
        "--limit", type=_positive_int, help="Max due notifications to process."
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
    results = send_due_notifications(
        session_factory=session_factory,
        push_client=push_client,
        apply=args.apply,
        limit=args.limit,
    )
    write_report(results, apply=args.apply, out=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
