"""重試開立失敗的電子發票（C2）。

Usage:
    python -m saas_mvp.ops.retry_failed_invoices --dry-run
    python -m saas_mvp.ops.retry_failed_invoices --apply [--max-age-hours 72]

掃 failed 與「pending 超過 10 分鐘」(首開途中死掉)的發票列重試;
超過 --max-age-hours 的不再重試(避免無限重打壞單,留人工處置)。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.invoice import INVOICE_FAILED, INVOICE_PENDING, Invoice


@dataclass(frozen=True)
class RetryResult:
    invoice_id: int
    relate_number: str
    status: str  # issued | failed | would_retry | skipped_too_old

    def to_line(self) -> str:
        return (
            f"invoice_id={self.invoice_id} relate={self.relate_number} "
            f"status={self.status}"
        )


def retry_failed_invoices(
    *,
    session_factory: sessionmaker = SessionLocal,
    issuer=None,
    apply: bool = False,
    max_age_hours: int = 72,
    now: datetime.datetime | None = None,
) -> list[RetryResult]:
    from saas_mvp.services.invoices import _attempt_issue

    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    pending_cutoff = effective_now - datetime.timedelta(minutes=10)
    age_cutoff = effective_now - datetime.timedelta(hours=max_age_hours)
    results: list[RetryResult] = []

    with session_factory() as db:
        rows = db.execute(
            select(Invoice).where(
                Invoice.status.in_((INVOICE_FAILED, INVOICE_PENDING))
            ).order_by(Invoice.id)
        ).scalars().all()
        for row in rows:
            created = row.created_at
            n_pending = pending_cutoff.replace(tzinfo=None)
            n_age = age_cutoff.replace(tzinfo=None)
            cmp_pending = n_pending if (created and created.tzinfo is None) else pending_cutoff
            cmp_age = n_age if (created and created.tzinfo is None) else age_cutoff
            if row.status == INVOICE_PENDING and created and created >= cmp_pending:
                continue  # 剛入列,首開流程還在跑
            if created and created < cmp_age:
                results.append(
                    RetryResult(row.id, row.relate_number, "skipped_too_old")
                )
                continue
            if not apply:
                results.append(RetryResult(row.id, row.relate_number, "would_retry"))
                continue
            _attempt_issue(db, row, issuer=issuer)
            results.append(RetryResult(row.id, row.relate_number, row.status))
    return results


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    import_all_models()
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True)
    group.add_argument("--apply", action="store_true")
    parser.add_argument("--max-age-hours", type=int, default=72)
    args = parser.parse_args(argv)

    results = retry_failed_invoices(apply=args.apply, max_age_hours=args.max_age_hours)
    for r in results:
        print(r.to_line(), file=out)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] total={len(results)}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
