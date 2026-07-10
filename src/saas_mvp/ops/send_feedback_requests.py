"""滿意度調查派發（A3.3）— 服務結束後推 1–5 分 quick-reply。

Usage:
    python -m saas_mvp.ops.send_feedback_requests --dry-run
    python -m saas_mvp.ops.send_feedback_requests --apply

設計（比照 ops/send_due_reminders.py）：
  * ``feedback.list_due_requests`` 撈「已結束 + confirmed + 未發卷 +
    FEEDBACK_SURVEY 開通」的預約。
  * 冪等：reservation_feedback.reservation_id UNIQUE；「入列 + 推播成功」
    同交易 commit，推播失敗 rollback（下輪重試）。
  * 推播額度：has_push_quota 前檢 + consume_push_in_txn 後扣（只計實際送出）。
  * bot_mode != booking 或無 LINE 設定：跳過（分數 postback 需 booking 對話）。
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.line_client import HttpLinePushClient, LinePushClient
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.services import feedback as feedback_svc
from saas_mvp.services import push_quota as push_quota_svc

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedbackSendResult:
    reservation_id: int
    tenant_id: int
    status: str  # sent | would_send | skipped | failed
    detail: str = ""

    def to_line(self) -> str:
        return (
            f"reservation_id={self.reservation_id} tenant_id={self.tenant_id} "
            f"status={self.status}" + (f" detail={self.detail}" if self.detail else "")
        )


def _score_buttons(reservation_id: int) -> list[tuple[str, str]]:
    return [
        (f"{'⭐' * n} {n} 分", f"action=rate&reservation_id={reservation_id}&score={n}")
        for n in range(1, 6)
    ]


def send_feedback_requests(
    *,
    session_factory: sessionmaker = SessionLocal,
    push_client: LinePushClient | None = None,
    apply: bool = False,
    now: datetime.datetime | None = None,
) -> list[FeedbackSendResult]:
    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    client = push_client or HttpLinePushClient()
    results: list[FeedbackSendResult] = []

    with session_factory() as db:
        due = feedback_svc.list_due_requests(db, now=effective_now)

    for resv, _slot in due:
        with session_factory() as db:
            cfg = db.execute(
                select(LineChannelConfig).where(
                    LineChannelConfig.tenant_id == resv.tenant_id
                )
            ).scalar_one_or_none()
            if cfg is None or (cfg.bot_mode or "translation") != "booking":
                results.append(
                    FeedbackSendResult(resv.id, resv.tenant_id, "skipped", "not_booking_mode")
                )
                continue
            if not apply:
                results.append(
                    FeedbackSendResult(resv.id, resv.tenant_id, "would_send", "dry_run")
                )
                continue
            if not push_quota_svc.has_push_quota(db, resv.tenant_id, now=effective_now):
                results.append(
                    FeedbackSendResult(
                        resv.id, resv.tenant_id, "skipped", "push_allowance_exceeded"
                    )
                )
                continue

            # 入列（未 commit）→ 推播成功才 commit（含額度後扣）；失敗 rollback
            # 下輪重試。reservation_id UNIQUE 擋並發重複入列。
            resv_local = db.get(type(resv), resv.id)
            feedback_svc.mark_requested(db, resv_local)
            try:
                client.push(
                    resv_local.line_user_id,
                    "感謝您的光臨！這次的服務體驗如何？點一下給我們評分：",
                    access_token=cfg.access_token,
                    quick_reply=_score_buttons(resv_local.id),
                )
            except Exception as exc:  # noqa: BLE001 — 單筆失敗不中斷批次
                db.rollback()
                results.append(
                    FeedbackSendResult(
                        resv.id, resv.tenant_id, "failed", type(exc).__name__
                    )
                )
                continue
            push_quota_svc.consume_push_in_txn(db, resv.tenant_id, now=effective_now)
            db.commit()
            results.append(FeedbackSendResult(resv.id, resv.tenant_id, "sent"))
    return results


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    import_all_models()
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True)
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    results = send_feedback_requests(apply=args.apply)
    for r in results:
        print(r.to_line(), file=out)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] total={len(results)}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
