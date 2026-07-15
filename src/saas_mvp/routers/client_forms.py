"""顧客免登入填寫預約諮詢表／同意書。"""

from __future__ import annotations
import json
from pathlib import Path
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from saas_mvp.auth.ratelimit import public_limiter
from saas_mvp.db import get_db
from saas_mvp.services import client_forms as forms_svc

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)
router = APIRouter(
    prefix="/client-forms",
    tags=["client-forms"],
    include_in_schema=False,
    dependencies=[Depends(public_limiter)],
)


def _ctx(request: Request, public, token: str, **extra):
    answers = (
        json.loads(public.request.answers_json) if public.request.answers_json else {}
    )
    return {
        "request": request,
        "form_request": public.request,
        "questions": public.questions,
        "answers": answers,
        "token": token,
        **extra,
    }


def _private(response: HTMLResponse) -> HTMLResponse:
    """表單可能含健康資料，禁止快取、索引與把能力連結帶到外站。"""
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@router.get("/{token}", response_class=HTMLResponse)
def form_page(token: str, request: Request, db: Session = Depends(get_db)):
    try:
        public = forms_svc.get_public(db, token)
    except forms_svc.ClientFormNotFound:
        return _private(
            HTMLResponse(
                "<h1>填寫連結不存在</h1>", status_code=status.HTTP_404_NOT_FOUND
            )
        )
    return _private(
        templates.TemplateResponse(
            "client_forms/public.html",
            _ctx(
                request,
                public,
                token,
                blocked=forms_svc.submission_block_reason(db, public.request),
            ),
        )
    )


@router.post("/{token}", response_class=HTMLResponse)
async def form_submit(token: str, request: Request, db: Session = Depends(get_db)):
    data = await request.form()
    answers = {
        key[2:]: str(value) for key, value in data.items() if key.startswith("q_")
    }
    try:
        forms_svc.submit(
            db,
            token=token,
            answers=answers,
            signer_name=str(data.get("signer_name") or ""),
            consent=data.get("consent") == "true",
            ip=request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent"),
        )
        public = forms_svc.get_public(db, token)
        return _private(
            templates.TemplateResponse(
                "client_forms/public.html", _ctx(request, public, token, saved=True)
            )
        )
    except forms_svc.ClientFormNotFound:
        return _private(
            HTMLResponse(
                "<h1>填寫連結不存在</h1>", status_code=status.HTTP_404_NOT_FOUND
            )
        )
    except forms_svc.ClientFormAlreadyCompleted:
        public = forms_svc.get_public(db, token)
        return _private(
            templates.TemplateResponse(
                "client_forms/public.html", _ctx(request, public, token)
            )
        )
    except forms_svc.ClientFormError as exc:
        db.rollback()
        public = forms_svc.get_public(db, token)
        return _private(
            templates.TemplateResponse(
                "client_forms/public.html",
                _ctx(request, public, token, error=str(exc), draft=data),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        )
