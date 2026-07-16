"""候補容量保留不變量檢核(R4-B1)— 純唯讀巡檢。

不變量:每個 slot 的 held_count == SUM(hold_party_size) of 該 slot 上
status=notified 的 waitlist entries。飄移(漏釋放/雙釋放)會被此腳本抓出。

Usage:
    python -m saas_mvp.ops.check_waitlist_holds          # 印飄移列
    python -m saas_mvp.ops.check_waitlist_holds --json

exit code:有飄移 → 1。可掛進巡檢 cron(唯讀,不修)。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TextIO

from sqlalchemy import func, select

from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.booking_waitlist import WAITLIST_NOTIFIED, WaitlistEntry


def find_drift(*, session_factory=SessionLocal) -> list[dict]:
    """回傳 held_count 與實際保留合計不符的 slot 列。"""
    import_all_models()
    drift: list[dict] = []
    with session_factory() as db:
        expected = dict(
            db.execute(
                select(
                    WaitlistEntry.slot_id,
                    func.coalesce(func.sum(WaitlistEntry.hold_party_size), 0),
                )
                .where(WaitlistEntry.status == WAITLIST_NOTIFIED)
                .group_by(WaitlistEntry.slot_id)
            ).all()
        )
        for slot_id, held in db.execute(
            select(BookingSlot.id, BookingSlot.held_count)
        ).all():
            want = int(expected.get(slot_id, 0))
            if int(held or 0) != want:
                drift.append(
                    {"slot_id": slot_id, "held_count": int(held or 0), "expected": want}
                )
    return drift


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    drift = find_drift()
    if args.json:
        print(json.dumps(drift, ensure_ascii=False), file=out)
    else:
        if not drift:
            print("waitlist holds OK: no drift", file=out)
        for d in drift:
            print(
                f"DRIFT slot={d['slot_id']} held_count={d['held_count']} "
                f"expected={d['expected']}",
                file=out,
            )
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
