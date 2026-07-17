"""可設定 loyalty 分級/折扣/集點率(R6-B3)。

per-tenant 設定覆寫全域 settings 預設;無設定 → 全域預設(向後相容)。
membership 的純函式(recompute_tier / tier_discount_percent)接受本模組算出的
thresholds / discounts,故仍可離線單測。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.tenant_loyalty_config import TenantLoyaltyConfig


class LoyaltyConfigError(ValueError):
    """設定驗證失敗(使用者可讀訊息)。"""


def get_config(db: Session, tenant_id: int) -> TenantLoyaltyConfig | None:
    return db.execute(
        select(TenantLoyaltyConfig).where(
            TenantLoyaltyConfig.tenant_id == tenant_id
        )
    ).scalar_one_or_none()


def thresholds_for(config: TenantLoyaltyConfig | None) -> list[tuple[int, str]]:
    """回 (門檻, 等級) 由高到低;無 config → 全域預設 [(500,gold),(100,silver),(0,regular)]。"""
    if config is None:
        return [(500, "gold"), (100, "silver"), (0, "regular")]
    return [
        (config.gold_threshold, "gold"),
        (config.silver_threshold, "silver"),
        (0, "regular"),
    ]


def discounts_for(config: TenantLoyaltyConfig | None) -> dict[str, int]:
    """各級結帳折扣百分比;無 config → 全域 settings。"""
    if config is None:
        return {
            "gold": settings.tier_discount_gold_percent,
            "silver": settings.tier_discount_silver_percent,
            "regular": settings.tier_discount_regular_percent,
        }
    return {
        "gold": config.gold_discount_pct,
        "silver": config.silver_discount_pct,
        "regular": config.regular_discount_pct,
    }


def points_per_booking_for(config: TenantLoyaltyConfig | None) -> int:
    return config.points_per_booking if config is not None else settings.points_per_booking


def save_config(
    db: Session,
    *,
    tenant_id: int,
    silver_threshold: int,
    gold_threshold: int,
    regular_discount_pct: int,
    silver_discount_pct: int,
    gold_discount_pct: int,
    points_per_booking: int,
    updated_by_user_id: int | None = None,
) -> TenantLoyaltyConfig:
    """upsert 租戶 loyalty 設定(表單路徑,本函式 commit)。"""
    if not (0 <= silver_threshold < gold_threshold):
        raise LoyaltyConfigError("白銀門檻需 ≥0 且小於黃金門檻。")
    for pct in (regular_discount_pct, silver_discount_pct, gold_discount_pct):
        if not 0 <= pct <= 100:
            raise LoyaltyConfigError("折扣百分比需介於 0–100。")
    if points_per_booking < 0:
        raise LoyaltyConfigError("每筆預約集點數不可為負。")

    row = get_config(db, tenant_id)
    if row is None:
        row = TenantLoyaltyConfig(tenant_id=tenant_id)
        db.add(row)
    row.silver_threshold = silver_threshold
    row.gold_threshold = gold_threshold
    row.regular_discount_pct = regular_discount_pct
    row.silver_discount_pct = silver_discount_pct
    row.gold_discount_pct = gold_discount_pct
    row.points_per_booking = points_per_booking
    row.updated_by_user_id = updated_by_user_id
    db.commit()
    db.refresh(row)
    return row
