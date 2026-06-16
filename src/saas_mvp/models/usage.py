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
    #
    # 兩端 default 必須同時存在（缺一就壞）：
    #   default=0              → ORM 端 INSERT 自動補 0（API/測試走 ORM 時）
    #   server_default=text("0")→ DB 端 DDL DEFAULT 0：
    #     · 既有 raw SQL INSERT 省略 char_count 時不撞 NOT NULL
    #     · ALTER TABLE ADD COLUMN ... NOT NULL DEFAULT 0 對既有列自動回填
    #     · SQLite/PostgreSQL 都吃同一行 DDL，無 DB 變體
    #
    # server_default 是 DDL 的一部分，會在 create_all / 顯式 ALTER 階段生效；
    # 但對**沒有走過 model schema** 的「真舊 DB」並不適用——例如早期曾以
    # `CREATE TABLE api_usage (..., char_count INTEGER NOT NULL)`（無 DEFAULT）
    # 建出的列，server_default 救不到既有 NULL。針對此情境，DB 啟動時
    # `_migrate_backfill_char_count()` 會對 NULL 列做一次性 UPDATE 回填 0。
    # 三層防護（ORM default / DB DDL / migration UPDATE）確保任何路徑
    # 產生的 NULL 都不會污染資料。
    char_count = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    tenant = relationship("Tenant")

    __table_args__ = (
        UniqueConstraint("tenant_id", "period", name="uq_usage_tenant_period"),
    )
