"""UI 子模組(P4 純搬移自 routers/ui.py):預約行事曆(月曆/週曆)。"""
from __future__ import annotations

import datetime

from fastapi import Depends, Query, Request
from fastapi.responses import (
    HTMLResponse,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_user,
)
from saas_mvp.services import calendar_view as calendar_view_svc

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx,
)

# ── 預約行事曆（月曆 / 週曆 + 雙模式：顧客預約 / 員工排班） ─────────────────────
@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(
    request: Request,
    view: str = Query(default="month"),
    mode: str = Query(default="reservations"),
    date: str | None = Query(default=None),
    gcal_retry_queued: int = Query(default=0, ge=0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """後台預約行事曆。view=month|week；mode=reservations|staff；date=錨點(YYYY-MM-DD)。"""
    tid = actor.user.tenant_id
    today = datetime.date.today()
    try:
        anchor = datetime.date.fromisoformat(date) if date else today
    except ValueError:
        anchor = today

    # E1:GCal 連結狀態(指引卡用)。
    from saas_mvp.models.tenant_gcal_credential import TenantGcalCredential

    gcal_cred = db.execute(
        select(TenantGcalCredential).where(TenantGcalCredential.tenant_id == tid)
    ).scalar_one_or_none()
    from saas_mvp.services import gcal as gcal_svc

    gcal_sync_summary = gcal_svc.summary(db, tid)

    month_data = week_data = staff_grid = None
    if mode == "staff":
        staff_grid = calendar_view_svc.build_staff_grid(db, tenant_id=tid)
    elif view == "week":
        week_data = calendar_view_svc.build_week(db, tenant_id=tid, anchor=anchor)
    else:
        view = "month"
        month_data = calendar_view_svc.build_month(
            db, tenant_id=tid, year=anchor.year, month=anchor.month
        )

    return templates.TemplateResponse(
        "calendar.html",
        _ctx(
            request,
            actor,
            gcal_cred=gcal_cred,
            gcal_sync_summary=gcal_sync_summary,
            gcal_retry_queued=gcal_retry_queued,
            view=view,
            mode=mode,
            today=today,
            month_data=month_data,
            week_data=week_data,
            staff_grid=staff_grid,
        ),
    )
