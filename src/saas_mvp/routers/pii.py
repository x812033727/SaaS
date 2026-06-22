"""隱私保護模式 PII 表單 router（PHASE 4-2）— 公開、無認證、include_in_schema=False。

* GET  /pii/{token} — 渲染表單（token 不存在 → 404；已過期/已使用 → 對應狀態頁）。
* POST /pii/{token} — 提交，寫回 Customer 並渲染完成頁。

安全：token 即能力，以 token 解析（不分租戶）；下游寫入一律 scope 到請求的 tenant_id。
CSRF（已知限制）：同 ui.py，MVP 僅靠 SameSite + 同源，未實作 per-request CSRF token；
此表單為公開填寫頁，無既有 session 可被攻擊者利用，風險低。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from saas_mvp.auth.ratelimit import public_limiter
from saas_mvp.db import get_db
from saas_mvp.services import pii as pii_svc
from saas_mvp.services.pii import (
    PII_PENDING,
    PiiTokenAlreadyUsed,
    PiiTokenExpired,
    PiiTokenNotFound,
)
from saas_mvp.models.pii_request import PII_SUBMITTED

_PKG_DIR = Path(__file__).resolve().parent.parent  # src/saas_mvp
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

router = APIRouter(
    prefix="/pii",
    tags=["pii"],
    include_in_schema=False,
    dependencies=[Depends(public_limiter)],
)


def _state_for(req) -> str:
    """把請求狀態映射到表單顯示狀態：form / used / expired。"""
    if req.status == PII_SUBMITTED:
        return "used"
    if pii_svc._is_expired(req):
        return "expired"
    return "form"


@router.get("/{token}", response_class=HTMLResponse)
def pii_form(token: str, request: Request, db: Session = Depends(get_db)):
    req = pii_svc.get_by_token(db, token)
    if req is None:
        return HTMLResponse("<h1>連結不存在</h1>", status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        "pii/form.html",
        {"request": request, "token": token, "state": _state_for(req)},
    )


@router.post("/{token}", response_class=HTMLResponse)
def pii_submit(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    name: str | None = Form(default=None),
    phone: str | None = Form(default=None),
    birthday: str | None = Form(default=None),
):
    try:
        pii_svc.submit(db, token=token, name=name, phone=phone, birthday=birthday)
    except PiiTokenNotFound:
        return HTMLResponse("<h1>連結不存在</h1>", status_code=status.HTTP_404_NOT_FOUND)
    except PiiTokenExpired:
        return templates.TemplateResponse(
            "pii/form.html",
            {"request": request, "token": token, "state": "expired"},
        )
    except PiiTokenAlreadyUsed:
        return templates.TemplateResponse(
            "pii/form.html",
            {"request": request, "token": token, "state": "used"},
        )
    return templates.TemplateResponse("pii/done.html", {"request": request})
