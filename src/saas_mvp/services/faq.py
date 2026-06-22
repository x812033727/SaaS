"""FAQ 知識庫服務 — CRUD（tenant_query）+ 關鍵字比對（餵 AI 助手 context）。

match() 以簡單的 case-insensitive contains 比對：問題或答案包含使用者輸入的
任一斷詞、或使用者輸入包含 FAQ 問題者，視為相關；依 sort_order 回傳。
"""

from __future__ import annotations

import re

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from saas_mvp.models.faq_entry import FAQEntry
from saas_mvp.services.tenants import tenant_query


def _get_or_404(db: Session, tenant_id: int, faq_id: int) -> FAQEntry:
    faq = (
        tenant_query(db, FAQEntry, tenant_id).filter(FAQEntry.id == faq_id).first()
    )
    if faq is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="FAQ not found"
        )
    return faq


def create_faq(
    db: Session,
    *,
    tenant_id: int,
    question: str,
    answer: str,
    sort_order: int = 0,
) -> FAQEntry:
    faq = FAQEntry(
        tenant_id=tenant_id,
        question=question,
        answer=answer,
        sort_order=sort_order,
    )
    db.add(faq)
    db.commit()
    db.refresh(faq)
    return faq


def list_faqs(
    db: Session, *, tenant_id: int, active_only: bool = False
) -> list[FAQEntry]:
    q = tenant_query(db, FAQEntry, tenant_id)
    if active_only:
        q = q.filter(FAQEntry.is_active.is_(True))
    return q.order_by(FAQEntry.sort_order, FAQEntry.id).all()


def get_faq(db: Session, *, tenant_id: int, faq_id: int) -> FAQEntry:
    return _get_or_404(db, tenant_id, faq_id)


def update_faq(
    db: Session,
    *,
    tenant_id: int,
    faq_id: int,
    question: str | None = None,
    answer: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
) -> FAQEntry:
    faq = _get_or_404(db, tenant_id, faq_id)
    if question is not None:
        faq.question = question
    if answer is not None:
        faq.answer = answer
    if sort_order is not None:
        faq.sort_order = sort_order
    if is_active is not None:
        faq.is_active = is_active
    db.commit()
    db.refresh(faq)
    return faq


def delete_faq(db: Session, *, tenant_id: int, faq_id: int) -> None:
    faq = _get_or_404(db, tenant_id, faq_id)
    db.delete(faq)
    db.commit()


def _tokens(text: str) -> list[str]:
    """切出可比對的斷詞（去除標點，過短的略過）。"""
    parts = re.split(r"\s+", text.strip())
    return [p for p in (s.strip() for s in parts) if len(p) >= 2]


def match(db: Session, tenant_id: int, question: str) -> list[FAQEntry]:
    """挑出與 question 相關的有效 FAQ（case-insensitive contains）。

    比對規則（任一成立即相關）：
      - FAQ 問題或答案包含使用者輸入（或其斷詞之一）。
      - 使用者輸入包含 FAQ 問題。
    依 sort_order 回傳。
    """
    rows = list_faqs(db, tenant_id=tenant_id, active_only=True)
    if not question.strip():
        return []
    q_low = question.lower()
    q_tokens = [t.lower() for t in _tokens(question)]
    matched: list[FAQEntry] = []
    for faq in rows:
        fq = (faq.question or "").lower()
        fa = (faq.answer or "").lower()
        if fq and fq in q_low:
            matched.append(faq)
            continue
        if q_low in fq or q_low in fa:
            matched.append(faq)
            continue
        if any(tok in fq or tok in fa for tok in q_tokens):
            matched.append(faq)
    return matched
