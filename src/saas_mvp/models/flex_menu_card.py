"""FlexMenuCard model — Flex carousel 內單張卡片（bubble）。

每張卡片對應一個 carousel bubble：可選 hero 圖、標題/副標、底部一個 action 按鈕。
action_type ∈ {'uri', 'postback', 'message'}；action_data 為對應的 URL / postback
data / 訊息文字。每個 menu 至多 12 張卡片（service 層強制）。
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    String,
    text,
)

from saas_mvp.db import Base


class FlexMenuCard(Base):
    __tablename__ = "flex_menu_cards"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    menu_id = Column(
        Integer,
        ForeignKey("flex_menus.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sort_order = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    title = Column(String(128), nullable=False)
    subtitle = Column(String(256), nullable=True)
    image_url = Column(String(512), nullable=True)
    bg_color = Column(String(16), nullable=True)
    icon = Column(String(32), nullable=True)
    action_type = Column(
        String(16), nullable=False, default="postback", server_default="postback"
    )
    action_data = Column(String(512), nullable=False)
