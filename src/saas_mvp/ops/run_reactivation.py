"""執行喚回（reactivation）行銷活動（cron 每日 14:00；單實例去重）。

Usage:
    python -m saas_mvp.ops.run_reactivation --dry-run
    python -m saas_mvp.ops.run_reactivation --apply --max 50

設計（比照 ops/send_due_reminders.py / run_birthday_campaigns.py）：
  * argparse --dry-run（預設）/ --apply；session_factory / push_client 可注入供測試。
  * 對每個開通 MARKETING_AUTO 且有 active reactivation 活動的租戶執行 run_campaign(now)。
  * 受眾＝last_booked_at 早於 reactivation_dormant_days（預設 90 天）的顧客。
  * --max 預設 settings.reactivation_cap_per_shop（50）。
  * per-tenant 例外隔離；冪等 period_key='YYYYMMDD'（每天一次）。
"""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

from sqlalchemy.orm import sessionmaker

from saas_mvp.config import settings
from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.line_client import LinePushClient
from saas_mvp.models.campaign import CAMPAIGN_REACTIVATION
from saas_mvp.ops.run_birthday_campaigns import (
    _positive_int,
    run_campaigns,
    write_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run reactivation marketing campaigns via LINE push."
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
    cap = args.cap if args.cap is not None else settings.reactivation_cap_per_shop
    results = run_campaigns(
        campaign_type=CAMPAIGN_REACTIVATION,
        session_factory=session_factory,
        push_client=push_client,
        apply=args.apply,
        cap=cap,
    )
    write_report(results, apply=args.apply, out=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
