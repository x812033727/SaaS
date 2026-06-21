"""OrderItem model — 訂單明細列。

下單時快照商品名稱與單價（name_snapshot / unit_price_cents），商品日後改價/改名
不影響既有訂單。line_total_cents = unit_price_cents * qty。
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, String

from saas_mvp.db import Base


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(
        Integer,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id = Column(
        Integer,
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name_snapshot = Column(String(128), nullable=False)
    unit_price_cents = Column(Integer, nullable=False)
    qty = Column(Integer, nullable=False)
    line_total_cents = Column(Integer, nullable=False)
