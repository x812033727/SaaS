"""逾時未付定金自動取消（C4）。

Usage:
    python -m saas_mvp.ops.cancel_unpaid_deposits --dry-run
    python -m saas_mvp.ops.cancel_unpaid_deposits --apply

掃「deposit pending 逾時且預約仍 confirmed」→ 標 expired + 取消預約
(cancel_reservation 系統路徑,自動回補名額)+ LINE 通知顧客(best-effort,
吃推播額度)。crontab 每 5 分。
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.line_client import HttpLinePushClient, LinePushClient
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.services import deposit as deposit_svc
from saas_mvp.services import push_quota as push_quota_svc

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CancelResult:
    reservation_id: int
    tenant_id: int
    status: str  # cancelled | would_cancel | skipped | failed

    def to_line(self) -> str:
        return (
            f"reservation_id={self.reservation_id} tenant_id={self.tenant_id} "
            f"status={self.status}"
        )


def cancel_unpaid_deposits(
    *,
    session_factory: sessionmaker = SessionLocal,
    push_client: LinePushClient | None = None,
    apply: bool = False,
    now: datetime.datetime | None = None,
) -> list[CancelResult]:
    from saas_mvp.models.reservation import Reservation
    from saas_mvp.services import booking as booking_svc

    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    client = push_client or HttpLinePushClient()
    results: list[CancelResult] = []

    with session_factory() as db:
        expired = deposit_svc.list_expired_pending(db, now=effective_now)
        targets = [(r.id, r.tenant_id, r.line_user_id) for r in expired]

    for resv_id, tenant_id, line_user_id in targets:
        if not apply:
            results.append(CancelResult(resv_id, tenant_id, "would_cancel"))
            continue
        with session_factory() as db:
            try:
                # 狀態守衛的原子過期:僅當定金仍為 pending 時才改 EXPIRED。
                # 與付款回調競態時,若對方已搶先標 PAID 則 rowcount=0 → 放棄本筆,
                # 不覆寫已付款狀態、也不取消已付款的預約(原本無守衛的
                # read-then-write 會在 READ COMMITTED / SQLite 下 clobber PAID→EXPIRED)。
                claimed = db.execute(
                    update(Reservation)
                    .where(
                        Reservation.id == resv_id,
                        Reservation.deposit_status == deposit_svc.DEPOSIT_PENDING,
                    )
                    .values(deposit_status=deposit_svc.DEPOSIT_EXPIRED)
                ).rowcount
                if claimed != 1:
                    db.commit()
                    results.append(CancelResult(resv_id, tenant_id, "skipped"))
                    continue
                db.commit()
                # 系統路徑取消(line_user_id=None 跳過擁有者驗證),回補名額。
                booking_svc.cancel_reservation(
                    db, tenant_id=tenant_id, reservation_id=resv_id,
                    line_user_id=None,
                )
                results.append(CancelResult(resv_id, tenant_id, "cancelled"))
            except Exception as exc:  # noqa: BLE001 — 單筆失敗不中斷批次
                db.rollback()
                _log.warning(
                    "cancel unpaid deposit failed resv=%d: %s", resv_id, exc
                )
                results.append(CancelResult(resv_id, tenant_id, "failed"))
                continue

            # LINE 通知(best-effort,額度前檢後扣)。
            if line_user_id:
                try:
                    cfg = db.execute(
                        select(LineChannelConfig).where(
                            LineChannelConfig.tenant_id == tenant_id
                        )
                    ).scalar_one_or_none()
                    if cfg is not None and push_quota_svc.has_push_quota(
                        db, tenant_id, now=effective_now
                    ):
                        client.push(
                            line_user_id,
                            f"您的預約 #{resv_id} 因定金逾時未付已自動取消。"
                            "如仍需預約,歡迎重新選擇時段!",
                            access_token=cfg.access_token,
                        )
                        push_quota_svc.consume_push(db, tenant_id, now=effective_now)
                except Exception:  # noqa: BLE001 — 通知失敗不影響取消
                    _log.warning(
                        "deposit-cancel notify failed resv=%d", resv_id, exc_info=True
                    )
    return results


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    import_all_models()
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True)
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    results = cancel_unpaid_deposits(apply=args.apply)
    for r in results:
        print(r.to_line(), file=out)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] total={len(results)}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
