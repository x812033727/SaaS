"""進階功能旗標（per-tenant entitlement）+ 閘門。

商業模式：基本預約免費；AUTO_REMINDER / COUPON_SYSTEM / PRODUCT_SALES 為進階功能，
可由租戶自助訂閱（stub 付款）或平台 admin 覆寫開關。

is_enabled 為**唯一真相來源**——REST / webhook / ops / UI 全部走它，避免某條路徑漏接。

三層判定（B1 變現翻正）：
  1. 明確 TenantFeature 列（單點加購／退訂／admin 覆寫）→ 以該列為準
  2. 無列 → 看 effective_plan（services/plans.py，含試用）的 bundle 是否內含
  3. 都沒有 → settings.features_default_enabled（正式環境 False；dev/test 可開 True）
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
# PHASE 5：Flex 圖文選單卡片（carousel）。
FLEX_MENU = "FLEX_MENU"
# 月度推播額度加購（vibeaico「Additional Push Notification Allowance」）：
# 開通後該租戶每月推播額度 = push_allowance_base + push_allowance_boost。
PUSH_BOOST = "PUSH_BOOST"
# 無限員工：未開通時員工數受 settings.free_staff_limit（預設 3）限制，
# 開通（輕量版以上）解除上限。對標 vibeaico「無限員工」。
UNLIMITED_STAFF = "UNLIMITED_STAFF"
# 網頁預約表單（A1.1）：bot 的 quick-reply 附「用網頁預約」token 深連結。
WEB_BOOKING = "WEB_BOOKING"

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
    FLEX_MENU: "圖文選單卡片",
    PUSH_BOOST: "加購推播額度",
    UNLIMITED_STAFF: "無限員工",
    WEB_BOOKING: "網頁預約表單",
}
VALID_FEATURES = frozenset(_FEATURE_LABELS)


def feature_label(feature: str) -> str:
    """feature key → 顯示名稱（未知 key 原樣回傳）。"""
    return _FEATURE_LABELS.get(feature, feature)


# ── 方案 bundle 偽 key（B2 訂閱收款用）────────────────────────────────────────
# 走 FeatureSubscription 既有訂閱機制（建立/回調/停扣/重試全複用），但回調成功
# 時改 tenant.plan 而非 set_enabled 單一 feature。不在 VALID_FEATURES 內：
# 不可被 set_enabled / require_feature 當一般 feature 操作。
BUNDLE_PREFIX = "BUNDLE_"
BUNDLE_STANDARD = "BUNDLE_STANDARD"
BUNDLE_PRO = "BUNDLE_PRO"
VALID_BUNDLES = frozenset({BUNDLE_STANDARD, BUNDLE_PRO})
BUNDLE_LABELS: dict[str, str] = {
    BUNDLE_STANDARD: "標準版",
    BUNDLE_PRO: "專業版",
}
# bundle key → 對應 plan 值（services/plans.py 的 PLAN_*）。
BUNDLE_TO_PLAN: dict[str, str] = {
    BUNDLE_STANDARD: "standard",
    BUNDLE_PRO: "pro",
}


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
    """該租戶是否開通某進階功能（三層判定，見模組 docstring）。

    效能註：第 2 層的 ``db.get(Tenant, ...)`` 在同一 request/session 內因
    SQLAlchemy identity map 免重查；webhook 熱路徑同 session 多次呼叫只打
    一次 DB。
    """
    row = db.execute(
        select(TenantFeature).where(
            TenantFeature.tenant_id == tenant_id,
            TenantFeature.feature == feature,
        )
    ).scalar_one_or_none()
    if row is not None:
        return bool(row.enabled)

    # 第 2 層：方案 bundle（含試用）。延遲 import 避免 plans ↔ features 循環。
    from saas_mvp.models.tenant import Tenant
    from saas_mvp.services import plans as plans_svc

    tenant = db.get(Tenant, tenant_id)
    if tenant is not None and plans_svc.plan_includes(
        plans_svc.effective_plan(tenant), feature
    ):
        return True

    return settings.features_default_enabled


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
    """所有進階功能的開通狀態 + 月費 + 最新訂閱概況（供自助/管理頁）。

    subscription 欄位：該功能最新一筆 FeatureSubscription 的
    {status, total_success_times, last_charged_at}（一次查詢取每 feature
    max(id)，避免 N+1）；無訂閱紀錄（stub 模式開通/從未訂閱）為 None。
    """
    from sqlalchemy import func, select

    from saas_mvp.models.feature_subscription import FeatureSubscription

    # 每個 feature 取最新一筆訂閱（id 最大者）。
    latest_ids = (
        select(func.max(FeatureSubscription.id))
        .where(FeatureSubscription.tenant_id == tenant_id)
        .group_by(FeatureSubscription.feature)
        .scalar_subquery()
    )
    latest_subs = {
        s.feature: s
        for s in db.execute(
            select(FeatureSubscription).where(
                FeatureSubscription.id.in_(latest_ids)
            )
        ).scalars()
    }

    out = []
    for key in sorted(VALID_FEATURES):
        sub = latest_subs.get(key)
        out.append({
            "key": key,
            "label": _FEATURE_LABELS[key],
            "monthly_price_cents": settings.feature_monthly_price_cents,
            "enabled": is_enabled(db, tenant_id, key),
            "subscription": None if sub is None else {
                "status": sub.status,
                "total_success_times": sub.total_success_times or 0,
                "last_charged_at": sub.last_charged_at,
            },
        })
    return out


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
