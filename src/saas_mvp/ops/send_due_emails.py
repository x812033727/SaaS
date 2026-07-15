"""派送 Email outbox；預設 dry-run，scheduler 使用 --apply。"""

from __future__ import annotations

import argparse
import datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.email_delivery import EMAIL_PENDING, EmailDelivery
from saas_mvp.services import email_delivery as delivery_svc
from saas_mvp.services.mailer import Mailer, get_mailer


def send_due_emails(
    *,
    apply: bool = False,
    limit: int = 100,
    now=None,
    session_factory: sessionmaker = SessionLocal,
    mailer: Mailer | None = None,
) -> list[tuple[int, str]]:
    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    with session_factory() as db:
        ids = delivery_svc.due_ids(db, now=effective_now, limit=limit)
    results: list[tuple[int, str]] = []
    for delivery_id in ids:
        if not apply:
            results.append((delivery_id, "would_send"))
            continue
        with session_factory() as db:
            row = db.execute(
                select(EmailDelivery).where(EmailDelivery.id == delivery_id).with_for_update()
            ).scalar_one_or_none()
            if row is None or row.status != EMAIL_PENDING:
                results.append((delivery_id, "skipped"))
                continue
            effective_mailer = mailer or get_mailer(db)
            results.append((delivery_id, delivery_svc.attempt(db, row, effective_mailer, now=effective_now)))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Send queued platform emails.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    import_all_models()
    results = send_due_emails(apply=args.apply, limit=max(1, args.limit))
    for delivery_id, result in results:
        print(f"email_delivery_id={delivery_id} status={result}")
    print(f"summary total={len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
