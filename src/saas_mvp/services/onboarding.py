"""Onboarding checklist（B3）— 純推導、零新表、永不失真。

各步驟完成與否由既有資料推導（LineChannelConfig / Service / BookingSlot /
email_verified_at / plan），不存任何 onboarding 狀態 → 無遷移、不會與現實脫鉤。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.service import Service
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User


def checklist(db: Session, *, tenant: Tenant, user: User) -> list[dict]:
    """五步 onboarding 狀態（依建議完成順序）。"""
    line_cfg = db.execute(
        select(LineChannelConfig.id).where(LineChannelConfig.tenant_id == tenant.id)
    ).first() is not None
    service_count = db.execute(
        select(func.count(Service.id)).where(
            Service.tenant_id == tenant.id, Service.is_active.is_(True)
        )
    ).scalar_one()
    slot_count = db.execute(
        select(func.count(BookingSlot.id)).where(BookingSlot.tenant_id == tenant.id)
    ).scalar_one()

    from saas_mvp.services import plans as plans_svc

    plan_chosen = (
        plans_svc.normalize_plan(tenant.plan) != plans_svc.PLAN_FREE
        or plans_svc.trial_active(tenant)
    )

    return [
        {
            "key": "verify_email",
            "label": "驗證 Email",
            "done": user.email_verified_at is not None,
            "href": "/ui/account",
        },
        {
            "key": "line_config",
            "label": "綁定 LINE 官方帳號",
            "done": line_cfg,
            "href": "/ui/line-config",
        },
        {
            "key": "services",
            "label": "建立服務項目",
            "done": service_count > 0,
            "href": "/ui/services",
        },
        {
            "key": "slots",
            "label": "開放預約時段",
            "done": slot_count > 0,
            "href": "/ui/booking",
        },
        {
            "key": "plan",
            "label": "選擇方案（或試用中）",
            "done": plan_chosen,
            "href": "/ui/plan",
        },
    ]


def all_done(items: list[dict]) -> bool:
    return all(i["done"] for i in items)
