"""payments 共用基座(P5 純搬移自 routers/payments.py):router/_log/_render_autosubmit。"""
from __future__ import annotations

import html
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


def _render_autosubmit(form: dict, action_url: str, title: str) -> HTMLResponse:
    inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v))}">'
        for k, v in form.items()
    )
    page = (
        f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
        "<body onload='document.forms[0].submit()'>"
        f"<p>正在前往綠界…</p>"
        f"<form method='post' action='{html.escape(action_url)}'>{inputs}"
        "<noscript><button type='submit'>前往綠界</button></noscript></form></body>"
    )
    return HTMLResponse(page)


