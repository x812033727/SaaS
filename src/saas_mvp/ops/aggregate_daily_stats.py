"""每日營運預聚合回填(R3-B3)— scheduler cron 每日跑一次。

Usage:
    python -m saas_mvp.ops.aggregate_daily_stats            # dry-run(只列不寫)
    python -m saas_mvp.ops.aggregate_daily_stats --apply    # 回填近 3 天
    python -m saas_mvp.ops.aggregate_daily_stats --apply --days 30   # 首次回填

逐租戶 rollup 近 N 天(不含今天)並各自 commit;單租戶失敗不影響其他租戶。
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import daily_stats

_log = logging.getLogger(__name__)


def aggregate_daily_stats(
    *, session_factory=SessionLocal, days: int = 3, apply: bool = False
) -> dict:
    import_all_models()
    summary = {"tenants": 0, "days": 0, "errors": 0}
    with session_factory() as db:
        tenant_ids = [t for t in db.execute(select(Tenant.id)).scalars()]
    for tenant_id in tenant_ids:
        try:
            with session_factory() as db:
                n = daily_stats.rollup(db, tenant_id=tenant_id, days_back=days)
                if apply:
                    db.commit()
                else:
                    db.rollback()
            summary["tenants"] += 1
            summary["days"] += n
        except Exception:  # noqa: BLE001 — 單租戶失敗不擋整批
            _log.warning("daily stats rollup failed tenant=%s", tenant_id, exc_info=True)
            summary["errors"] += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真的寫入(預設 dry-run)")
    parser.add_argument("--days", type=int, default=3, help="回填天數(不含今天)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    summary = aggregate_daily_stats(days=args.days, apply=args.apply)
    print(
        f"daily-stats {'APPLY' if args.apply else 'DRY-RUN'}: "
        f"tenants={summary['tenants']} days={summary['days']} errors={summary['errors']}"
    )
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
