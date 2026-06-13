"""Note 服務層 — CRUD，所有查詢強制帶 tenant_id。

規則：
- 每個函式都接受 tenant_id，並透過 tenant_query() 隔離。
- 找不到（或不屬於此租戶）一律 404，不回 403，避免 ID 枚舉攻擊。
- Router 不得直接 db.query(Note)，必須透過這裡。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from saas_mvp.models.note import Note
from saas_mvp.services.tenants import tenant_query


def _get_or_404(db: Session, tenant_id: int, note_id: int) -> Note:
    """抓取 note；不存在或跨租戶一律 404（不洩漏 ID 存在性）。"""
    note = (
        tenant_query(db, Note, tenant_id)
        .filter(Note.id == note_id)
        .first()
    )
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return note


def create_note(
    db: Session,
    *,
    tenant_id: int,
    owner_id: int,
    title: str,
    content: str = "",
) -> Note:
    note = Note(title=title, content=content, owner_id=owner_id, tenant_id=tenant_id)
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


def get_note(db: Session, *, tenant_id: int, note_id: int) -> Note:
    return _get_or_404(db, tenant_id, note_id)


def list_notes(db: Session, *, tenant_id: int) -> list[Note]:
    return tenant_query(db, Note, tenant_id).all()


def update_note(
    db: Session,
    *,
    tenant_id: int,
    note_id: int,
    title: str | None = None,
    content: str | None = None,
) -> Note:
    note = _get_or_404(db, tenant_id, note_id)
    if title is not None:
        note.title = title
    if content is not None:
        note.content = content
    db.commit()
    db.refresh(note)
    return note


def delete_note(db: Session, *, tenant_id: int, note_id: int) -> None:
    note = _get_or_404(db, tenant_id, note_id)
    db.delete(note)
    db.commit()
