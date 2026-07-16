"""重放卡住的 LINE webhook 事件（A0.2 outbox）。

Usage:
    python -m saas_mvp.ops.retry_stuck_webhook_events --dry-run
    python -m saas_mvp.ops.retry_stuck_webhook_events --apply [--stuck-minutes 10]

背景：webhook handler 回 200 後事件在 in-process BackgroundTasks 處理；
worker 在處理中死掉時 LINE 不會重送（已收到 200），該事件此前直接蒸發。
0011 起 claim 時把原始 payload 落盤 — 本腳本掃「pending 且卡超過 N 分鐘、
attempt 未達上限」的列重放（line_webhook.replay_stored_event）。

注意：replyToken 重放時多半已過期，回覆可能失敗（標 failed 不再重試），
但服務層副作用（建單等）會補齊 — 這是重放的核心價值。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from saas_mvp.config import settings
from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.line_webhook_event import (
    LineWebhookEvent,
    LineWebhookEventStatus,
)


@dataclass(frozen=True)
class ReplayResult:
    event_row_id: int
    tenant_id: int
    status: str  # processed | failed | skipped | would_replay

    def to_line(self) -> str:
        return (
            f"event_row_id={self.event_row_id} tenant_id={self.tenant_id} "
            f"status={self.status}"
        )


def retry_stuck_events(
    *,
    session_factory: sessionmaker = SessionLocal,
    apply: bool = False,
    stuck_minutes: int = 10,
    limit: int = 100,
    now: datetime.datetime | None = None,
    line_client=None,
    profile_client=None,
    translator=None,
) -> list[ReplayResult]:
    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = effective_now - datetime.timedelta(minutes=stuck_minutes)
    results: list[ReplayResult] = []

    with session_factory() as db:
        naive_cutoff = cutoff.replace(tzinfo=None)
        rows = db.execute(
            select(LineWebhookEvent)
            .where(
                # 卡住的 PENDING(worker 死於原始處理)或卡住的 PROCESSING
                # (retry_stuck 認領後死於重放中途)皆為重放候選。
                LineWebhookEvent.status.in_(
                    [
                        LineWebhookEventStatus.PENDING.value,
                        LineWebhookEventStatus.PROCESSING.value,
                    ]
                ),
                LineWebhookEvent.payload_json.is_not(None),
                LineWebhookEvent.attempt_count < settings.webhook_max_attempts,
            )
            .order_by(LineWebhookEvent.id)
            .limit(limit)
        ).scalars().all()
        stuck = []
        for r in rows:
            upd = r.updated_at
            cmp = naive_cutoff if (upd is not None and upd.tzinfo is None) else cutoff
            if upd is not None and upd < cmp:
                stuck.append((r.id, r.tenant_id, r.status, r.attempt_count))

    for row_id, tenant_id, obs_status, obs_attempt in stuck:
        if not apply:
            results.append(ReplayResult(row_id, tenant_id, "would_replay"))
            continue
        with session_factory() as db:
            # 原子認領:以 (status, attempt_count) 為 CAS 守衛把列改 PROCESSING、
            # attempt_count+1。取代原本的 SELECT..FOR UPDATE —— 後者在 replay 內部
            # 多次 commit 時提早釋放列鎖,讓重疊的 cron 實例搶到同一列重放(雙重
            # 副作用)。CAS 下並發實例因 status/attempt 已變而 rowcount=0,只有一個
            # 得以重放;認領後崩潰的列以 PROCESSING+舊 updated_at 於下輪被重新認領,
            # 重放的建單側效由 reservations.source_webhook_event_id 冪等鍵保證不重複。
            claim_now = datetime.datetime.now(datetime.timezone.utc)
            claimed = db.execute(
                update(LineWebhookEvent)
                .where(
                    LineWebhookEvent.id == row_id,
                    LineWebhookEvent.status == obs_status,
                    LineWebhookEvent.attempt_count == obs_attempt,
                )
                .values(
                    status=LineWebhookEventStatus.PROCESSING.value,
                    attempt_count=obs_attempt + 1,
                    updated_at=claim_now,
                )
            ).rowcount
            db.commit()
            if claimed != 1:
                results.append(ReplayResult(row_id, tenant_id, "skipped"))
                continue
            row = db.get(LineWebhookEvent, row_id)
            if row is None:  # pragma: no cover - 認領後被刪除,防禦
                results.append(ReplayResult(row_id, tenant_id, "skipped"))
                continue
            from saas_mvp.routers.line_webhook import replay_stored_event

            status = replay_stored_event(
                db, row,
                line_client=line_client,
                profile_client=profile_client,
                translator=translator,
            )
            results.append(ReplayResult(row_id, tenant_id, status))
    return results


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    import_all_models()
    # scheduler 行程無 app lifespan:主動接上 redis 發佈端,否則本行程的
    # publish_event 只 deliver_local(無訂閱者)⇒ SSE 通知靜默丟失。
    from saas_mvp.services.events import enable_redis_publisher
    enable_redis_publisher()
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True)
    group.add_argument("--apply", action="store_true")
    parser.add_argument("--stuck-minutes", type=int, default=10)
    args = parser.parse_args(argv)

    results = retry_stuck_events(apply=args.apply, stuck_minutes=args.stuck_minutes)
    for r in results:
        print(r.to_line(), file=out)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] total={len(results)}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
