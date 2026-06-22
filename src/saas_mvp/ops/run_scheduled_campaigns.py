"""執行已排程的群發/限時行銷活動（cron；單實例去重）。

Usage:
    python -m saas_mvp.ops.run_scheduled_campaigns --dry-run
    python -m saas_mvp.ops.run_scheduled_campaigns --apply --max 500

設計（比照 ops/send_due_reminders.py / run_birthday_campaigns.py）：
  * argparse --dry-run（預設）/ --apply；session_factory / push_client 可注入供測試。
  * 觸發條件：is_active 且 status='active' 且 schedule_at<=now 且
    (expires_at is null 或 now<expires_at)。
  * 對開通 MARKETING_AUTO 的租戶執行 run_campaign(now)。
  * per-tenant 例外隔離；--max 控制每活動單次上限。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from typing import TextIO

from sqlalchemy import or_, select
from sqlalchemy.orm import sessionmaker

from saas_mvp.config import settings
from saas_mvp.db import SessionLocal
from saas_mvp.line_client import HttpLinePushClient, LinePushClient
from saas_mvp.models.campaign import Campaign
from saas_mvp.ops.run_birthday_campaigns import (
    CampaignRunResult,
    _positive_int,
    write_report,
)
from saas_mvp.services import features as features_svc
from saas_mvp.services import marketing as marketing_svc


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _due_scheduled(db, *, now: datetime.datetime) -> list[Campaign]:
    return list(
        db.execute(
            select(Campaign).where(
                Campaign.is_active.is_(True),
                Campaign.status == "active",
                Campaign.schedule_at.is_not(None),
                Campaign.schedule_at <= now,
                or_(Campaign.expires_at.is_(None), Campaign.expires_at > now),
            )
        ).scalars()
    )


def run_scheduled(
    *,
    session_factory: sessionmaker = SessionLocal,
    push_client: LinePushClient | None = None,
    apply: bool = False,
    cap: int | None = None,
    now: datetime.datetime | None = None,
) -> list[CampaignRunResult]:
    client = push_client or HttpLinePushClient()
    effective_now = now or _utcnow()
    effective_cap = cap if cap is not None else settings.marketing_max_per_run

    with session_factory() as db:
        campaigns = _due_scheduled(db, now=effective_now)
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
            except Exception as exc:  # noqa: BLE001 - per-tenant isolation
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run scheduled / time-limited broadcast campaigns via LINE push."
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
    results = run_scheduled(
        session_factory=session_factory,
        push_client=push_client,
        apply=args.apply,
        cap=args.cap,
    )
    write_report(results, apply=args.apply, out=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
