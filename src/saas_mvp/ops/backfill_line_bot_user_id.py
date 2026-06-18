"""Backfill LINE bot userId for existing channel configs.

Usage:
    python -m saas_mvp.ops.backfill_line_bot_user_id --dry-run
    python -m saas_mvp.ops.backfill_line_bot_user_id --apply --limit 50
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from saas_mvp.db import SessionLocal
from saas_mvp.line_client import HttpLineBotInfoClient, LineBotInfoClient
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.tenant import Tenant  # noqa: F401 - resolve SQLAlchemy relationship


@dataclass(frozen=True)
class BackfillResult:
    tenant_id: int
    status: str
    reason: str
    conflict_tenant_id: int | None = None
    error_type: str | None = None

    def to_line(self) -> str:
        parts = [
            f"tenant_id={self.tenant_id}",
            f"status={self.status}",
            f"reason={self.reason}",
        ]
        if self.conflict_tenant_id is not None:
            parts.append(f"conflict_tenant_id={self.conflict_tenant_id}")
        if self.error_type:
            parts.append(f"error_type={self.error_type}")
        return " ".join(parts)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _candidate_tenant_ids(
    db: Session,
    *,
    tenant_id: int | None,
    limit: int | None,
) -> tuple[list[int], BackfillResult | None]:
    if tenant_id is not None:
        cfg = db.execute(
            select(LineChannelConfig).where(LineChannelConfig.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if cfg is None:
            return [], BackfillResult(tenant_id, "skipped", "not_found")
        if cfg.line_bot_user_id is not None:
            return [], BackfillResult(tenant_id, "skipped", "already_set")
        return [tenant_id], None

    stmt = (
        select(LineChannelConfig.tenant_id)
        .where(LineChannelConfig.line_bot_user_id.is_(None))
        .order_by(LineChannelConfig.tenant_id.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars()), None


def _find_conflict_tenant_id(db: Session, user_id: str, tenant_id: int) -> int | None:
    return db.execute(
        select(LineChannelConfig.tenant_id)
        .where(LineChannelConfig.line_bot_user_id == user_id)
        .where(LineChannelConfig.tenant_id != tenant_id)
        .limit(1)
    ).scalar_one_or_none()


def _process_one(
    session_factory: sessionmaker,
    bot_info_client: LineBotInfoClient,
    *,
    tenant_id: int,
    apply: bool,
) -> BackfillResult:
    with session_factory() as db:
        cfg = db.execute(
            select(LineChannelConfig).where(LineChannelConfig.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if cfg is None:
            return BackfillResult(tenant_id, "skipped", "not_found")
        if cfg.line_bot_user_id is not None:
            return BackfillResult(tenant_id, "skipped", "already_set")

        try:
            user_id = bot_info_client.get_user_id(cfg.access_token)
        except Exception as exc:  # noqa: BLE001 - per-row failure must not stop batch
            db.rollback()
            return BackfillResult(
                tenant_id,
                "failed",
                "bot_info_error",
                error_type=type(exc).__name__,
            )

        if not user_id:
            db.rollback()
            return BackfillResult(tenant_id, "failed", "user_id_missing")

        conflict_tenant_id = _find_conflict_tenant_id(db, user_id, tenant_id)
        if conflict_tenant_id is not None:
            db.rollback()
            return BackfillResult(
                tenant_id,
                "conflict",
                "duplicate_line_bot_user_id",
                conflict_tenant_id=conflict_tenant_id,
            )

        if not apply:
            db.rollback()
            return BackfillResult(tenant_id, "updated", "dry_run")

        cfg.line_bot_user_id = user_id
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            with session_factory() as fresh_db:
                conflict_tenant_id = _find_conflict_tenant_id(
                    fresh_db, user_id, tenant_id
                )
            return BackfillResult(
                tenant_id,
                "conflict",
                "duplicate_line_bot_user_id",
                conflict_tenant_id=conflict_tenant_id,
            )
        except Exception as exc:  # noqa: BLE001 - keep the rest of the batch moving
            db.rollback()
            return BackfillResult(
                tenant_id,
                "failed",
                "commit_error",
                error_type=type(exc).__name__,
            )

        return BackfillResult(tenant_id, "updated", "applied")


def backfill_line_bot_user_ids(
    *,
    session_factory: sessionmaker = SessionLocal,
    bot_info_client: LineBotInfoClient | None = None,
    apply: bool = False,
    limit: int | None = None,
    tenant_id: int | None = None,
) -> list[BackfillResult]:
    """Run the backfill and return per-tenant results.

    Dry-run still calls LINE bot/info to verify recoverability, but never flushes
    or commits updates. Apply mode commits one tenant at a time.
    """
    client = bot_info_client or HttpLineBotInfoClient()
    with session_factory() as db:
        tenant_ids, immediate = _candidate_tenant_ids(
            db, tenant_id=tenant_id, limit=limit
        )
    if immediate is not None:
        return [immediate]
    return [
        _process_one(session_factory, client, tenant_id=current_id, apply=apply)
        for current_id in tenant_ids
    ]


def write_report(
    results: list[BackfillResult],
    *,
    apply: bool,
    out: TextIO,
) -> None:
    mode = "apply" if apply else "dry_run"
    print(f"mode={mode}", file=out)
    for result in results:
        print(result.to_line(), file=out)

    counts = Counter(result.status for result in results)
    print(
        "summary "
        f"total={len(results)} "
        f"updated={counts['updated']} "
        f"skipped={counts['skipped']} "
        f"failed={counts['failed']} "
        f"conflict={counts['conflict']}",
        file=out,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill line_channel_configs.line_bot_user_id via LINE bot/info."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_false",
        dest="apply",
        help="Call bot/info and report what would update; do not commit. Default.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        dest="apply",
        help="Commit line_bot_user_id updates.",
    )
    parser.set_defaults(apply=False)
    parser.add_argument("--limit", type=_positive_int, help="Max NULL configs to scan.")
    parser.add_argument("--tenant-id", type=_positive_int, help="Only process one tenant.")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    session_factory: sessionmaker = SessionLocal,
    bot_info_client: LineBotInfoClient | None = None,
    stdout: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    results = backfill_line_bot_user_ids(
        session_factory=session_factory,
        bot_info_client=bot_info_client,
        apply=args.apply,
        limit=args.limit,
        tenant_id=args.tenant_id,
    )
    write_report(results, apply=args.apply, out=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

