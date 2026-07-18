"""執行到訪後感謝行銷活動(cron 每小時;R7-B)。

比照 ops/run_anniversary_campaigns.py,只差 campaign_type=CAMPAIGN_POST_VISIT。
受眾=過去 post_visit_hours 內 attended=True 的 confirmed 預約顧客;
日粒度冪等(同顧客同日多次到訪只謝一次)。

Usage:
    python -m saas_mvp.ops.run_post_visit_campaigns --dry-run
    python -m saas_mvp.ops.run_post_visit_campaigns --apply --max 200
"""

from __future__ import annotations

import sys
from typing import TextIO

from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.line_client import LinePushClient
from saas_mvp.models.campaign import CAMPAIGN_POST_VISIT
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
        campaign_type=CAMPAIGN_POST_VISIT,
        session_factory=session_factory,
        push_client=push_client,
        apply=args.apply,
        cap=args.cap,
    )
    write_report(results, apply=args.apply, out=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
