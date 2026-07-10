"""試用到期通知（B3）— 每日 cron，到期前 7/3/1 天與到期日寄 email。

Usage:
    python -m saas_mvp.ops.send_trial_notices --dry-run
    python -m saas_mvp.ops.send_trial_notices --apply

設計：
  * 試用到期本身**不需要**任何翻旗標（plans.effective_plan 純計算，到期即刻
    降回 tenant.plan）；本腳本只負責提醒店家訂閱，避免功能突然消失的體感。
  * 冪等靠「每日一跑 × 日粒度」：days_left ∈ {7,3,1,0} 每租戶各只命中一次，
    不需去重表。多實例部署請沿用 scheduler 單一實例慣例。
  * 收件人：該租戶全部使用者（目前一租戶一 user；B5 RBAC 後可改 owner）。
  * 寄送失敗只記 log 繼續下一租戶（best-effort；下一個里程碑日還會再提醒）。
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.services.mailer import Mailer, MailerError, get_mailer

_log = logging.getLogger(__name__)

# 到期前提醒里程碑（天）；0 = 到期日當天（已轉免費版通知）。
NOTICE_DAYS = (7, 3, 1, 0)


@dataclass(frozen=True)
class NoticeResult:
    tenant_id: int
    name: str
    days_left: int
    status: str  # sent | would_send | send_failed | no_recipient

    def to_line(self) -> str:
        return (
            f"tenant_id={self.tenant_id} name={self.name} "
            f"days_left={self.days_left} status={self.status}"
        )


def _days_left(tenant: Tenant, now: datetime.datetime) -> int | None:
    """試用剩餘天數（日粒度）；無試用/非法值回 None。"""
    from saas_mvp.services import plans as plans_svc

    trial_plan = tenant.trial_plan
    ends = tenant.trial_ends_at
    if trial_plan not in plans_svc.PLAN_BUNDLES or ends is None:
        return None
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=datetime.timezone.utc)
    return (ends.date() - now.date()).days


def _subject_body(tenant: Tenant, days_left: int) -> tuple[str, str]:
    from saas_mvp.services import plans as plans_svc

    label = plans_svc.plan_label(plans_svc.normalize_plan(tenant.trial_plan))
    if days_left == 0:
        return (
            f"{label}試用已到期 — LINE 預約平台",
            f"您好！\n\n「{tenant.name}」的{label}試用已到期，帳號已轉為免費版；"
            "所有資料完整保留。\n\n訂閱方案即可繼續使用進階功能："
            "後台 →「方案」頁。\n",
        )
    return (
        f"{label}試用還剩 {days_left} 天 — LINE 預約平台",
        f"您好！\n\n「{tenant.name}」的{label}試用將於 {days_left} 天後到期。\n"
        "到期後自動轉為免費版（資料保留），訂閱即可無縫接續全部功能：\n"
        "後台 →「方案」頁。\n",
    )


def send_trial_notices(
    *,
    session_factory: sessionmaker = SessionLocal,
    mailer: Mailer | None = None,
    apply: bool = False,
    now: datetime.datetime | None = None,
) -> list[NoticeResult]:
    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    effective_mailer = mailer or get_mailer()
    results: list[NoticeResult] = []

    db = session_factory()
    try:
        tenants = db.execute(select(Tenant).order_by(Tenant.id)).scalars().all()
        for t in tenants:
            days = _days_left(t, effective_now)
            if days is None or days not in NOTICE_DAYS:
                continue
            recipients = db.execute(
                select(User).where(User.tenant_id == t.id)
            ).scalars().all()
            if not recipients:
                results.append(NoticeResult(t.id, t.name, days, "no_recipient"))
                continue
            if not apply:
                results.append(NoticeResult(t.id, t.name, days, "would_send"))
                continue
            subject, body = _subject_body(t, days)
            status = "sent"
            for u in recipients:
                try:
                    effective_mailer.send(to=u.email, subject=subject, body=body)
                except MailerError:
                    _log.warning(
                        "trial notice send failed tenant=%d to=%s", t.id, u.email,
                        exc_info=True,
                    )
                    status = "send_failed"
            results.append(NoticeResult(t.id, t.name, days, status))
    finally:
        db.close()
    return results


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    import_all_models()
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True)
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    results = send_trial_notices(apply=args.apply)
    for r in results:
        print(r.to_line(), file=out)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] total={len(results)}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
