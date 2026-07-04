"""LINE webhook failed 事件重試上限（MAX_ATTEMPTS）與失敗診斷訊息。

驗收標準
--------
- attempt_count < SAAS_WEBHOOK_MAX_ATTEMPTS 的 failed 事件可被原子 claim 重試
- attempt_count 達上限後不再 claim（LINE 重送落入 duplicate-skip，狀態不變）
- 邊界：attempt_count == 上限 - 1 時最後一次 claim 成功，之後不再成功
- reply 後階段（REPLY_SENT）失敗者不可重試（既有行為回歸鎖）
- _mark_webhook_event_failed 記錄「類名: 訊息」且截斷至 255 字元
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.config import settings
from saas_mvp.db import Base
from saas_mvp.models.line_webhook_event import (
    LineWebhookEvent,
    LineWebhookEventStage,
    LineWebhookEventStatus,
)
from saas_mvp.routers.line_webhook import (
    _claim_failed_webhook_event_for_retry,
    _mark_webhook_event_failed,
)

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_TENANT_ID = 1


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=_engine)
    session = _Session()
    try:
        yield session
        session.rollback()
    finally:
        session.query(LineWebhookEvent).delete()
        session.commit()
        session.close()


def _make_failed_event(
    db,
    *,
    webhook_event_id: str,
    attempt_count: int = 0,
    last_stage: str = LineWebhookEventStage.CLAIMED.value,
) -> LineWebhookEvent:
    row = LineWebhookEvent(
        tenant_id=_TENANT_ID,
        webhook_event_id=webhook_event_id,
        status=LineWebhookEventStatus.FAILED.value,
        attempt_count=attempt_count,
        last_stage=last_stage,
        last_error="Boom",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _reload(db, row_id: int) -> LineWebhookEvent:
    db.expire_all()
    return db.get(LineWebhookEvent, row_id)


class TestRetryCap:
    def test_below_cap_is_claimed(self, db):
        row = _make_failed_event(db, webhook_event_id="evt-below", attempt_count=0)
        assert _claim_failed_webhook_event_for_retry(db, _TENANT_ID, "evt-below")
        fresh = _reload(db, row.id)
        assert fresh.status == LineWebhookEventStatus.PENDING.value
        assert fresh.attempt_count == 1
        assert fresh.last_error is None

    def test_at_cap_is_not_claimed(self, db):
        row = _make_failed_event(
            db,
            webhook_event_id="evt-at-cap",
            attempt_count=settings.webhook_max_attempts,
        )
        assert not _claim_failed_webhook_event_for_retry(db, _TENANT_ID, "evt-at-cap")
        fresh = _reload(db, row.id)
        assert fresh.status == LineWebhookEventStatus.FAILED.value
        assert fresh.attempt_count == settings.webhook_max_attempts

    def test_boundary_last_attempt_then_capped(self, db):
        """attempt_count = 上限-1 → 最後一次 claim 成功；再失敗後即封頂。"""
        row = _make_failed_event(
            db,
            webhook_event_id="evt-boundary",
            attempt_count=settings.webhook_max_attempts - 1,
        )
        assert _claim_failed_webhook_event_for_retry(db, _TENANT_ID, "evt-boundary")
        fresh = _reload(db, row.id)
        assert fresh.attempt_count == settings.webhook_max_attempts

        # 模擬本輪也失敗 → 之後不可再 claim
        fresh.status = LineWebhookEventStatus.FAILED.value
        db.commit()
        assert not _claim_failed_webhook_event_for_retry(
            db, _TENANT_ID, "evt-boundary"
        )
        assert (
            _reload(db, row.id).status == LineWebhookEventStatus.FAILED.value
        )

    def test_post_reply_stage_not_retryable(self, db):
        """reply 已送出者不可重試（避免重複回覆）——既有行為回歸鎖。"""
        _make_failed_event(
            db,
            webhook_event_id="evt-replied",
            attempt_count=0,
            last_stage=LineWebhookEventStage.REPLY_SENT.value,
        )
        assert not _claim_failed_webhook_event_for_retry(
            db, _TENANT_ID, "evt-replied"
        )

    def test_other_tenant_not_claimed(self, db):
        _make_failed_event(db, webhook_event_id="evt-tenant-iso", attempt_count=0)
        assert not _claim_failed_webhook_event_for_retry(
            db, _TENANT_ID + 99, "evt-tenant-iso"
        )


class TestFailureDiagnostics:
    def test_last_error_contains_type_and_message(self, db):
        row = _make_failed_event(db, webhook_event_id="evt-diag", attempt_count=0)
        _mark_webhook_event_failed(
            db,
            row.id,
            LineWebhookEventStage.TRANSLATED.value,
            ValueError("upstream returned 502: bad gateway"),
        )
        fresh = _reload(db, row.id)
        assert fresh.last_error == "ValueError: upstream returned 502: bad gateway"
        assert fresh.last_stage == LineWebhookEventStage.TRANSLATED.value
        assert fresh.status == LineWebhookEventStatus.FAILED.value

    def test_last_error_truncated_to_255(self, db):
        row = _make_failed_event(db, webhook_event_id="evt-trunc", attempt_count=0)
        _mark_webhook_event_failed(
            db,
            row.id,
            LineWebhookEventStage.CLAIMED.value,
            RuntimeError("x" * 500),
        )
        fresh = _reload(db, row.id)
        assert len(fresh.last_error) == 255
        assert fresh.last_error.startswith("RuntimeError: xxx")
