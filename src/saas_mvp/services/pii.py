"""隱私保護模式服務（PHASE 4-2）— 一次性 token PII 表單。

店家透過 LINE 推送一條表單連結（form_url），顧客在網頁填寫姓名/電話/生日，
資料寫回對應 Customer 檔，而非在聊天室明文索取個資。

設計：
* token 即能力：公開表單以 token 解析（get_by_token，不分租戶）；所有下游寫入
  一律 scope 到該請求的 tenant_id（防跨租戶污染）。
* 原子性：submit 在單一交易內 upsert customer + 回填 phone/birthday + 標記 submitted，
  一次 commit。
* 過期/重複提交：以 domain error 表達，由 router 轉成適當頁面。
"""

from __future__ import annotations

import datetime
import secrets

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.customer import Customer, upsert_customer_from_line
from saas_mvp.models.pii_request import (
    PII_PENDING,
    PII_SUBMITTED,
    PiiRequest,
)

# 對齊 Customer 欄位長度（display_name String(128) / phone String(32)）：
# 伺服器端先驗長度，超長回 422（避免 Postgres 寫入時 500、且不半寫顧客檔）。
_NAME_MAX = 128
_PHONE_MAX = 32


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class PiiError(Exception):
    """隱私表單 domain 錯誤（token 不存在/已過期/已使用）。"""


class PiiTokenNotFound(PiiError):
    pass


class PiiTokenExpired(PiiError):
    pass


class PiiTokenAlreadyUsed(PiiError):
    pass


def _is_expired(req: PiiRequest, now: datetime.datetime | None = None) -> bool:
    if req.expires_at is None:
        return False
    now = now or _utcnow()
    exp = req.expires_at
    # 容忍 naive/aware 混用（SQLite 取出多為 naive）。
    if exp.tzinfo is None:
        now = now.replace(tzinfo=None)
    return now > exp


def create_request(
    db: Session,
    *,
    tenant_id: int,
    line_user_id: str,
    ttl_minutes: int | None = None,
) -> PiiRequest:
    """建立一筆 pending PII 請求並 commit；token 即表單連結能力。"""
    if ttl_minutes is None:
        ttl_minutes = settings.pii_token_ttl_minutes
    now = _utcnow()
    req = PiiRequest(
        tenant_id=tenant_id,
        line_user_id=line_user_id,
        token=secrets.token_urlsafe(32),
        status=PII_PENDING,
        created_at=now,
        expires_at=now + datetime.timedelta(minutes=ttl_minutes),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def get_by_token(db: Session, token: str) -> PiiRequest | None:
    """以 token 解析請求（不分租戶；token 即能力）；查無回 None。"""
    return db.execute(
        select(PiiRequest).where(PiiRequest.token == token)
    ).scalar_one_or_none()


def form_url(req: PiiRequest) -> str:
    """組公開表單絕對網址（供 LINE 推送）。"""
    base = settings.public_base_url.rstrip("/")
    return f"{base}/pii/{req.token}"


def _parse_birthday(value: str | None) -> datetime.date | None:
    """安全解析生日字串（YYYY-MM-DD 或 YYYY/MM/DD）；解析失敗回 None。"""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def submit(
    db: Session,
    *,
    token: str,
    name: str | None,
    phone: str | None,
    birthday: str | None,
) -> Customer:
    """顧客提交表單：驗 token → upsert customer 回填 phone/birthday → 標記 submitted。

    單一交易、一次 commit。token 不存在/已過期/已使用 → 拋對應 PiiError。
    """
    req = get_by_token(db, token)
    if req is None:
        raise PiiTokenNotFound("token not found")
    if req.status != PII_PENDING:
        raise PiiTokenAlreadyUsed("token already used")
    if _is_expired(req):
        raise PiiTokenExpired("token expired")

    # 伺服器端長度上限：在任何 DB 寫入前先擋下，避免半寫顧客檔（DB 截斷/500）。
    if name and len(name.strip()) > _NAME_MAX:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"姓名長度不可超過 {_NAME_MAX} 個字。",
        )
    if phone and len(phone.strip()) > _PHONE_MAX:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"電話長度不可超過 {_PHONE_MAX} 個字。",
        )

    # 下游寫入一律 scope 到本請求的 tenant_id（防跨租戶污染）。
    customer = upsert_customer_from_line(
        db,
        tenant_id=req.tenant_id,
        line_user_id=req.line_user_id,
        display_name=(name.strip() if name and name.strip() else None),
        bump_booking=False,
    )
    if phone and phone.strip():
        customer.phone = phone.strip()
    parsed_bday = _parse_birthday(birthday)
    if parsed_bday is not None:
        customer.birthday = parsed_bday

    req.status = PII_SUBMITTED
    req.submitted_at = _utcnow()
    db.commit()
    db.refresh(customer)
    return customer


def push_form_link(db: Session, *, tenant_id: int, line_user_id: str) -> str:
    """供 LINE booking bot 呼叫：建立請求並回傳要推送的表單連結文字。

    僅在租戶開通 PRIVACY_MODE 時由呼叫端使用（行為閘在呼叫端）。本函式保持極簡：
    建請求 + 回傳可推送的訊息文字（含 form_url）。
    """
    req = create_request(db, tenant_id=tenant_id, line_user_id=line_user_id)
    url = form_url(req)
    return f"為保護您的個資，請點此連結填寫聯絡資訊：{url}"
