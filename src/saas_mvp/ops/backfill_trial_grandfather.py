"""既有租戶 grandfathering — 翻正 features_default_enabled=False 前的一次性緩衝。

Usage:
    python -m saas_mvp.ops.backfill_trial_grandfather --dry-run
    python -m saas_mvp.ops.backfill_trial_grandfather --apply [--days 30]

設計：
  * 嚴格 freemium 上線瞬間，既有租戶（無 TenantFeature 列者）會立刻失去
    原本「預設全開」的功能。本腳本給每個既有租戶開一段 pro **試用**
    （trial_plan="pro", trial_ends_at=now+days），效果 = 全功能保留 N 天緩衝，
    到期由 effective_plan 純計算即刻降回，不需第二支 cron 翻旗標。
  * 冪等：已有進行中試用（trial_ends_at 在未來）或已是 pro 的租戶跳過；
    重跑不會延長既有試用。
  * 不動 TenantFeature 列：租戶原有的明確開/關（admin 覆寫、單點訂閱）優先權
    本來就高於方案 bundle（features.is_enabled 第 1 層），不受影響。

部署鐵則：先跑本腳本（--apply）再翻 SAAS_FEATURES_DEFAULT_ENABLED=false，
翻的瞬間零行為變化。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.tenant import Tenant


@dataclass(frozen=True)
class BackfillResult:
    tenant_id: int
    name: str
    status: str       # granted | would_grant | skipped_active_trial | skipped_pro
    trial_ends_at: str = ""

    def to_line(self) -> str:
        parts = [
            f"tenant_id={self.tenant_id}",
            f"name={self.name}",
            f"status={self.status}",
        ]
        if self.trial_ends_at:
            parts.append(f"trial_ends_at={self.trial_ends_at}")
        return " ".join(parts)


def backfill_trial(
    *,
    session_factory: sessionmaker = SessionLocal,
    apply: bool = False,
    days: int = 30,
    now: datetime.datetime | None = None,
) -> list[BackfillResult]:
    """為既有租戶補 pro 試用；回傳每租戶結果。"""
    from saas_mvp.services import plans as plans_svc

    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    ends = effective_now + datetime.timedelta(days=days)
    results: list[BackfillResult] = []

    db = session_factory()
    try:
        tenants = db.execute(select(Tenant).order_by(Tenant.id)).scalars().all()
        for t in tenants:
            if plans_svc.trial_active(t, now=effective_now):
                results.append(BackfillResult(t.id, t.name, "skipped_active_trial"))
                continue
            if plans_svc.normalize_plan(t.plan) == plans_svc.PLAN_PRO:
                results.append(BackfillResult(t.id, t.name, "skipped_pro"))
                continue
            if apply:
                t.trial_plan = plans_svc.PLAN_PRO
                t.trial_ends_at = ends
                results.append(
                    BackfillResult(t.id, t.name, "granted", ends.isoformat())
                )
            else:
                results.append(
                    BackfillResult(t.id, t.name, "would_grant", ends.isoformat())
                )
        if apply:
            db.commit()
    finally:
        db.close()
    return results


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    import_all_models()
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True)
    group.add_argument("--apply", action="store_true")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)

    results = backfill_trial(apply=args.apply, days=args.days)
    for r in results:
        print(r.to_line(), file=out)
    granted = sum(1 for r in results if r.status in ("granted", "would_grant"))
    skipped = len(results) - granted
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] granted={granted} skipped={skipped} total={len(results)}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
