"""FAQ 知識庫服務 — CRUD（tenant_query）+ 關鍵字比對（餵 AI 助手 context）。

match() 以簡單的 case-insensitive contains 比對：問題或答案包含使用者輸入的
任一斷詞、或使用者輸入包含 FAQ 問題者，視為相關；依 sort_order 回傳。
"""

from __future__ import annotations

import re

from fastapi import HTTPException, status
from sqlalchemy import select
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


# 比對前的正規化：移除空白與標點（含全形 CJK 標點與全形符號），只留可辨識字元
# 做字元級比對。中文沒有空白可斷詞，純靠「整句包含」會漏掉「請問營業時間？」這類
# 加了綴詞的問法；改以字元 bigram 重疊衡量相似度。
_STRIP_RE = re.compile(
    r"[\s　 -⁯　-〿＀-￯!-/:-@\[-`{-~]+"
)

# build_context 預設帶入的 FAQ 上限（避免 context 過長 / 餵付費 LLM 過量）。
_CONTEXT_MAX_ENTRIES = 6


def _normalize(text: str) -> str:
    return _STRIP_RE.sub("", (text or "").lower())


def _char_ngrams(text: str, n: int = 2) -> set[str]:
    """字元級 n-gram（預設 bigram）。中文無空白，用連續字元窗格比對相似度。"""
    s = _normalize(text)
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def _relevance(query_norm: str, query_grams: set[str], faq: FAQEntry) -> int:
    """FAQ 與 query 的相關度分數（越高越相關，0 = 不相關）。"""
    fq_norm = _normalize(faq.question)
    fa_norm = _normalize(faq.answer)
    score = 0
    # 直接子字串（任一方向）→ 高權重，確保精準命中排最前。
    if query_norm and fq_norm and (fq_norm in query_norm or query_norm in fq_norm):
        score += 100
    if query_norm and fa_norm and query_norm in fa_norm:
        score += 50
    # 字元 bigram 重疊（問題權重高於答案）——解決中文模糊問法。
    score += 3 * len(query_grams & _char_ngrams(faq.question))
    score += len(query_grams & _char_ngrams(faq.answer))
    return score


def match(
    db: Session, tenant_id: int, question: str, *, top_k: int | None = None
) -> list[FAQEntry]:
    """挑出與 question 相關的有效 FAQ，依相關度（高→低）、sort_order 排序。

    比對採「子字串（任一方向）+ 字元 bigram 重疊」評分：中文加綴詞的模糊問法
    （如「請問營業時間？」對上「營業時間？幾點開到幾點？」）也能命中。
    top_k 提供時只回前 N 筆最相關。
    """
    rows = list_faqs(db, tenant_id=tenant_id, active_only=True)
    query_norm = _normalize(question)
    if not query_norm:
        return []
    query_grams = _char_ngrams(question)
    scored = [(faq, _relevance(query_norm, query_grams, faq)) for faq in rows]
    relevant = [(faq, s) for faq, s in scored if s > 0]
    relevant.sort(key=lambda fs: (-fs[1], fs[0].sort_order, fs[0].id))
    matched = [faq for faq, _ in relevant]
    return matched[:top_k] if top_k is not None else matched


def build_context(
    db: Session,
    tenant_id: int,
    question: str,
    *,
    max_entries: int = _CONTEXT_MAX_ENTRIES,
) -> str:
    """組 AI 助手 context：取最相關的前 N 筆 FAQ，串成 Q/A 文字餵給助手。"""
    matched = match(db, tenant_id, question, top_k=max_entries)
    return "\n".join(f"Q: {f.question}\nA: {f.answer}" for f in matched)


# ── D4 FAQ 自學:AI 答不好的問題 ──────────────────────────────────────────────

def record_unanswered(db: Session, *, tenant_id: int, question: str) -> None:
    """AI 無 FAQ 命中/回答失敗時 upsert(hash 去重 + hit 累加)。永不拋錯。"""
    import datetime as _dt
    import hashlib

    try:
        from saas_mvp.models.ai_unanswered_question import (
            UNANSWERED_OPEN,
            AiUnansweredQuestion,
        )

        q = (question or "").strip()
        if not q:
            return
        digest = hashlib.sha256(_normalize(q).encode()).hexdigest()
        row = db.execute(
            select(AiUnansweredQuestion).where(
                AiUnansweredQuestion.tenant_id == tenant_id,
                AiUnansweredQuestion.question_hash == digest,
            )
        ).scalar_one_or_none()
        now = _dt.datetime.now(_dt.timezone.utc)
        if row is None:
            db.add(AiUnansweredQuestion(
                tenant_id=tenant_id, question=q[:2000], question_hash=digest,
            ))
        else:
            row.hit_count = (row.hit_count or 0) + 1
            row.updated_at = now
            if row.status != UNANSWERED_OPEN:
                # 轉正/忽略後又被問到:重開,提醒店家答案沒接住
                row.status = UNANSWERED_OPEN
        db.commit()
    except Exception:  # noqa: BLE001 — 記錄失敗絕不影響回覆主流程
        db.rollback()


def list_unanswered(db: Session, *, tenant_id: int, limit: int = 50) -> list:
    from saas_mvp.models.ai_unanswered_question import (
        UNANSWERED_OPEN,
        AiUnansweredQuestion,
    )

    return list(db.execute(
        select(AiUnansweredQuestion)
        .where(
            AiUnansweredQuestion.tenant_id == tenant_id,
            AiUnansweredQuestion.status == UNANSWERED_OPEN,
        )
        .order_by(AiUnansweredQuestion.hit_count.desc(), AiUnansweredQuestion.id)
        .limit(limit)
    ).scalars())


def convert_unanswered(
    db: Session, *, tenant_id: int, unanswered_id: int, answer: str
) -> FAQEntry:
    """一鍵轉正式 FAQ(補答案);找不到/非本租戶拋 404。"""
    from saas_mvp.models.ai_unanswered_question import (
        UNANSWERED_CONVERTED,
        AiUnansweredQuestion,
    )

    row = db.execute(
        select(AiUnansweredQuestion).where(
            AiUnansweredQuestion.id == unanswered_id,
            AiUnansweredQuestion.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="unanswered question not found")
    if not (answer or "").strip():
        # 空答案會建出空白的「已啟用」FAQ 並讓該問題永遠標為已解決(丟失待答提示)。
        raise HTTPException(status_code=400, detail="answer must not be empty")
    faq = create_faq(
        db, tenant_id=tenant_id, question=row.question, answer=answer
    )
    row.status = UNANSWERED_CONVERTED
    db.commit()
    return faq


def dismiss_unanswered(db: Session, *, tenant_id: int, unanswered_id: int) -> None:
    from saas_mvp.models.ai_unanswered_question import (
        UNANSWERED_DISMISSED,
        AiUnansweredQuestion,
    )

    row = db.execute(
        select(AiUnansweredQuestion).where(
            AiUnansweredQuestion.id == unanswered_id,
            AiUnansweredQuestion.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if row is not None:
        row.status = UNANSWERED_DISMISSED
        db.commit()
