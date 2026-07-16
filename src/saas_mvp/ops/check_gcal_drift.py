"""GCal 漂移偵測 cron(R4-B3)— 逐租戶輪詢 Google 端已同步事件是否被改/刪。

Usage:
    python -m saas_mvp.ops.check_gcal_drift            # dry-run(不寫、不通知)
    python -m saas_mvp.ops.check_gcal_drift --apply

每租戶一次 events.get 掃描(僅未來已同步預約);命中設 drift 欄 + email 通知
店家(每筆只寄一次)。**絕不改動預約狀態**。配額安全:每輪限 N 個租戶。
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TextIO

from sqlalchemy import select

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.tenant_gcal_credential import GCAL_CONNECTED, TenantGcalCredential
from saas_mvp.services import gcal as gcal_svc

_log = logging.getLogger(__name__)


def check_gcal_drift(*, session_factory=SessionLocal, apply: bool = False, limit: int = 200) -> dict:
    import_all_models()
    summary = {"tenants": 0, "checked": 0, "drift": 0, "cleared": 0, "errors": 0}
    with session_factory() as db:
        tenant_ids = list(db.execute(
            select(TenantGcalCredential.tenant_id)
            .where(TenantGcalCredential.status == GCAL_CONNECTED)
            .limit(limit)
        ).scalars())
    for tenant_id in tenant_ids:
        try:
            with session_factory() as db:
                res = gcal_svc.check_drift_for_tenant(db, tenant_id, apply=apply)
            summary["tenants"] += 1
            summary["checked"] += res["checked"]
            summary["drift"] += res["drift"]
            summary["cleared"] += res["cleared"]
        except Exception:  # noqa: BLE001 — 單租戶失敗不擋整批
            _log.warning("gcal drift check failed tenant=%s", tenant_id, exc_info=True)
            summary["errors"] += 1
    return summary


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    s = check_gcal_drift(apply=args.apply, limit=args.limit)
    print(
        f"gcal-drift {'APPLY' if args.apply else 'DRY-RUN'}: "
        f"tenants={s['tenants']} checked={s['checked']} drift={s['drift']} "
        f"cleared={s['cleared']} errors={s['errors']}",
        file=out,
    )
    return 1 if s["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
