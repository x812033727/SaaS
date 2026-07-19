"""UI 子模組(P4 純搬移自 routers/ui.py):店家自助:AI 客服/FAQ + 後台 LINE 客服訊息 + SSE。"""
from __future__ import annotations


from fastapi import Depends, Form, Query, Request
from fastapi.responses import (
    HTMLResponse,
    StreamingResponse,
)
from sqlalchemy.orm import Session

from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_user,
)
from saas_mvp.line_client import (
    LinePushClient,
    get_push_client,
)
from saas_mvp.services import features as features_svc
from saas_mvp.services import faq as faq_svc
from saas_mvp.services import line_chat as line_chat_svc
from saas_mvp.services.events import broker as event_broker
from saas_mvp.ai import AIError, get_assistant
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _AI_QUESTION_MAX_LEN, _ctx, _require_ui_feature,
)
from saas_mvp.routers.ui._shared import _feature_locked

# ── 店家自助：AI 客服 / FAQ（AI_ASSISTANT） ─────────────────────────────────────


def _faq_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    extra.setdefault("editing_id", None)
    return _ctx(
        request,
        actor,
        faqs=faq_svc.list_faqs(db, tenant_id=tid),
        unanswered=faq_svc.list_unanswered(db, tenant_id=tid),
        **extra,
    )


