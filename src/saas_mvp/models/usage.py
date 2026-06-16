"""ApiUsage model — per-tenant daily API call counter."""

from sqlalchemy import Column, Date, ForeignKey, Integer, UniqueConstraint, text
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


class ApiUsage(Base):
    __tablename__ = "api_usage"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    period = Column(Date, nullable=False)        # 計量日期（UTC date）
    count = Column(Integer, nullable=False, default=0)
    # 翻譯字數累計（與 count 獨立計量、獨立超額擋下）。
    # 兩端 default 必須同時存在：
    #   default=0          → ORM 端 INSERT 自動補 0
    #   server_default=text("0") → DB 端 DEFAULT 0
    #     - raw SQL INSERT 省略 char_count 時不撞 NOT NULL
    #     - ALTER TABLE ADD COLUMN ... NOT NULL DEFAULT 0 對既有列自動回填
    #     - SQLite/PostgreSQL 都吃同一行 DDL，無 DB 變體
    # 雙保險：既有列若升級前 char_count 仍是 NULL，仍由
    # ``_migrate_backfill_char_count()`` 一次性 UPDATE 回填 0。
    char_count = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    tenant = relationship("Tenant")

    __table_args__ = (
        UniqueConstraint("tenant_id", "period", name="uq_usage_tenant_period"),
    )
