"""PiiRequest model — LINE 隱私保護模式的一次性 PII 表單請求（PHASE 4-2）。

店家不在 LINE 聊天室直接索取顧客個資，改以一次性 token 連結引導顧客在網頁填寫。
每筆請求綁定 (tenant_id, line_user_id)，token 即能力（unique），有過期時間。
顧客提交後 status → submitted，並把 phone/birthday 寫回對應 Customer 檔。

狀態：pending（待填）→ submitted（已填）/ expired（逾時，由讀取邊界判定）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
)

from saas_mvp.db import Base

PII_PENDING = "pending"
PII_SUBMITTED = "submitted"
PII_EXPIRED = "expired"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class PiiRequest(Base):
    __tablename__ = "pii_requests"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_user_id = Column(String(64), nullable=False)
    # token 即能力；公開表單以此解析（不分租戶），下游寫入一律 scope 到本列 tenant_id。
    token = Column(String(64), nullable=False, unique=True, index=True)
    status = Column(
        String(16), nullable=False, default=PII_PENDING, server_default=PII_PENDING
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
