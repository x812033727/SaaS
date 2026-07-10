"""統一稽核日誌（F1）— append-only,敏感操作全記。

與 PlanChangeHistory / FeatureChangeHistory 等領域歷程互補:那些是領域事實,
本表是「誰在何時對什麼做了什麼」的操作軌跡(admin 停權/代管/帳務/LINE 設定)。
impersonator_user_id:代管(F2)期間 actor=被代管 owner、impersonator=admin。
"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    # 平台層動作(如跨租戶查詢)可為 NULL;租戶刪除保留軌跡(SET NULL)。
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_user_id = Column(Integer, nullable=True)          # 系統動作可為 NULL
    impersonator_user_id = Column(Integer, nullable=True)   # F2 代管時 = admin id
    action = Column(String(64), nullable=False)             # 如 admin.tenant.patch
    target = Column(String(128), nullable=True)             # 如 tenant:5
    detail_json = Column(Text, nullable=True)               # 白名單過濾後的摘要
    ip = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_action_created", "action", "created_at"),
    )
