"""Notes router — CRUD，受認證 + 租戶隔離管控。

所有操作均透過 services/notes.py，Router 只做輸入驗證 → Service 呼叫。
跨租戶存取一律由 Service 層回 404（不洩漏 ID 存在性）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db
from saas_mvp.models.user import User
from saas_mvp.services.notes import (
    create_note,
    delete_note,
    get_note,
    list_notes,
    update_note,
)

router = APIRouter(prefix="/notes", tags=["notes"])


# ─────────────────────────────── Schemas ─────────────────────────────────────

class NoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(default="", max_length=65536)


class NoteUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    content: str | None = Field(default=None, max_length=65536)


class NoteResponse(BaseModel):
    id: int
    title: str
    content: str
    owner_id: int
    tenant_id: int

    model_config = {"from_attributes": True}


# ─────────────────────────────── Endpoints ───────────────────────────────────

@router.post("/", response_model=NoteResponse, status_code=status.HTTP_201_CREATED)
def create(
    body: NoteCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NoteResponse:
    """建立 note（隸屬當前租戶）。"""
    note = create_note(
        db,
        tenant_id=current_user.tenant_id,
        owner_id=current_user.id,
        title=body.title,
        content=body.content,
    )
    return NoteResponse.model_validate(note)


@router.get("/", response_model=list[NoteResponse])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[NoteResponse]:
    """列出當前租戶所有 notes。"""
    notes = list_notes(db, tenant_id=current_user.tenant_id)
    return [NoteResponse.model_validate(n) for n in notes]


@router.get("/{note_id}", response_model=NoteResponse)
def get_one(
    note_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NoteResponse:
    """取得單一 note；跨租戶 → 404。"""
    note = get_note(db, tenant_id=current_user.tenant_id, note_id=note_id)
    return NoteResponse.model_validate(note)


@router.put("/{note_id}", response_model=NoteResponse)
def update_one(
    note_id: int,
    body: NoteUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NoteResponse:
    """更新 note；跨租戶 → 404。"""
    note = update_note(
        db,
        tenant_id=current_user.tenant_id,
        note_id=note_id,
        title=body.title,
        content=body.content,
    )
    return NoteResponse.model_validate(note)


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_one(
    note_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """刪除 note；跨租戶 → 404。"""
    delete_note(db, tenant_id=current_user.tenant_id, note_id=note_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
