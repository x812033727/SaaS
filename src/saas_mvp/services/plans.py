"""方案（plan bundle）定義 — 方案為主幹、feature flag 為執行面的唯一真相來源。

商業模式（B1 變現翻正）：
  * free（免費版）  ：預約核心 + CRM + 服務目錄；員工上限 free_staff_limit（3）。
  * standard（標準版）：+ 自動提醒/異動通知/排班/店家頁/Flex 選單/無限員工。
  * pro（專業版）    ：+ 優惠券/商品 POS/多分店/行銷自動化/AI 客服/報表/隱私模式。

與 features 的關係：
  * ``features.is_enabled`` 三層判定 —— ①明確 TenantFeature 列（單點加購／admin
    覆寫）優先 ②無列時看 effective_plan 的 bundle ③最後 fallback
    ``settings.features_default_enabled``（正式環境 False）。
  * PUSH_BOOST 不入任何 bundle：唯一主推的單點加購（必須明確訂閱）。

試用（trial）：
  * ``tenant.trial_plan`` + ``tenant.trial_ends_at``：未過期時 ``effective_plan``
    回 trial_plan（通常 pro），過期自動回 ``tenant.plan`` —— **無需 cron 翻旗標**，
    到期即刻生效。既有租戶的 grandfathering 也用同一機制
    （ops/backfill_trial_grandfather.py）。

定價：settings 可環境覆寫（SAAS_PLAN_STANDARD_PRICE_CENTS…）。
"""

from __future__ import annotations

import datetime

from saas_mvp.config import settings
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import features as features_svc

# ── 方案常數 ─────────────────────────────────────────────────────────────────
PLAN_FREE = "free"
PLAN_STANDARD = "standard"
PLAN_PRO = "pro"
VALID_PLANS: tuple[str, ...] = (PLAN_FREE, PLAN_STANDARD, PLAN_PRO)
# 方案等級(C3 降級判斷用);未知值 normalize 後必在表內。
PLAN_RANK: dict[str, int] = {PLAN_FREE: 0, PLAN_STANDARD: 1, PLAN_PRO: 2}

_PLAN_LABELS: dict[str, str] = {
    PLAN_FREE: "免費版",
    PLAN_STANDARD: "標準版",
    PLAN_PRO: "專業版",
}

# 各方案內含的 feature 集合（含下級方案全部內容）。
_STANDARD_FEATURES = frozenset({
    features_svc.SERVICE_CATALOG,
    features_svc.AUTO_REMINDER,
    features_svc.BOOKING_NOTIFY,
    features_svc.STAFF_SCHEDULING,
    features_svc.PUBLIC_PROFILE,
    features_svc.FLEX_MENU,
    features_svc.UNLIMITED_STAFF,
    features_svc.WEB_BOOKING,
})

PLAN_BUNDLES: dict[str, frozenset[str]] = {
    # 免費版內建服務目錄（預約核心的一部分，不鎖）。
    PLAN_FREE: frozenset({features_svc.SERVICE_CATALOG}),
    PLAN_STANDARD: _STANDARD_FEATURES,
    PLAN_PRO: _STANDARD_FEATURES | frozenset({
        features_svc.COUPON_SYSTEM,
        features_svc.PRODUCT_SALES,
        features_svc.MULTI_LOCATION,
        features_svc.MARKETING_AUTO,
        features_svc.AI_ASSISTANT,
        features_svc.PRIVACY_MODE,
        features_svc.ADVANCED_REPORTING,
        features_svc.FEEDBACK_SURVEY,
        features_svc.AI_BOOKING_AGENT,
        features_svc.DEPOSIT_PAYMENT,
        features_svc.SERVICE_PACKAGES,
        features_svc.GIFT_CARDS,
        features_svc.CLIENT_FORMS,
    }),
    # PUSH_BOOST / AI_BOOST 刻意不在任何 bundle（單點加購）。
}


def plan_label(plan: str) -> str:
    return _PLAN_LABELS.get(plan, plan)


def plan_price_cents(plan: str) -> int:
    """方案月費（分）。free = 0。"""
    if plan == PLAN_STANDARD:
        return settings.plan_standard_price_cents
    if plan == PLAN_PRO:
        return settings.plan_pro_price_cents
    return 0


def normalize_plan(plan: str | None) -> str:
    """未知/空值一律視為 free（防禦舊資料或手改 DB）。"""
    return plan if plan in PLAN_BUNDLES else PLAN_FREE


def trial_active(tenant: Tenant, *, now: datetime.datetime | None = None) -> bool:
    """試用是否進行中：trial_plan 合法且 trial_ends_at 在未來。"""
    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    trial_plan = getattr(tenant, "trial_plan", None)
    ends = getattr(tenant, "trial_ends_at", None)
    if trial_plan not in PLAN_BUNDLES or ends is None:
        return False
    if ends.tzinfo is None:  # SQLite 可能吐 naive datetime
        ends = ends.replace(tzinfo=datetime.timezone.utc)
    return ends > effective_now


def effective_plan(
    tenant: Tenant, *, now: datetime.datetime | None = None
) -> str:
    """租戶目前生效方案：試用未過期回 trial_plan，否則回 tenant.plan。

    試用到期**即刻生效**（純計算，無需 cron 翻旗標）；trial_plan 值不合法
    或 trial_ends_at 缺失時忽略試用。
    """
    if trial_active(tenant, now=now):
        return tenant.trial_plan
    return normalize_plan(tenant.plan)


def plan_includes(plan: str, feature: str) -> bool:
    """該方案 bundle 是否內含此 feature。"""
    return feature in PLAN_BUNDLES.get(plan, frozenset())


def start_trial(
    tenant: Tenant, *, now: datetime.datetime | None = None
) -> None:
    """為租戶開啟試用（不 commit，由呼叫端提交）。

    方案與天數取 settings（SAAS_TRIAL_PLAN / SAAS_TRIAL_DAYS）；
    settings.trial_days <= 0 時視為停用試用，no-op。
    """
    if settings.trial_days <= 0:
        return
    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    tenant.trial_plan = normalize_plan(settings.trial_plan)
    tenant.trial_ends_at = effective_now + datetime.timedelta(days=settings.trial_days)


def list_plans(*, current: str | None = None) -> list[dict]:
    """方案清單（供定價頁/選方案頁渲染）。"""
    out = []
    for plan in VALID_PLANS:
        features = sorted(PLAN_BUNDLES[plan])
        out.append({
            "key": plan,
            "label": plan_label(plan),
            "monthly_price_cents": plan_price_cents(plan),
            "features": features,
            "feature_labels": [
                features_svc.feature_label(f) for f in features
            ],
            "is_current": plan == current,
        })
    return out
