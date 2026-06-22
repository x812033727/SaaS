"""AI router — AI 客服問答 widget + FAQ 知識庫 CRUD（PHASE 4-1）。

受認證 + require_feature(AI_ASSISTANT)。/ai/ask 用 get_assistant() 回答（離線預設
StubAIAssistant，設定 SAAS_ANTHROPIC_API_KEY 後走 Claude），並以 faq.match 注入 context。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.ai import AIError, get_assistant
from saas_mvp.deps import get_current_user, get_db
from saas_mvp.models.user import User
from saas_mvp.services import faq as faq_svc
from saas_mvp.services.features import AI_ASSISTANT, require_feature

router = APIRouter(
    prefix="/ai",
    tags=["ai"],
    dependencies=[Depends(require_feature(AI_ASSISTANT))],
)


def _build_context(db: Session, tenant_id: int, question: str) -> str:
    """以 faq.match 組裝 context（matched FAQ 的 Q/A 串接）。"""
    matched = faq_svc.match(db, tenant_id, question)
    return "\n".join(f"Q: {f.question}\nA: {f.answer}" for f in matched)


# ── /ai/ask ──────────────────────────────────────────────────────────────────

class AskRequest(BaseModel):
    question: str = Field(min_length=1)


class AskResponse(BaseModel):
    answer: str
    source: str


@router.post("/ask", response_model=AskResponse)
def ask(
    body: AskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AskResponse:
    context = _build_context(db, current_user.tenant_id, body.question)
    assistant = get_assistant()
    try:
        result = assistant.answer(body.question, context)
    except AIError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=502, detail=f"AI backend error: {exc}")
    return AskResponse(answer=result.answer, source=result.source)


# ── FAQ CRUD ─────────────────────────────────────────────────────────────────

class FAQCreate(BaseModel):
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    sort_order: int = 0


class FAQUpdate(BaseModel):
    question: str | None = Field(default=None, min_length=1)
    answer: str | None = Field(default=None, min_length=1)
    sort_order: int | None = None
    is_active: bool | None = None


class FAQResponse(BaseModel):
    id: int
    tenant_id: int
    question: str
    answer: str
    is_active: bool
    sort_order: int

    model_config = {"from_attributes": True}


@router.post("/faq", response_model=FAQResponse, status_code=status.HTTP_201_CREATED)
def create_faq(
    body: FAQCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FAQResponse:
    faq = faq_svc.create_faq(
        db,
        tenant_id=current_user.tenant_id,
        question=body.question,
        answer=body.answer,
        sort_order=body.sort_order,
    )
    return FAQResponse.model_validate(faq)


@router.get("/faq", response_model=list[FAQResponse])
def list_faqs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[FAQResponse]:
    return [
        FAQResponse.model_validate(f)
        for f in faq_svc.list_faqs(db, tenant_id=current_user.tenant_id)
    ]


@router.put("/faq/{faq_id}", response_model=FAQResponse)
def update_faq(
    faq_id: int,
    body: FAQUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FAQResponse:
    faq = faq_svc.update_faq(
        db,
        tenant_id=current_user.tenant_id,
        faq_id=faq_id,
        question=body.question,
        answer=body.answer,
        sort_order=body.sort_order,
        is_active=body.is_active,
    )
    return FAQResponse.model_validate(faq)


@router.delete(
    "/faq/{faq_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
def delete_faq(
    faq_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    faq_svc.delete_faq(db, tenant_id=current_user.tenant_id, faq_id=faq_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
