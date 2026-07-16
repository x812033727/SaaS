"""AI router — AI 客服問答 widget + FAQ 知識庫 CRUD（PHASE 4-1）。

受認證 + require_feature(AI_ASSISTANT)。/ai/ask 用 get_assistant() 回答（離線預設
StubAIAssistant，後台或環境設定 MiniMax 後走真實模型），並以 faq.match 注入 context。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.ai import AIError, get_assistant
from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services import faq as faq_svc
from saas_mvp.services.features import AI_ASSISTANT, require_feature

router = APIRouter(
    prefix="/ai",
    tags=["ai"],
    dependencies=[
        Depends(require_rate_limit),
        Depends(require_feature(AI_ASSISTANT)),
    ],
)


def _build_context(
    db: Session, tenant_id: int, question: str, max_entries: int
) -> str:
    """以 faq.build_context 組裝 context（最相關 FAQ 的 Q/A 串接）。

    max_entries 由 backend 決定：stub 只回最相關 1 筆（否則會把多筆 FAQ 全列出），
    真 LLM 則可吃多筆綜合作答。
    """
    return faq_svc.build_context(db, tenant_id, question, max_entries=max_entries)


# ── /ai/ask ──────────────────────────────────────────────────────────────────

class AskRequest(BaseModel):
    # max_length 防成本放大：超長 prompt 被送往付費 MiniMax API。
    question: str = Field(min_length=1, max_length=2000)


class AskResponse(BaseModel):
    answer: str
    source: str


@router.post("/ask", response_model=AskResponse)
def ask(
    body: AskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AskResponse:
    assistant = get_assistant(db)
    context = _build_context(
        db, current_user.tenant_id, body.question, assistant.context_max_entries
    )
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


@router.get("/faq/{faq_id}", response_model=FAQResponse)
def get_faq(
    faq_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FAQResponse:
    faq = faq_svc.get_faq(db, tenant_id=current_user.tenant_id, faq_id=faq_id)
    return FAQResponse.model_validate(faq)


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
