"""帳務健康檢查（純報表,唯讀;供 cron 日誌與人工巡檢）。

Usage:
    python -m saas_mvp.ops.check_billing_health

檢查三類異常:
  1. cancel_failed:停扣未確認的訂閱（可能仍在扣卡,retry_cancel_failed
     會自動重試,此處列出供追蹤）。
  2. pending 過期:建立超過 --stale-hours(預設 48)仍未活化的訂閱
     （顧客未完成首期授權,可能卡在金流頁）。
  3. 狀態不一致:訂閱已 failed/cancel_failed 但功能旗標仍 enabled
     （不應發生;發現即列出租戶與功能供人工處置）。

exit code:發現任何異常回 1（cron 可據此告警）,乾淨回 0。
機器面監控另見 obs/business.py 的 /metrics gauges。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.feature_subscription import (
    SUB_CANCEL_FAILED,
    SUB_FAILED,
    SUB_PENDING,
    FeatureSubscription,
)
from saas_mvp.models.tenant_feature import TenantFeature


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def check_billing_health(
    *,
    session_factory: sessionmaker = SessionLocal,
    stale_hours: int = 48,
    now: datetime.datetime | None = None,
) -> dict:
    """回傳 {cancel_failed: [...], stale_pending: [...], inconsistent: [...]}。"""
    effective_now = now or _utcnow()
    cutoff = effective_now - datetime.timedelta(hours=stale_hours)
    with session_factory() as db:
        cancel_failed = list(db.execute(
            select(FeatureSubscription)
            .where(FeatureSubscription.status == SUB_CANCEL_FAILED)
            .order_by(FeatureSubscription.id)
        ).scalars())

        stale_pending = list(db.execute(
            select(FeatureSubscription)
            .where(
                FeatureSubscription.status == SUB_PENDING,
                FeatureSubscription.created_at < cutoff,
            )
            .order_by(FeatureSubscription.id)
        ).scalars())

        # 訂閱已終止但旗標仍開:join TenantFeature（同 tenant + feature）。
        inconsistent = list(db.execute(
            select(FeatureSubscription)
            .join(
                TenantFeature,
                (TenantFeature.tenant_id == FeatureSubscription.tenant_id)
                & (TenantFeature.feature == FeatureSubscription.feature),
            )
            .where(
                FeatureSubscription.status.in_((SUB_FAILED, SUB_CANCEL_FAILED)),
                TenantFeature.enabled.is_(True),
            )
            .order_by(FeatureSubscription.id)
        ).scalars())

        def _row(s: FeatureSubscription) -> dict:
            return {
                "subscription_id": s.id,
                "tenant_id": s.tenant_id,
                "feature": s.feature,
                "trade_no": s.merchant_trade_no,
                "status": s.status,
            }

        return {
            "cancel_failed": [_row(s) for s in cancel_failed],
            "stale_pending": [_row(s) for s in stale_pending],
            "inconsistent": [_row(s) for s in inconsistent],
        }


def write_report(report: dict, *, out: TextIO) -> None:
    for section, rows in report.items():
        print(f"[{section}] count={len(rows)}", file=out)
        for r in rows:
            print(
                f"  subscription_id={r['subscription_id']} "
                f"tenant_id={r['tenant_id']} feature={r['feature']} "
                f"trade_no={r['trade_no']} status={r['status']}",
                file=out,
            )
    total = sum(len(rows) for rows in report.values())
    print(f"summary anomalies={total}", file=out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report billing anomalies (read-only)."
    )
    parser.add_argument(
        "--stale-hours", type=int, default=48,
        help="Pending subscriptions older than this are flagged (default 48).",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    session_factory: sessionmaker = SessionLocal,
    stdout: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    import_all_models()
    report = check_billing_health(
        session_factory=session_factory, stale_hours=args.stale_hours
    )
    write_report(report, out=stdout)
    return 1 if any(report.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
