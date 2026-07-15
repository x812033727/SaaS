"""重試到期的 Google Calendar 同步工作；預設 dry-run。"""

from __future__ import annotations

import argparse
import datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.gcal_sync_job import GCAL_SYNC_PENDING, GcalSyncJob
from saas_mvp.services import gcal as gcal_svc


def retry_gcal_syncs(
    *,
    apply: bool = False,
    limit: int = 100,
    now=None,
    session_factory: sessionmaker = SessionLocal,
    client=None,
) -> list[tuple[int, str]]:
    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    with session_factory() as db:
        ids = gcal_svc.due_ids(db, now=effective_now, limit=limit)

    results: list[tuple[int, str]] = []
    for job_id in ids:
        if not apply:
            results.append((job_id, "would_sync"))
            continue
        with session_factory() as db:
            row = db.execute(
                select(GcalSyncJob).where(GcalSyncJob.id == job_id).with_for_update()
            ).scalar_one_or_none()
            if row is None or row.status != GCAL_SYNC_PENDING:
                results.append((job_id, "skipped"))
                continue
            result = gcal_svc.attempt_sync(
                db, row, client=client, now=effective_now
            )
            db.commit()
            results.append((job_id, result))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry queued Google Calendar syncs.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    import_all_models()
    results = retry_gcal_syncs(apply=args.apply, limit=max(1, args.limit))
    for job_id, result in results:
        print(f"gcal_sync_job_id={job_id} status={result}")
    print(f"summary total={len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
