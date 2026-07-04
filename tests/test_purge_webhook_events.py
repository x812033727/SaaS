"""ops/purge_webhook_events — LINE webhook 冪等事件 TTL 清理。

驗收標準
--------
- dry-run（預設）只回報數量，不刪任何資料
- apply 刪除 processed 且 processed_at 早於 TTL 的事件
- 保留：TTL 內的 processed、pending、未達重試上限的 failed
- --include-failed 才會刪「已達重試上限且過期」的 failed
- --limit 限制單次刪除量
- main() 報表輸出格式
"""

from __future__ import annotations

import datetime
import io

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.config import settings
from saas_mvp.db import Base
from saas_mvp.models.line_webhook_event import (
    LineWebhookEvent,
    LineWebhookEventStage,
    LineWebhookEventStatus,
)
from saas_mvp.ops.purge_webhook_events import main, purge_webhook_events

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_NOW = datetime.datetime(2026, 7, 4, 12, 0, tzinfo=datetime.timezone.utc)
_OLD = _NOW - datetime.timedelta(days=60)
_RECENT = _NOW - datetime.timedelta(days=1)


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=_engine)
    session = _Session()
    try:
        yield session
    finally:
        session.rollback()
        session.query(LineWebhookEvent).delete()
        session.commit()
        session.close()


def _add_event(
    db,
    *,
    webhook_event_id: str,
    status: str,
    processed_at: datetime.datetime | None = None,
    updated_at: datetime.datetime | None = None,
    attempt_count: int = 0,
    tenant_id: int = 1,
) -> int:
    row = LineWebhookEvent(
        tenant_id=tenant_id,
        webhook_event_id=webhook_event_id,
        status=status,
        attempt_count=attempt_count,
        last_stage=LineWebhookEventStage.CLAIMED.value,
        processed_at=processed_at,
    )
    db.add(row)
    db.commit()
    if updated_at is not None:
        # onupdate 會覆寫,直接以 UPDATE 設定測試用時間戳
        db.query(LineWebhookEvent).filter(
            LineWebhookEvent.id == row.id
        ).update({"updated_at": updated_at}, synchronize_session=False)
        db.commit()
    return row.id


def _remaining_ids(db) -> set[int]:
    db.expire_all()
    return set(db.execute(select(LineWebhookEvent.id)).scalars())


def test_dry_run_counts_but_keeps_rows(db):
    _add_event(
        db, webhook_event_id="e-old", status="processed", processed_at=_OLD
    )
    result = purge_webhook_events(session_factory=_Session, apply=False, now=_NOW)
    assert result.dry_run is True
    assert result.processed_purged == 1
    assert len(_remaining_ids(db)) == 1  # 未刪


def test_apply_purges_expired_processed_only(db):
    old_id = _add_event(
        db, webhook_event_id="e-old", status="processed", processed_at=_OLD
    )
    recent_id = _add_event(
        db, webhook_event_id="e-recent", status="processed", processed_at=_RECENT
    )
    pending_id = _add_event(db, webhook_event_id="e-pending", status="pending")
    failed_id = _add_event(
        db,
        webhook_event_id="e-failed",
        status="failed",
        updated_at=_OLD,
        attempt_count=settings.webhook_max_attempts,
    )

    result = purge_webhook_events(session_factory=_Session, apply=True, now=_NOW)
    assert result.dry_run is False
    assert result.processed_purged == 1
    assert result.failed_purged == 0  # 未帶 --include-failed
    remaining = _remaining_ids(db)
    assert old_id not in remaining
    assert {recent_id, pending_id, failed_id} <= remaining


def test_include_failed_purges_only_exhausted_and_expired(db):
    exhausted_old = _add_event(
        db,
        webhook_event_id="e-exhausted-old",
        status="failed",
        updated_at=_OLD,
        attempt_count=settings.webhook_max_attempts,
    )
    retryable_old = _add_event(
        db,
        webhook_event_id="e-retryable-old",
        status="failed",
        updated_at=_OLD,
        attempt_count=0,  # 未達上限 → 仍可能重試,保留
    )
    exhausted_recent = _add_event(
        db,
        webhook_event_id="e-exhausted-recent",
        status="failed",
        updated_at=_RECENT,
        attempt_count=settings.webhook_max_attempts,
    )

    result = purge_webhook_events(
        session_factory=_Session, apply=True, include_failed=True, now=_NOW
    )
    assert result.failed_purged == 1
    remaining = _remaining_ids(db)
    assert exhausted_old not in remaining
    assert {retryable_old, exhausted_recent} <= remaining


def test_limit_caps_deletion(db):
    for i in range(5):
        _add_event(
            db,
            webhook_event_id=f"e-old-{i}",
            status="processed",
            processed_at=_OLD,
        )
    result = purge_webhook_events(
        session_factory=_Session, apply=True, limit=3, now=_NOW
    )
    assert result.processed_purged == 3
    assert len(_remaining_ids(db)) == 2


def test_custom_days_overrides_default(db):
    _add_event(
        db,
        webhook_event_id="e-2d",
        status="processed",
        processed_at=_NOW - datetime.timedelta(days=2),
    )
    kept = purge_webhook_events(
        session_factory=_Session, apply=True, days=3, now=_NOW
    )
    assert kept.processed_purged == 0
    purged = purge_webhook_events(
        session_factory=_Session, apply=True, days=1, now=_NOW
    )
    assert purged.processed_purged == 1


def test_main_report_output(db):
    _add_event(
        db, webhook_event_id="e-main", status="processed", processed_at=_OLD
    )
    out = io.StringIO()
    rc = main([], session_factory=_Session, stdout=out)
    assert rc == 0
    report = out.getvalue()
    assert "mode=dry_run" in report
    assert "would_purge_total=1" in report
    # dry-run 未刪
    assert len(_remaining_ids(db)) == 1

    out2 = io.StringIO()
    rc2 = main(["--apply"], session_factory=_Session, stdout=out2)
    assert rc2 == 0
    assert "mode=apply" in out2.getvalue()
    assert "purged_total=1" in out2.getvalue()
    assert len(_remaining_ids(db)) == 0
