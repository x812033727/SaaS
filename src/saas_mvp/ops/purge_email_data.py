"""清理過期 Email 派送稽核紀錄與已失效 token。"""

from __future__ import annotations

import argparse
import datetime
import sys
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import sessionmaker

from saas_mvp.config import settings
from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.email_delivery import (
    EMAIL_CANCELED,
    EMAIL_FAILED,
    EMAIL_SENT,
    EmailDelivery,
)
from saas_mvp.models.email_token import EmailToken


@dataclass(frozen=True)
class PurgeEmailResult:
    deliveries_purged: int
    tokens_purged: int
    dry_run: bool

    @property
    def total(self) -> int:
        return self.deliveries_purged + self.tokens_purged


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def purge_email_data(
    *,
    session_factory: sessionmaker = SessionLocal,
    apply: bool = False,
    delivery_days: int | None = None,
    token_days: int | None = None,
    limit: int = 10000,
    now: datetime.datetime | None = None,
) -> PurgeEmailResult:
    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    delivery_cutoff = effective_now - datetime.timedelta(
        days=delivery_days or settings.email_delivery_retention_days
    )
    token_cutoff = effective_now - datetime.timedelta(
        days=token_days or settings.email_token_retention_days
    )
    with session_factory() as db:
        delivery_ids = list(db.execute(
            select(EmailDelivery.id)
            .where(
                EmailDelivery.status.in_((EMAIL_SENT, EMAIL_FAILED, EMAIL_CANCELED)),
                EmailDelivery.updated_at < delivery_cutoff,
            )
            .order_by(EmailDelivery.id)
            .limit(limit)
        ).scalars())
        remaining = max(0, limit - len(delivery_ids))
        token_ids = list(db.execute(
            select(EmailToken.id)
            .where(or_(
                EmailToken.expires_at < token_cutoff,
                EmailToken.used_at < token_cutoff,
            ))
            .order_by(EmailToken.id)
            .limit(remaining)
        ).scalars()) if remaining else []
        if apply:
            if delivery_ids:
                db.execute(delete(EmailDelivery).where(EmailDelivery.id.in_(delivery_ids)))
            if token_ids:
                db.execute(delete(EmailToken).where(EmailToken.id.in_(token_ids)))
            db.commit()
        else:
            db.rollback()
        return PurgeEmailResult(len(delivery_ids), len(token_ids), not apply)


def main(argv: list[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(description="Purge expired email data.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delivery-days", type=_positive_int)
    parser.add_argument("--token-days", type=_positive_int)
    parser.add_argument("--limit", type=_positive_int, default=10000)
    args = parser.parse_args(argv)
    import_all_models()
    result = purge_email_data(
        apply=args.apply,
        delivery_days=args.delivery_days,
        token_days=args.token_days,
        limit=args.limit,
    )
    verb = "would_purge" if result.dry_run else "purged"
    print(
        f"summary {verb}_total={result.total} "
        f"deliveries={result.deliveries_purged} tokens={result.tokens_purged}",
        file=stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
