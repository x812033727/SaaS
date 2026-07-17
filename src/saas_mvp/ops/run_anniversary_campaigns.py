"""執行建檔週年行銷活動(cron 每日;R6-B2)。

比照 ops/run_birthday_campaigns.py,只差 campaign_type=CAMPAIGN_ANNIVERSARY。
掃描所有開通 MARKETING_AUTO 且有 active anniversary 活動的租戶執行 run_campaign。

Usage:
    python -m saas_mvp.ops.run_anniversary_campaigns --dry-run
    python -m saas_mvp.ops.run_anniversary_campaigns --apply --max 200
"""

from __future__ import annotations

import sys
from typing import TextIO

from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.line_client import LinePushClient
from saas_mvp.models.campaign import CAMPAIGN_ANNIVERSARY
from saas_mvp.ops.run_birthday_campaigns import (
    build_parser,
    run_campaigns,
    write_report,
)


def main(
    argv: list[str] | None = None,
    *,
    session_factory: sessionmaker = SessionLocal,
    push_client: LinePushClient | None = None,
    stdout: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    import_all_models()
    results = run_campaigns(
        campaign_type=CAMPAIGN_ANNIVERSARY,
        session_factory=session_factory,
        push_client=push_client,
        apply=args.apply,
        cap=args.cap,
    )
    write_report(results, apply=args.apply, out=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
