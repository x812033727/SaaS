"""Billing router — 帳單升降級端點。

端點
----
POST /billing/checkout  — 模擬結帳，設定任意 plan（checkout 不限方向）
POST /billing/upgrade   — 升級（new_plan 限額需高於當前）
POST /billing/downgrade — 降級（new_plan 限額需低於當前）；超量回 409
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from saas_mvp.deps import Actor, get_current_actor, get_db
from saas_mvp.models.tenant import Tenant
from saas_mvp.quota import PLAN_DAILY_LIMITS
from saas_mvp.services.billing import downgrade_plan, upgrade_plan

router = APIRouter(prefix="/billing", tags=["billing"])


# ── Pydantic schemas ──────────────────────────────────────────

class PlanRequest(BaseModel):
    plan: str


class BillingResponse(BaseModel):
    ok: bool
    plan: str
    payment_id: str


# ── 共用 helper ───────────────────────────────────────────────

def _get_tenant(actor: Actor, db: Session) -> Tenant:
    tenant = db.get(Tenant, actor.user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return tenant


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/checkout", response_model=BillingResponse)
def checkout(
    body: PlanRequest,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
) -> BillingResponse:
    """模擬結帳後立即生效改 plan（可任意方向，含初次訂閱）。

    回傳 payment_id 形如 "simulated_xxxxxxxxxxxx"，不發任何外部網路請求。
    """
    tenant = _get_tenant(actor, db)
    payment_id = upgrade_plan(db, tenant, body.plan, actor.user.id, reason="checkout")
    return BillingResponse(ok=True, plan=body.plan, payment_id=payment_id)


@router.post("/upgrade", response_model=BillingResponse)
def upgrade(
    body: PlanRequest,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
) -> BillingResponse:
    """升級到更高方案（new_plan 日限額須高於目前方案）。"""
    tenant = _get_tenant(actor, db)

    current_limit = PLAN_DAILY_LIMITS.get(tenant.plan, 0)
    new_limit = PLAN_DAILY_LIMITS.get(body.plan)
    if new_limit is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown plan: '{body.plan}'. Valid plans: {sorted(PLAN_DAILY_LIMITS)}",
        )
    if new_limit <= current_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plan '{body.plan}' is not an upgrade from '{tenant.plan}'.",
        )

    payment_id = upgrade_plan(db, tenant, body.plan, actor.user.id, reason="upgrade")
    return BillingResponse(ok=True, plan=body.plan, payment_id=payment_id)


@router.post("/downgrade", response_model=BillingResponse)
def downgrade(
    body: PlanRequest,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
) -> BillingResponse:
    """降級到較低方案（new_plan 日限額須低於目前方案）。

    若今日用量已超過新方案上限，回 409 Conflict 並附 current_usage/new_limit。
    """
    tenant = _get_tenant(actor, db)

    current_limit = PLAN_DAILY_LIMITS.get(tenant.plan, 0)
    new_limit = PLAN_DAILY_LIMITS.get(body.plan)
    if new_limit is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown plan: '{body.plan}'. Valid plans: {sorted(PLAN_DAILY_LIMITS)}",
        )
    if new_limit >= current_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plan '{body.plan}' is not a downgrade from '{tenant.plan}'.",
        )

    payment_id = downgrade_plan(db, tenant, body.plan, actor.user.id)
    return BillingResponse(ok=True, plan=body.plan, payment_id=payment_id)
