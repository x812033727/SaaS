"""重試 cancel_failed 訂閱的綠界停扣（兌現 README「待 ops 重試」承諾）。

Usage:
    python -m saas_mvp.ops.retry_cancel_failed --dry-run
    python -m saas_mvp.ops.retry_cancel_failed --apply

設計（比照 ops/send_due_reminders.py）：
  * 掃 status=cancel_failed 的 FeatureSubscription（退訂時綠界
    CreditCardPeriodAction 失敗者;功能已關但可能仍在扣卡）。
  * 逐筆呼叫 EcpayClient().cancel_period(merchant_trade_no)：
    RtnCode==1 → mark_cancelled(ok=True);否則留在 cancel_failed 等下輪。
  * 每筆獨立 try/except（單筆網路失敗不中斷批次）;--dry-run 只列不打。
  * ecpay_client 可注入供測試。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.feature_subscription import (
    SUB_CANCEL_FAILED,
    FeatureSubscription,
)


@dataclass(frozen=True)
class RetryResult:
    subscription_id: int
    trade_no: str
    status: str       # cancelled | still_failed | error | would_retry
    detail: str = ""

    def to_line(self) -> str:
        parts = [
            f"subscription_id={self.subscription_id}",
            f"trade_no={self.trade_no}",
            f"status={self.status}",
        ]
        if self.detail:
            parts.append(f"detail={self.detail}")
        return " ".join(parts)


def retry_cancel_failed(
    *,
    session_factory: sessionmaker = SessionLocal,
    ecpay_client=None,
    apply: bool = False,
    limit: int = 100,
) -> list[RetryResult]:
    """掃描並（apply 時）重試停扣;回傳每筆結果。"""
    results: list[RetryResult] = []
    with session_factory() as db:
        subs = list(db.execute(
            select(FeatureSubscription)
            .where(FeatureSubscription.status == SUB_CANCEL_FAILED)
            .order_by(FeatureSubscription.id)
            .limit(limit)
        ).scalars())

        if not apply:
            return [
                RetryResult(s.id, s.merchant_trade_no or "", "would_retry")
                for s in subs
            ]

        if ecpay_client is None:
            from saas_mvp.services.payment_ecpay import EcpayClient

            ecpay_client = EcpayClient()

        from saas_mvp.services import subscriptions as subs_svc

        for sub in subs:
            try:
                resp = ecpay_client.cancel_period(sub.merchant_trade_no)
                if str(resp.get("RtnCode")) == "1":
                    subs_svc.mark_cancelled(db, sub, ok=True)
                    results.append(RetryResult(
                        sub.id, sub.merchant_trade_no or "", "cancelled"
                    ))
                else:
                    results.append(RetryResult(
                        sub.id,
                        sub.merchant_trade_no or "",
                        "still_failed",
                        detail=str(resp.get("RtnMsg", ""))[:64],
                    ))
            except Exception as exc:  # noqa: BLE001 — per-row isolation
                db.rollback()
                results.append(RetryResult(
                    sub.id,
                    sub.merchant_trade_no or "",
                    "error",
                    detail=type(exc).__name__,
                ))
    return results


def write_report(results: list[RetryResult], *, apply: bool, out: TextIO) -> None:
    print(f"mode={'apply' if apply else 'dry_run'}", file=out)
    for r in results:
        print(r.to_line(), file=out)
    counts = Counter(r.status for r in results)
    print(
        "summary "
        f"total={len(results)} "
        f"cancelled={counts['cancelled']} "
        f"still_failed={counts['still_failed']} "
        f"error={counts['error']} "
        f"would_retry={counts['would_retry']}",
        file=out,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Retry ECPay period-cancel for cancel_failed subscriptions."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_false", dest="apply",
        help="List cancel_failed subscriptions; do not call ECPay. Default.",
    )
    mode.add_argument(
        "--apply", action="store_true", dest="apply",
        help="Call ECPay cancel and mark cancelled on success.",
    )
    parser.set_defaults(apply=False)
    parser.add_argument("--limit", type=int, default=100)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    session_factory: sessionmaker = SessionLocal,
    ecpay_client=None,
    stdout: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    import_all_models()
    results = retry_cancel_failed(
        session_factory=session_factory,
        ecpay_client=ecpay_client,
        apply=args.apply,
        limit=args.limit,
    )
    write_report(results, apply=args.apply, out=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
