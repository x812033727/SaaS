"""LINE webhook 健康檢查（F3,M2-004）— 純唯讀報表 + 告警。

Usage:
    python -m saas_mvp.ops.check_webhook_health [--stale-minutes 5] [--failed-ratio 0.1]

檢查:
  1. pending 超時:卡超過 --stale-minutes 的事件(tenant/id/stage/attempt)
     — outbox 重放 cron 會處理,此處是「重放也救不回或積壓」的告警面。
  2. 近 24h failed 比率超過 --failed-ratio。

異常 → obs.alerts.capture_alert(未設 Sentry 退化 error log)+ exit 1。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from typing import TextIO

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.line_webhook_event import (
    LineWebhookEvent,
    LineWebhookEventStatus,
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def check_webhook_health(
    *,
    session_factory: sessionmaker = SessionLocal,
    stale_minutes: int = 5,
    failed_ratio: float = 0.1,
    now: datetime.datetime | None = None,
) -> dict:
    effective_now = now or _utcnow()
    cutoff = effective_now - datetime.timedelta(minutes=stale_minutes)
    day_ago = effective_now - datetime.timedelta(hours=24)

    with session_factory() as db:
        pending = db.execute(
            select(LineWebhookEvent).where(
                LineWebhookEvent.status == LineWebhookEventStatus.PENDING.value
            )
        ).scalars().all()
        naive_cutoff = cutoff.replace(tzinfo=None)
        stale = []
        for r in pending:
            upd = r.updated_at
            if upd is None:
                continue
            cmp = naive_cutoff if upd.tzinfo is None else cutoff
            if upd < cmp:
                stale.append({
                    "id": r.id,
                    "tenant_id": r.tenant_id,
                    "webhook_event_id": r.webhook_event_id,
                    "event_type": getattr(r, "event_type", None),
                    "last_stage": r.last_stage,
                    "attempt_count": r.attempt_count,
                })

        naive_day_ago = day_ago.replace(tzinfo=None)

        def _count(status: str) -> int:
            total = 0
            for tz_aware in (True, False):
                bound = day_ago if tz_aware else naive_day_ago
                try:
                    total = db.execute(
                        select(func.count(LineWebhookEvent.id)).where(
                            LineWebhookEvent.status == status,
                            LineWebhookEvent.created_at >= bound,
                        )
                    ).scalar_one()
                    break
                except Exception:  # noqa: BLE001 — tz 比較保底
                    continue
            return int(total)

        failed_24h = _count(LineWebhookEventStatus.FAILED.value)
        processed_24h = _count(LineWebhookEventStatus.PROCESSED.value)

    total_24h = failed_24h + processed_24h
    ratio = (failed_24h / total_24h) if total_24h else 0.0
    return {
        "stale_pending": stale,
        "failed_24h": failed_24h,
        "processed_24h": processed_24h,
        "failed_ratio": round(ratio, 3),
        "ratio_exceeded": total_24h >= 10 and ratio > failed_ratio,
    }


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    import_all_models()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stale-minutes", type=int, default=5)
    parser.add_argument("--failed-ratio", type=float, default=0.1)
    args = parser.parse_args(argv)

    report = check_webhook_health(
        stale_minutes=args.stale_minutes, failed_ratio=args.failed_ratio
    )
    problems = 0
    for row in report["stale_pending"]:
        print(
            f"STALE_PENDING id={row['id']} tenant={row['tenant_id']} "
            f"event={row['webhook_event_id']} type={row['event_type']} "
            f"stage={row['last_stage']} attempts={row['attempt_count']}",
            file=out,
        )
        problems += 1
    if report["ratio_exceeded"]:
        print(
            f"FAILED_RATIO {report['failed_ratio']} "
            f"(failed={report['failed_24h']}/total={report['failed_24h'] + report['processed_24h']})",
            file=out,
        )
        problems += 1

    print(
        f"[webhook-health] stale={len(report['stale_pending'])} "
        f"failed_24h={report['failed_24h']} ratio={report['failed_ratio']}",
        file=out,
    )
    if problems:
        from saas_mvp.obs.alerts import capture_alert

        capture_alert(
            f"webhook health: stale_pending={len(report['stale_pending'])} "
            f"failed_ratio={report['failed_ratio']}"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
