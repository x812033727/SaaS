"""執行生日行銷活動（cron 每日 09:00；單實例去重）。

Usage:
    python -m saas_mvp.ops.run_birthday_campaigns --dry-run
    python -m saas_mvp.ops.run_birthday_campaigns --apply --max 200

設計（比照 ops/send_due_reminders.py）：
  * argparse --dry-run（預設）/ --apply；session_factory / push_client 可注入供測試。
  * 對每個開通 MARKETING_AUTO 且有 active birthday 活動的租戶執行 run_campaign(now)。
  * per-tenant 例外隔離；--max 控制每活動單次上限。
  * 冪等：CampaignSend UniqueConstraint(campaign_id, customer_id, period_key='YYYY')。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from collections import Counter
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from saas_mvp.config import settings
from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.line_client import HttpLinePushClient, LinePushClient
from saas_mvp.models.campaign import CAMPAIGN_BIRTHDAY, Campaign
from saas_mvp.services import features as features_svc
from saas_mvp.services import marketing as marketing_svc


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass(frozen=True)
class CampaignRunResult:
    tenant_id: int
    campaign_id: int
    status: str  # ran | would_run | skipped
    sent: int = 0
    skipped: int = 0
    reason: str = ""

    def to_line(self) -> str:
        return (
            f"tenant_id={self.tenant_id} campaign_id={self.campaign_id} "
            f"status={self.status} sent={self.sent} skipped={self.skipped} "
            f"reason={self.reason}"
        )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _eligible_campaigns(db, *, campaign_type: str) -> list[Campaign]:
    return list(
        db.execute(
            select(Campaign).where(
                Campaign.type == campaign_type,
                Campaign.is_active.is_(True),
                Campaign.status == "active",
            )
        ).scalars()
    )


def run_campaigns(
    *,
    campaign_type: str = CAMPAIGN_BIRTHDAY,
    session_factory: sessionmaker = SessionLocal,
    push_client: LinePushClient | None = None,
    apply: bool = False,
    cap: int | None = None,
    now: datetime.datetime | None = None,
) -> list[CampaignRunResult]:
    """掃描指定類型活動並（apply 時）執行 run_campaign；回傳每活動結果。"""
    client = push_client or HttpLinePushClient()
    effective_now = now or _utcnow()
    effective_cap = cap if cap is not None else settings.marketing_max_per_run

    with session_factory() as db:
        campaigns = _eligible_campaigns(db, campaign_type=campaign_type)
        ids = [(c.tenant_id, c.id) for c in campaigns]

    results: list[CampaignRunResult] = []
    for tenant_id, campaign_id in ids:
        with session_factory() as db:
            try:
                if not features_svc.is_enabled(
                    db, tenant_id, features_svc.MARKETING_AUTO
                ):
                    results.append(
                        CampaignRunResult(
                            tenant_id, campaign_id, "skipped", reason="feature_disabled"
                        )
                    )
                    continue
                campaign = db.get(Campaign, campaign_id)
                if campaign is None:
                    continue
                if not apply:
                    results.append(
                        CampaignRunResult(
                            tenant_id, campaign_id, "would_run", reason="dry_run"
                        )
                    )
                    continue
                outcome = marketing_svc.run_campaign(
                    db,
                    campaign=campaign,
                    now=effective_now,
                    cap=effective_cap,
                    push_client=client,
                )
                results.append(
                    CampaignRunResult(
                        tenant_id,
                        campaign_id,
                        "ran",
                        sent=outcome["sent"],
                        skipped=outcome["skipped"],
                    )
                )
            except Exception as exc:  # noqa: BLE001 - per-tenant failure must not stop batch
                db.rollback()
                results.append(
                    CampaignRunResult(
                        tenant_id,
                        campaign_id,
                        "skipped",
                        reason=f"error:{type(exc).__name__}",
                    )
                )
    return results


def write_report(
    results: list[CampaignRunResult], *, apply: bool, out: TextIO
) -> None:
    mode = "apply" if apply else "dry_run"
    print(f"mode={mode}", file=out)
    for result in results:
        print(result.to_line(), file=out)
    counts = Counter(result.status for result in results)
    total_sent = sum(r.sent for r in results)
    print(
        "summary "
        f"campaigns={len(results)} "
        f"ran={counts['ran']} "
        f"would_run={counts['would_run']} "
        f"skipped={counts['skipped']} "
        f"sent={total_sent}",
        file=out,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run birthday marketing campaigns via LINE push."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_false",
        dest="apply",
        help="Report what would run; do not push or commit. Default.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        dest="apply",
        help="Run campaigns and push messages.",
    )
    parser.set_defaults(apply=False)
    parser.add_argument(
        "--max", type=_positive_int, dest="cap", help="Max sends per campaign."
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    session_factory: sessionmaker = SessionLocal,
    push_client: LinePushClient | None = None,
    stdout: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    # 確保 SQLAlchemy registry 完整：standalone（python -m / cron）執行時
    # 各 model 未必都被 import，relationship 字串（如 'Tenant'）會解析失敗。
    import_all_models()
    results = run_campaigns(
        campaign_type=CAMPAIGN_BIRTHDAY,
        session_factory=session_factory,
        push_client=push_client,
        apply=args.apply,
        cap=args.cap,
    )
    write_report(results, apply=args.apply, out=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
