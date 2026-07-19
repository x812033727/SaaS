"""UI 子模組:會員分級設定 + 商品/服務 CSV 匯出。

R12-C3a:顧客 CRM 頁(列表/明細/標籤/點數/套票/匯入/匯出)已遷 console
並實體刪除;此檔僅保留尚無 console 對應的 loyalty 設定頁與兩個 CSV
匯出端點(顧客匯出已改由 API 層 /booking/customers/export.csv 提供)。
"""
from __future__ import annotations

from fastapi import Depends, Form, Request, status
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_owner,
    require_ui_user,
)
from saas_mvp.services import audit as audit_svc
from saas_mvp.services import catalog as catalog_svc
from saas_mvp.services import loyalty_config as loyalty_config_svc
from saas_mvp.services import shop as shop_svc

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx,
)


def _csv_response(rows: list[dict], fieldnames: list[str], filename: str) -> Response:
    import csv as _csv
    import io as _io

    buf = _io.StringIO()
    writer = _csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/products/export.csv")
def products_export(
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
) -> Response:
    rows = [
        {
            "name": p.name,
            "price_cents": p.price_cents,
            "stock": "" if p.stock is None else p.stock,
            "is_active": "yes" if p.is_active else "no",
            "description": p.description or "",
        }
        for p in shop_svc.list_products(db, tenant_id=actor.user.tenant_id)
    ]
    return _csv_response(
        rows,
        ["name", "price_cents", "stock", "is_active", "description"],
        "products.csv",
    )


@router.get("/services/export.csv")
def services_export(
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
) -> Response:
    rows = [
        {
            "name": s.name,
            "duration_minutes": s.duration_minutes or "",
            "price_cents": s.price_cents or 0,
            "is_active": "yes" if s.is_active else "no",
        }
        for s in catalog_svc.list_services(db, tenant_id=actor.user.tenant_id)
    ]
    return _csv_response(
        rows,
        ["name", "duration_minutes", "price_cents", "is_active"],
        "services.csv",
    )


# ── 會員分級設定（R6-B3，owner 限定）──────────────────────────────────────────


def _loyalty_ctx(request, actor, db, **extra):
    cfg = loyalty_config_svc.get_config(db, actor.user.tenant_id)
    return _ctx(
        request, actor,
        loyalty=cfg,
        # 無設定時顯示全域預設(讓表單有合理初值)。
        defaults={
            "silver_threshold": cfg.silver_threshold if cfg else 100,
            "gold_threshold": cfg.gold_threshold if cfg else 500,
            "regular_discount_pct": cfg.regular_discount_pct if cfg else 0,
            "silver_discount_pct": cfg.silver_discount_pct if cfg else 5,
            "gold_discount_pct": cfg.gold_discount_pct if cfg else 10,
            "points_per_booking": cfg.points_per_booking if cfg else 10,
        },
        **extra,
    )


@router.get("/loyalty", response_class=HTMLResponse)
def loyalty_settings(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("loyalty.html", _loyalty_ctx(request, actor, db))


@router.post("/loyalty", response_class=HTMLResponse)
def loyalty_settings_save(
    request: Request,
    silver_threshold: int = Form(...),
    gold_threshold: int = Form(...),
    regular_discount_pct: int = Form(...),
    silver_discount_pct: int = Form(...),
    gold_discount_pct: int = Form(...),
    points_per_booking: int = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    try:
        loyalty_config_svc.save_config(
            db,
            tenant_id=actor.user.tenant_id,
            silver_threshold=silver_threshold,
            gold_threshold=gold_threshold,
            regular_discount_pct=regular_discount_pct,
            silver_discount_pct=silver_discount_pct,
            gold_discount_pct=gold_discount_pct,
            points_per_booking=points_per_booking,
            updated_by_user_id=actor.user.id,
        )
    except loyalty_config_svc.LoyaltyConfigError as exc:
        return templates.TemplateResponse(
            "loyalty.html",
            _loyalty_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db, actor, action="loyalty.config.update",
        target=f"tenant:{actor.user.tenant_id}", request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "loyalty.html", _loyalty_ctx(request, actor, db, saved=True)
    )
