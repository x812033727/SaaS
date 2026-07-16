"""Reverify stale, previously-valid LINE bot credentials.

The job is intentionally database-driven and single-run. supercronic owns the
schedule, while per-row verification reuses the same service-layer rate budget
as manual verification.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from saas_mvp.config import settings
from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.line_client import HttpLineBotInfoClient, LineBotInfoClient
from saas_mvp.models.line_channel_config import CredentialStatus, LineChannelConfig
from saas_mvp.services.line_config import verify_config_row

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReverifyReport:
    scanned: int = 0
    valid: int = 0
    invalid: int = 0
    error: int = 0
    conflict: int = 0
    rate_limited: int = 0
    circuit_open: bool = False


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def reverify_stale_credentials(
    *,
    session_factory: sessionmaker = SessionLocal,
    bot_info_client: LineBotInfoClient | None = None,
    threshold_hours: int | None = None,
    batch_size: int | None = None,
    throttle_seconds: float = 5.0,
    circuit_breaker_failures: int = 10,
    now: datetime.datetime | None = None,
    sleep=time.sleep,
) -> ReverifyReport:
    effective_now = now or _utcnow()
    cutoff = effective_now - datetime.timedelta(
        hours=threshold_hours or settings.line_credential_reverify_hours
    )
    limit = batch_size or settings.line_credential_reverify_batch_size
    client = bot_info_client or HttpLineBotInfoClient()

    with session_factory() as db:
        candidates = db.execute(
            select(LineChannelConfig.id)
            .where(
                LineChannelConfig.credential_status == CredentialStatus.VALID.value,
                LineChannelConfig.credential_checked_at.is_not(None),
                LineChannelConfig.credential_checked_at < cutoff,
            )
            .order_by(LineChannelConfig.credential_checked_at, LineChannelConfig.id)
            .limit(limit)
        ).scalars().all()

    counts = {status.value: 0 for status in CredentialStatus}
    counts["rate_limited"] = 0
    scanned = 0
    consecutive_failures = 0
    circuit_open = False

    for config_id in candidates:
        with session_factory() as db:
            cfg = db.execute(
                select(LineChannelConfig)
                .where(
                    LineChannelConfig.id == config_id,
                    LineChannelConfig.credential_status == CredentialStatus.VALID.value,
                )
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if cfg is None:
                continue
            result = verify_config_row(db, cfg, bot_info_client=client)
            scanned += 1
            counts[result] = counts.get(result, 0) + 1
            if result in {CredentialStatus.ERROR.value, "rate_limited"}:
                consecutive_failures += 1
            else:
                consecutive_failures = 0

        if consecutive_failures >= circuit_breaker_failures:
            logger.error(
                "LINE credential reverify circuit breaker opened after %d failures",
                consecutive_failures,
            )
            circuit_open = True
            break
        if throttle_seconds:
            sleep(throttle_seconds)

    return ReverifyReport(
        scanned=scanned,
        valid=counts[CredentialStatus.VALID.value],
        invalid=counts[CredentialStatus.INVALID.value],
        error=counts[CredentialStatus.ERROR.value],
        conflict=counts[CredentialStatus.CONFLICT.value],
        rate_limited=counts["rate_limited"],
        circuit_open=circuit_open,
    )


def main(argv: list[str] | None = None) -> int:
    import_all_models()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold-hours", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--throttle-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    report = reverify_stale_credentials(
        threshold_hours=args.threshold_hours,
        batch_size=args.batch_size,
        throttle_seconds=args.throttle_seconds,
    )
    print(
        "[line-credential-reverify] "
        f"scanned={report.scanned} valid={report.valid} invalid={report.invalid} "
        f"error={report.error} conflict={report.conflict} "
        f"rate_limited={report.rate_limited} circuit_open={int(report.circuit_open)}"
    )
    return 1 if report.circuit_open else 0


if __name__ == "__main__":
    raise SystemExit(main())
