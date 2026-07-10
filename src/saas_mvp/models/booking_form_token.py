"""網頁預約表單 token（A1.1）— tokenized 深連結，不用 LIFF。

多租戶下 LIFF 的硬傷：LIFF app 掛在 LINE Login channel，其 userId 只有與
Messaging API channel 同 provider 才一致；本平台每店家自己的 provider，
平台級單一 LIFF 拿到的 userId 對不上顧客檔 → 改用一次性 token 攜帶身分
（比照 models/pii_request.py 模式）：bot 端發 token 連結 → LINE 內建瀏覽器
開表單 → token 解析出 (tenant_id, line_user_id)，免登入。
"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class BookingFormToken(Base):
    __tablename__ = "booking_form_tokens"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_user_id = Column(String(64), nullable=False)
    # 建單時回填顧客檔的顯示名（發 token 當下已知則帶上，免再打 profile API）。
    display_name = Column(String(128), nullable=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    # 成功建單後標記（一次性：一 token 一單；要再約請回 LINE 重新點按鈕）。
    used_at = Column(DateTime(timezone=True), nullable=True)
