"""Backfill booking_customers.display_name from LINE profile API.

LINE webhook event.source 只給 userId，舊資料的 display_name 因此一直空白。
本腳本對 display_name 為空的顧客逐筆呼叫 `GET /v2/bot/profile/{userId}` 補名字，
讓店家後台能核對是誰預約。LINE profile 僅對「已加 bot 好友」者回名字，非好友/
封鎖回 404 → 該筆跳過（UI 仍有 line_user_id 可核對）。

Usage:
    python -m saas_mvp.ops.backfill_customer_display_name --dry-run
    python -m saas_mvp.ops.backfill_customer_display_name --apply --limit 50
    python -m saas_mvp.ops.backfill_customer_display_name --apply --tenant-id 2
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Callable, TextIO

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.line_client import HttpLineProfileClient, LineProfileClient
from saas_mvp.models.customer import Customer
from saas_mvp.models.line_channel_config import LineChannelConfig


@dataclass(frozen=True)
class BackfillResult:
    customer_id: int
    tenant_id: int
    status: str
    reason: str
    error_type: str | None = None

    def to_line(self) -> str:
        parts = [
            f"customer_id={self.customer_id}",
            f"tenant_id={self.tenant_id}",
            f"status={self.status}",
            f"reason={self.reason}",
        ]
        if self.error_type:
            parts.append(f"error_type={self.error_type}")
        return " ".join(parts)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _nonneg_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _candidate_customer_ids(
    db: Session,
    *,
    tenant_id: int | None,
    limit: int | None,
) -> list[int]:
    """display_name 為 NULL 或空字串的顧客 id（依 id 升冪，可選限 tenant / 上限）。"""
    stmt = (
        select(Customer.id)
        .where(or_(Customer.display_name.is_(None), Customer.display_name == ""))
        .order_by(Customer.id.asc())
    )
    if tenant_id is not None:
        stmt = stmt.where(Customer.tenant_id == tenant_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars())


def _access_token_by_tenant(db: Session) -> dict[int, str]:
    """一次取出所有 tenant 的解密 access token，避免逐筆重複解密/查詢。"""
    tokens: dict[int, str] = {}
    for cfg in db.execute(select(LineChannelConfig)).scalars():
        try:
            tokens[cfg.tenant_id] = cfg.access_token
        except Exception:  # noqa: BLE001 - 解密失敗的 tenant 視為無 token，跳過
            continue
    return tokens


def _process_one(
    session_factory: sessionmaker,
    profile_client: LineProfileClient,
    *,
    customer_id: int,
    token_by_tenant: dict[int, str],
    apply: bool,
) -> BackfillResult:
    with session_factory() as db:
        cust = db.get(Customer, customer_id)
        if cust is None:
            return BackfillResult(customer_id, 0, "skipped", "not_found")
        if cust.display_name:
            return BackfillResult(customer_id, cust.tenant_id, "skipped", "already_set")
        if not cust.line_user_id:
            return BackfillResult(
                customer_id, cust.tenant_id, "skipped", "no_line_user_id"
            )

        token = token_by_tenant.get(cust.tenant_id)
        if not token:
            return BackfillResult(
                customer_id, cust.tenant_id, "skipped", "no_line_config"
            )

        try:
            profile = profile_client.get_profile(
                cust.line_user_id, access_token=token
            )
        except Exception as exc:  # noqa: BLE001 - per-row failure must not stop batch
            db.rollback()
            return BackfillResult(
                customer_id,
                cust.tenant_id,
                "failed",
                "profile_error",
                error_type=type(exc).__name__,
            )

        display_name = profile.display_name if profile else None
        if not display_name:
            db.rollback()
            return BackfillResult(
                customer_id, cust.tenant_id, "skipped", "display_name_missing"
            )

        if not apply:
            db.rollback()
            return BackfillResult(customer_id, cust.tenant_id, "updated", "dry_run")

        cust.display_name = display_name
        try:
            db.commit()
        except Exception as exc:  # noqa: BLE001 - keep the rest of the batch moving
            db.rollback()
            return BackfillResult(
                customer_id,
                cust.tenant_id,
                "failed",
                "commit_error",
                error_type=type(exc).__name__,
            )
        return BackfillResult(customer_id, cust.tenant_id, "updated", "applied")


def backfill_customer_display_names(
    *,
    session_factory: sessionmaker = SessionLocal,
    profile_client: LineProfileClient | None = None,
    apply: bool = False,
    limit: int | None = None,
    tenant_id: int | None = None,
    sleep_seconds: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> list[BackfillResult]:
    """Run the backfill and return per-customer results.

    Dry-run still calls LINE profile API（驗證可補名字），但不 commit。
    Apply mode 一次 commit 一筆。每筆之間 sleep_seconds 節流，避免觸 LINE rate limit。
    """
    # standalone 入口：確保所有 model 已註冊，否則 Tenant→User 等字串 relationship 解析失敗。
    import_all_models()
    client = profile_client or HttpLineProfileClient()
    with session_factory() as db:
        customer_ids = _candidate_customer_ids(
            db, tenant_id=tenant_id, limit=limit
        )
        token_by_tenant = _access_token_by_tenant(db)

    results: list[BackfillResult] = []
    for idx, current_id in enumerate(customer_ids):
        if idx > 0 and sleep_seconds > 0:
            sleep(sleep_seconds)
        results.append(
            _process_one(
                session_factory,
                client,
                customer_id=current_id,
                token_by_tenant=token_by_tenant,
                apply=apply,
            )
        )
    return results


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
        f"failed={counts['failed']}",
        file=out,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill booking_customers.display_name via LINE profile API."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_false",
        dest="apply",
        help="Call profile API and report what would update; do not commit. Default.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        dest="apply",
        help="Commit display_name updates.",
    )
    parser.set_defaults(apply=False)
    parser.add_argument(
        "--limit", type=_positive_int, help="Max blank-name customers to scan."
    )
    parser.add_argument(
        "--tenant-id", type=_positive_int, help="Only process one tenant."
    )
    parser.add_argument(
        "--sleep-ms",
        type=_nonneg_int,
        default=200,
        help="Throttle between profile API calls (milliseconds). Default 200.",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    session_factory: sessionmaker = SessionLocal,
    profile_client: LineProfileClient | None = None,
    stdout: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    results = backfill_customer_display_names(
        session_factory=session_factory,
        profile_client=profile_client,
        apply=args.apply,
        limit=args.limit,
        tenant_id=args.tenant_id,
        sleep_seconds=args.sleep_ms / 1000.0,
    )
    write_report(results, apply=args.apply, out=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