@router.post("/faq/unanswered/{unanswered_id}/convert", response_class=HTMLResponse)
def faq_unanswered_convert(
    unanswered_id: int,
    request: Request,
    answer: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    error = None
    try:
        faq_svc.convert_unanswered(
            db,
            tenant_id=actor.user.tenant_id,
            unanswered_id=unanswered_id,
            answer=answer,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "faq.html", _faq_ctx(request, actor, db, error=error)
    )


@router.post("/faq/unanswered/{unanswered_id}/dismiss", response_class=HTMLResponse)
def faq_unanswered_dismiss(
    unanswered_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    faq_svc.dismiss_unanswered(
        db, tenant_id=actor.user.tenant_id, unanswered_id=unanswered_id
    )
    return templates.TemplateResponse("faq.html", _faq_ctx(request, actor, db))


@router.get("/faq", response_class=HTMLResponse)
def faq_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    return templates.TemplateResponse("faq.html", _faq_ctx(request, actor, db))


@router.get("/faq/list", response_class=HTMLResponse)
def faq_list_partial(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """FAQ 清單 partial；供編輯列取消時還原，避免把完整頁面嵌進卡片。"""
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    return templates.TemplateResponse("_faq_list.html", _faq_ctx(request, actor, db))


@router.post("/faq", response_class=HTMLResponse)
def faq_create(
    request: Request,
    question: str = Form(...),
    answer: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    tid = actor.user.tenant_id
    error = None
    try:
        faq_svc.create_faq(
            db, tenant_id=tid, question=question, answer=answer, sort_order=sort_order
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_faq_list.html", _faq_ctx(request, actor, db, error=error)
    )


@router.post("/faq/{faq_id}/delete", response_class=HTMLResponse)
def faq_delete(
    request: Request,
    faq_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    tid = actor.user.tenant_id
    error = None
    try:
        faq_svc.delete_faq(db, tenant_id=tid, faq_id=faq_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_faq_list.html", _faq_ctx(request, actor, db, error=error)
    )


@router.post("/faq/{faq_id}/toggle", response_class=HTMLResponse)
def faq_toggle(
    request: Request,
    faq_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    tid = actor.user.tenant_id
    error = None
    try:
        faq = faq_svc.get_faq(db, tenant_id=tid, faq_id=faq_id)
        faq_svc.update_faq(
            db, tenant_id=tid, faq_id=faq_id, is_active=not faq.is_active
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_faq_list.html", _faq_ctx(request, actor, db, error=error)
    )


@router.get("/faq/{faq_id}/edit", response_class=HTMLResponse)
def faq_edit_form(
    request: Request,
    faq_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    return templates.TemplateResponse(
        "_faq_list.html", _faq_ctx(request, actor, db, editing_id=faq_id)
    )


@router.post("/faq/{faq_id}/update", response_class=HTMLResponse)
def faq_update(
    request: Request,
    faq_id: int,
    question: str = Form(...),
    answer: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        faq_svc.update_faq(
            db,
            tenant_id=tid,
            faq_id=faq_id,
            question=question,
            answer=answer,
            sort_order=sort_order,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_id = faq_id
    return templates.TemplateResponse(
        "_faq_list.html",
        _faq_ctx(request, actor, db, error=error, editing_id=editing_id),
    )


@router.post("/ai-widget/ask", response_class=HTMLResponse)
def ai_widget_ask(
    request: Request,
    question: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """右下角浮動 AI 客服 widget 的問答端點（對標 vibeaico AI 客服 widget）。"""
    tid = actor.user.tenant_id
    answer = None
    error = None
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        error = "AI 客服未開通（專業版功能）。"
    elif len(question) > _AI_QUESTION_MAX_LEN:
        error = f"問題過長（上限 {_AI_QUESTION_MAX_LEN} 字），請精簡後再試。"
    else:
        assistant = get_assistant(db)
        context = faq_svc.build_context(
            db, tid, question, max_entries=assistant.context_max_entries
        )
        try:
            result = assistant.answer(question, context)
            answer = result.answer
        except AIError as exc:
            error = f"AI 後端錯誤：{exc}"
    return templates.TemplateResponse(
        "_ai_widget_answer.html",
        _ctx(request, actor, question=question, answer=answer, error=error),
    )


@router.post("/faq/ask", response_class=HTMLResponse)
def faq_ask(
    request: Request,
    question: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    tid = actor.user.tenant_id
    answer = None
    source = None
    error = None
    # 成本防護：避免超長問題被送往付費 LLM（cost amplification）。
    if len(question) > _AI_QUESTION_MAX_LEN:
        error = f"問題過長（上限 {_AI_QUESTION_MAX_LEN} 字），請精簡後再試。"
        return templates.TemplateResponse(
            "_ai_test.html",
            _ctx(
                request,
                actor,
                question=question,
                answer=answer,
                source=source,
                error=error,
            ),
        )
    assistant = get_assistant(db)
    context = faq_svc.build_context(
        db, tid, question, max_entries=assistant.context_max_entries
    )
    try:
        result = assistant.answer(question, context)
        answer = result.answer
        source = result.source
    except AIError as exc:
        error = f"AI 後端錯誤：{exc}"
    return templates.TemplateResponse(
        "_ai_test.html",
        _ctx(
            request, actor, question=question, answer=answer, source=source, error=error
        ),
    )


# ── 後台 LINE 客服訊息 + SSE 即時通知 ────────────────────────────────────────
@router.get("/line-chat", response_class=HTMLResponse)
def line_chat_page(
    request: Request,
    u: str | None = Query(default=None),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """客服對話頁：左側對話列表，右側選定對話的訊息序列 + 回覆框。"""
    tid = actor.user.tenant_id
    conversations = line_chat_svc.list_conversations(db, tenant_id=tid)
    selected = u
    selected_name = None
    messages = []
    if selected:
        messages = line_chat_svc.list_messages(db, tenant_id=tid, line_user_id=selected)
        for c in conversations:
            if c["line_user_id"] == selected:
                selected_name = c["display_name"]
                break
    return templates.TemplateResponse(
        "line_chat.html",
        _ctx(
            request,
            actor,
            conversations=conversations,
            selected=selected,
            selected_name=selected_name,
            line_user_id=selected,
            messages=messages,
        ),
    )


@router.post("/line-chat/{line_user_id}/reply", response_class=HTMLResponse)
def line_chat_reply(
    request: Request,
    line_user_id: str,
    text: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    push_client: LinePushClient = Depends(get_push_client),
):
    """店家從後台回覆顧客：LINE push → 存檔 outbound → SSE 廣播。

    實作抽至 line_chat_svc.send_reply(R5-A4,與 console JSON API 共用)。
    """
    tid = actor.user.tenant_id
    error = None
    try:
        line_chat_svc.send_reply(
            db,
            tenant_id=tid,
            line_user_id=line_user_id,
            text=text,
            push_client=push_client,
        )
    except line_chat_svc.LineChatError as exc:
        error = str(exc)

    messages = line_chat_svc.list_messages(db, tenant_id=tid, line_user_id=line_user_id)
    return templates.TemplateResponse(
        "_line_chat_messages.html",
        _ctx(request, actor, messages=messages, line_user_id=line_user_id, error=error),
    )


@router.get("/events")
async def line_events_stream(
    request: Request,
    actor: Actor = Depends(require_ui_user),
):
    """SSE 即時通知串流：新預約 / 取消 / 新訊息即時推送到後台。

    以 cookie 認證（EventSource 會自動帶上同源 cookie）。每租戶一條訂閱。
    """
    import asyncio
    import json as _json

    tenant_id = actor.user.tenant_id
    queue = await event_broker.subscribe(tenant_id)

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # 心跳，維持連線
                    continue
                etype = event.get("type", "message")
                data = _json.dumps(event, ensure_ascii=False)
                yield f"event: {etype}\ndata: {data}\n\n"
        finally:
            event_broker.unsubscribe(tenant_id, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


