"""公開退訂頁(R6-B1,PDPA)。

比照 customer_portal:公開 + public_limiter 限流、include_in_schema=False、
token 即能力(解析失敗一律 404 不洩漏存在性)。交易性通知不受退訂影響。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from saas_mvp.auth.ratelimit import public_limiter
from saas_mvp.db import get_db
from saas_mvp.services import customer_marketing
from saas_mvp.services.customer_marketing import UnsubscribeTokenNotFound

_PKG_DIR = Path(__file__).resolve().parent.parent  # src/saas_mvp
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

router = APIRouter(
    prefix="/unsubscribe",
    tags=["unsubscribe"],
    include_in_schema=False,
    dependencies=[Depends(public_limiter)],
)

_NOT_FOUND = HTMLResponse("<h1>連結不存在</h1>", status_code=status.HTTP_404_NOT_FOUND)


@router.get("/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str, request: Request, db: Session = Depends(get_db)):
    try:
        customer = customer_marketing.resolve_unsubscribe_token(db, token)
    except UnsubscribeTokenNotFound:
        return _NOT_FOUND
    return templates.TemplateResponse(
        "unsubscribe.html",
        {
            "request": request,
            "token": token,
            "opted_out": customer_marketing.is_opted_out(customer),
            "name": customer.display_name or "您",
        },
    )


@router.post("/{token}", response_class=HTMLResponse)
def unsubscribe_submit(
    token: str,
    request: Request,
    action: str = Form("out"),
    db: Session = Depends(get_db),
):
    try:
        customer = customer_marketing.resolve_unsubscribe_token(db, token)
    except UnsubscribeTokenNotFound:
        return _NOT_FOUND
    if action == "in":
        customer_marketing.opt_in(db, customer)
    else:
        customer_marketing.opt_out(db, customer)
    return RedirectResponse(
        f"/unsubscribe/{token}", status_code=status.HTTP_303_SEE_OTHER
    )
