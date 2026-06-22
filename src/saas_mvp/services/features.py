"""進階功能旗標（per-tenant entitlement）+ 閘門。

商業模式：基本預約免費；AUTO_REMINDER / COUPON_SYSTEM / PRODUCT_SALES 為進階功能，
可由租戶自助訂閱（stub 付款）或平台 admin 覆寫開關。

is_enabled 為**唯一真相來源**——REST / webhook / ops / UI 全部走它，避免某條路徑漏接。
無 TenantFeature 列時回 settings.features_default_enabled（預設 True＝向後相容）。
"""

from __future__ import annotations

import datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.auth.dependencies import Actor, get_current_actor
from saas_mvp.config import settings
from saas_mvp.db import get_db
from saas_mvp.models.feature_change_history import FeatureChangeHistory
from saas_mvp.models.tenant_feature import TenantFeature

# ── 進階功能常數 ──────────────────────────────────────────────────────────────
AUTO_REMINDER = "AUTO_REMINDER"
COUPON_SYSTEM = "COUPON_SYSTEM"
PRODUCT_SALES = "PRODUCT_SALES"
# PHASE 1：多分店 / 員工排班 / 服務項目。
STAFF_SCHEDULING = "STAFF_SCHEDULING"
MULTI_LOCATION = "MULTI_LOCATION"
SERVICE_CATALOG = "SERVICE_CATALOG"
# PHASE 2：預約異動通知（店家修改/取消時 LINE 推播）。
BOOKING_NOTIFY = "BOOKING_NOTIFY"
# PHASE 3：公開店家頁（含作品集）。
PUBLIC_PROFILE = "PUBLIC_PROFILE"
# PHASE 4-1：行銷自動化（生日/喚回/群發活動）+ AI 客服。
MARKETING_AUTO = "MARKETING_AUTO"
AI_ASSISTANT = "AI_ASSISTANT"
# PHASE 4-2：隱私保護模式（tokenized PII 網頁表單）+ 進階報表（xlsx/pdf 匯出）。
PRIVACY_MODE = "PRIVACY_MODE"
ADVANCED_REPORTING = "ADVANCED_REPORTING"

# registry：key → 顯示資訊。月費取自 settings（可由環境覆寫）。
_FEATURE_LABELS: dict[str, str] = {
    AUTO_REMINDER: "自動提醒",
    COUPON_SYSTEM: "優惠券／會員",
    PRODUCT_SALES: "商品銷售",
    STAFF_SCHEDULING: "員工排班",
    MULTI_LOCATION: "多分店",
    SERVICE_CATALOG: "服務項目",
    BOOKING_NOTIFY: "預約異動通知",
    PUBLIC_PROFILE: "公開店家頁",
    MARKETING_AUTO: "行銷自動化",
    AI_ASSISTANT: "AI 客服",
    PRIVACY_MODE: "隱私保護",
    ADVANCED_REPORTING: "進階報表",
}
VALID_FEATURES = frozenset(_FEATURE_LABELS)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class UnknownFeatureError(ValueError):
    """未知的 feature key。"""


def validate_feature(feature: str) -> str:
    if feature not in VALID_FEATURES:
        raise UnknownFeatureError(
            f"Unknown feature: {feature!r}. Valid: {sorted(VALID_FEATURES)}"
        )
    return feature


def is_enabled(db: Session, tenant_id: int, feature: str) -> bool:
    """該租戶是否開通某進階功能；無明確設定時回 settings 預設。"""
    row = db.execute(
        select(TenantFeature).where(
            TenantFeature.tenant_id == tenant_id,
            TenantFeature.feature == feature,
        )
    ).scalar_one_or_none()
    if row is None:
        return settings.features_default_enabled
    return bool(row.enabled)


def set_enabled(
    db: Session,
    tenant_id: int,
    feature: str,
    enabled: bool,
    *,
    actor_user_id: int | None,
    source: str,
    reason: str | None = None,
) -> TenantFeature:
    """upsert TenantFeature + 寫 append-only 稽核 + commit。source: subscribe/unsubscribe/admin。"""
    validate_feature(feature)
    row = db.execute(
        select(TenantFeature).where(
            TenantFeature.tenant_id == tenant_id,
            TenantFeature.feature == feature,
        )
    ).scalar_one_or_none()
    if row is None:
        row = TenantFeature(
            tenant_id=tenant_id, feature=feature, enabled=enabled,
            updated_by_user_id=actor_user_id,
        )
        db.add(row)
    else:
        row.enabled = enabled
        row.updated_by_user_id = actor_user_id
    db.add(FeatureChangeHistory(
        tenant_id=tenant_id, feature=feature, enabled=enabled,
        changed_by_user_id=actor_user_id, source=source, reason=reason,
    ))
    db.commit()
    db.refresh(row)
    return row


def list_for_tenant(db: Session, tenant_id: int) -> list[dict]:
    """所有進階功能的開通狀態 + 月費（供自助/管理頁）。"""
    return [
        {
            "key": key,
            "label": _FEATURE_LABELS[key],
            "monthly_price_cents": settings.feature_monthly_price_cents,
            "enabled": is_enabled(db, tenant_id, key),
        }
        for key in sorted(VALID_FEATURES)
    ]


# ── FastAPI 閘門 dependency（API；未開通 → 403） ──────────────────────────────

def require_feature(feature: str):
    """回傳一個 dependency：該租戶未開通 ``feature`` 時拋 403。

    用法：``APIRouter(..., dependencies=[Depends(require_feature(COUPON_SYSTEM))])``。
    """

    def _dep(
        actor: Actor = Depends(get_current_actor),
        db: Session = Depends(get_db),
    ) -> Actor:
        if not is_enabled(db, actor.user.tenant_id, feature):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Feature '{feature}' is not enabled for this tenant",
            )
        return actor

    return _dep
