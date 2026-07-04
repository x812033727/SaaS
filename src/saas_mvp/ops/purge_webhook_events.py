"""清理過期的 LINE webhook 冪等事件（line_webhook_events）。

Usage:
    python -m saas_mvp.ops.purge_webhook_events --dry-run
    python -m saas_mvp.ops.purge_webhook_events --apply --days 30 --limit 10000

設計（比照 ops/send_due_reminders.py）：
  * argparse --dry-run（預設）/ --apply；session_factory 可注入供測試。
  * 刪除條件：status=processed 且 processed_at 早於 now - days
    （--days 預設 settings.webhook_event_ttl_days）。
  * --include-failed：一併刪除「已達重試上限」且 updated_at 早於門檻的 failed 事件
    （attempt_count >= settings.webhook_max_attempts；未達上限者保留，仍可能重試）。
  * --limit 保護單次刪除量（子查詢取 id 再刪，SQLite/PG 皆可用）。
  * 全域清理（跨租戶）；冪等表僅為去重用途，過期資料無業務價值。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from saas_mvp.config import settings
from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.line_webhook_event import (
    LineWebhookEvent,
    LineWebhookEventStatus,
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass(frozen=True)
class PurgeResult:
    processed_purged: int
    failed_purged: int
    dry_run: bool

    @property
    def total(self) -> int:
        return self.processed_purged + self.failed_purged


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _candidate_ids(
    db,
    *,
    cutoff: datetime.datetime,
    include_failed: bool,
    limit: int,
) -> tuple[list[int], list[int]]:
    """回傳 (processed 待刪 id, failed 待刪 id)，合計不超過 limit。"""
    processed_stmt = (
        select(LineWebhookEvent.id)
        .where(
            LineWebhookEvent.status == LineWebhookEventStatus.PROCESSED.value,
            LineWebhookEvent.processed_at.is_not(None),
            LineWebhookEvent.processed_at < cutoff,
        )
        .order_by(LineWebhookEvent.id.asc())
        .limit(limit)
    )
    processed_ids = list(db.execute(processed_stmt).scalars())

    failed_ids: list[int] = []
    remaining = limit - len(processed_ids)
    if include_failed and remaining > 0:
        failed_stmt = (
            select(LineWebhookEvent.id)
            .where(
                LineWebhookEvent.status == LineWebhookEventStatus.FAILED.value,
                LineWebhookEvent.attempt_count >= settings.webhook_max_attempts,
                LineWebhookEvent.updated_at < cutoff,
            )
            .order_by(LineWebhookEvent.id.asc())
            .limit(remaining)
        )
        failed_ids = list(db.execute(failed_stmt).scalars())
    return processed_ids, failed_ids


def purge_webhook_events(
    *,
    session_factory: sessionmaker = SessionLocal,
    apply: bool = False,
    days: int | None = None,
    include_failed: bool = False,
    limit: int = 10000,
    now: datetime.datetime | None = None,
) -> PurgeResult:
    """刪除過期 webhook 冪等事件；dry-run 只計數不刪。"""
    effective_days = days if days is not None else settings.webhook_event_ttl_days
    effective_now = now or _utcnow()
    cutoff = effective_now - datetime.timedelta(days=effective_days)

    with session_factory() as db:
        processed_ids, failed_ids = _candidate_ids(
            db, cutoff=cutoff, include_failed=include_failed, limit=limit
        )
        if not apply:
            db.rollback()
            return PurgeResult(len(processed_ids), len(failed_ids), dry_run=True)

        all_ids = processed_ids + failed_ids
        if all_ids:
            db.execute(
                delete(LineWebhookEvent).where(LineWebhookEvent.id.in_(all_ids))
            )
        db.commit()
        return PurgeResult(len(processed_ids), len(failed_ids), dry_run=False)


def write_report(result: PurgeResult, *, out: TextIO) -> None:
    mode = "dry_run" if result.dry_run else "apply"
    verb = "would_purge" if result.dry_run else "purged"
    print(f"mode={mode}", file=out)
    print(
        f"summary {verb}_total={result.total} "
        f"processed={result.processed_purged} "
        f"failed={result.failed_purged}",
        file=out,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Purge expired LINE webhook idempotency events."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_false",
        dest="apply",
        help="Report what would be purged; do not delete. Default.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        dest="apply",
        help="Delete expired webhook events.",
    )
    parser.set_defaults(apply=False)
    parser.add_argument(
        "--days",
        type=_positive_int,
        help="Retention days (default: SAAS_WEBHOOK_EVENT_TTL_DAYS).",
    )
    parser.add_argument(
        "--include-failed",
        action="store_true",
        help="Also purge failed events that exhausted retry attempts.",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=10000,
        help="Max rows to delete per run (default 10000).",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    session_factory: sessionmaker = SessionLocal,
    stdout: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    # 確保 SQLAlchemy registry 完整：standalone（python -m / cron）執行時
    # 各 model 未必都被 import，relationship 字串（如 'Tenant'）會解析失敗。
    import_all_models()
    result = purge_webhook_events(
        session_factory=session_factory,
        apply=args.apply,
        days=args.days,
        include_failed=args.include_failed,
        limit=args.limit,
    )
    write_report(result, out=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
