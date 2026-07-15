"""收敛候補 offer 並在有名額時遞補下一位。

Usage:
    python -m saas_mvp.ops.process_waitlists --dry-run
    python -m saas_mvp.ops.process_waitlists --apply --limit 200

每分鐘執行：已通知但逾時未預約者轉 expired，同時段仍有名額時
依排隊順序通知下一位。也會補捉容量加開、通知短暫失敗等情形。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.services import waitlist as waitlist_svc


@dataclass(frozen=True)
class ProcessResult:
    tenant_id: int
    slot_id: int
    status: str

    def to_line(self) -> str:
        return (
            f"tenant_id={self.tenant_id} slot_id={self.slot_id} "
            f"status={self.status}"
        )


def process_waitlists(
    *,
    session_factory: sessionmaker = SessionLocal,
    push_client=None,
    apply: bool = False,
    now: datetime.datetime | None = None,
    limit: int = 200,
) -> list[ProcessResult]:
    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    with session_factory() as db:
        targets = waitlist_svc.candidate_slots(
            db, now=effective_now, limit=max(1, min(limit, 1000))
        )

    results: list[ProcessResult] = []
    for tenant_id, slot_id in targets:
        if not apply:
            results.append(ProcessResult(tenant_id, slot_id, "would_check"))
            continue
        with session_factory() as db:
            offered = waitlist_svc.notify_next_for_slot_best_effort(
                db,
                tenant_id=tenant_id,
                slot_id=slot_id,
                push_client=push_client,
                now=effective_now,
            )
        results.append(
            ProcessResult(tenant_id, slot_id, "offered" if offered else "checked")
        )
    return results


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    import_all_models()
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True)
    group.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv)

    results = process_waitlists(apply=args.apply, limit=args.limit)
    for result in results:
        print(result.to_line(), file=out)
    mode = "APPLY" if args.apply else "DRY-RUN"
    offered = sum(result.status == "offered" for result in results)
    print(f"[{mode}] total={len(results)} offered={offered}", file=out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
