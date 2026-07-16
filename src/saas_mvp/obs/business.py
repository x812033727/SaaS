"""業務面 Prometheus gauges — /metrics scrape 時即時查 DB。

REGISTRY 是 per-process 的,cron 腳本設的 gauge 無法透過 web worker 的
/metrics 曝露;故由 /metrics 端點在 render 前呼叫 collect_business_gauges
即時 COUNT(皆走索引欄位,scrape 間隔下成本可忽略)。

collect 失敗絕不可毀掉 /metrics(呼叫端已 try/except,此處各查詢再各自
防禦一層)。人工巡檢清單見 ops/check_billing_health。
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.obs.metrics import REGISTRY

_log = logging.getLogger(__name__)

# gauge 名稱
SUBSCRIPTIONS_CANCEL_FAILED = "saas_subscriptions_cancel_failed"
SUBSCRIPTIONS_PENDING_STALE = "saas_subscriptions_pending_stale"
WEBHOOK_EVENTS_STUCK_PENDING = "saas_webhook_events_stuck_pending"
WEBHOOK_EVENTS_RETRY_ATTEMPTS = "saas_webhook_events_retry_attempts"
WEBHOOK_EVENTS_DEAD_LETTER = "saas_webhook_events_dead_letter"

# pending 訂閱視為過期的小時數 / webhook pending 視為卡住的分鐘數
_PENDING_STALE_HOURS = 48
_WEBHOOK_STUCK_MINUTES = 30


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def collect_business_gauges(db: Session) -> None:
    """即時查 DB 更新業務 gauges;單一查詢失敗不影響其他 gauge。"""
    from saas_mvp.models.feature_subscription import (
        SUB_CANCEL_FAILED,
        SUB_PENDING,
        FeatureSubscription,
    )
    from saas_mvp.models.line_webhook_event import (
        LineWebhookEvent,
        LineWebhookEventStatus,
    )

    now = _utcnow()

    try:
        REGISTRY.set_gauge(
            SUBSCRIPTIONS_CANCEL_FAILED,
            db.execute(
                select(func.count()).where(
                    FeatureSubscription.status == SUB_CANCEL_FAILED
                )
            ).scalar_one(),
            help_text="Subscriptions stuck in cancel_failed (ECPay stop-charge unconfirmed)",
        )
    except Exception:  # noqa: BLE001
        _log.warning("collect gauge failed: %s", SUBSCRIPTIONS_CANCEL_FAILED)

    try:
        REGISTRY.set_gauge(
            SUBSCRIPTIONS_PENDING_STALE,
            db.execute(
                select(func.count()).where(
                    FeatureSubscription.status == SUB_PENDING,
                    FeatureSubscription.created_at
                    < now - datetime.timedelta(hours=_PENDING_STALE_HOURS),
                )
            ).scalar_one(),
            help_text="Subscriptions pending activation for too long",
        )
    except Exception:  # noqa: BLE001
        _log.warning("collect gauge failed: %s", SUBSCRIPTIONS_PENDING_STALE)

    try:
        # pending 且長時間未更新的 webhook 事件（背景任務掛掉/卡住的訊號;
        # 正常事件在收到後數秒內轉 processed/failed）。
        REGISTRY.set_gauge(
            WEBHOOK_EVENTS_STUCK_PENDING,
            db.execute(
                select(func.count()).where(
                    LineWebhookEvent.status.in_([
                        LineWebhookEventStatus.PENDING.value,
                        LineWebhookEventStatus.PROCESSING.value,
                    ]),
                    LineWebhookEvent.updated_at
                    < now - datetime.timedelta(minutes=_WEBHOOK_STUCK_MINUTES),
                )
            ).scalar_one(),
            help_text="LINE webhook events stuck in pending state",
        )
    except Exception:  # noqa: BLE001
        _log.warning("collect gauge failed: %s", WEBHOOK_EVENTS_STUCK_PENDING)

    try:
        REGISTRY.set_gauge(
            WEBHOOK_EVENTS_RETRY_ATTEMPTS,
            db.execute(
                select(func.coalesce(func.sum(LineWebhookEvent.attempt_count), 0))
            ).scalar_one(),
            help_text="Cumulative persisted LINE webhook retry attempts",
        )
        REGISTRY.set_gauge(
            WEBHOOK_EVENTS_DEAD_LETTER,
            db.execute(
                select(func.count()).where(
                    LineWebhookEvent.status == LineWebhookEventStatus.FAILED.value,
                    LineWebhookEvent.attempt_count >= settings.webhook_max_attempts,
                )
            ).scalar_one(),
            help_text="LINE webhook events exhausted retry attempts",
        )
    except Exception:  # noqa: BLE001
        _log.warning("collect webhook retry/dead-letter gauges failed")
