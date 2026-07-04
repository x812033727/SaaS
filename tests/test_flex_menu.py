"""Flex 圖文選單測試 — 選單/卡片 CRUD、12 卡上限、payload 形狀、租戶隔離。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t  # noqa: F401
import saas_mvp.models.flex_menu as _fm  # noqa: F401
import saas_mvp.models.flex_menu_card as _fc  # noqa: F401

from saas_mvp.db import Base
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import flex_menu as flex_svc

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db, name="flex") -> int:
    t = Tenant(name=name, plan="free")
    db.add(t)
    db.commit()
    return t.id


class TestMenuCrud:
    def test_create_list_get_update(self, db):
        tid = _tenant(db)
        menu = flex_svc.create_menu(db, tenant_id=tid, title="主選單")
        assert menu.title == "主選單" and menu.is_active is True
        assert [m.id for m in flex_svc.list_menus(db, tenant_id=tid)] == [menu.id]
        got = flex_svc.get_menu(db, tenant_id=tid, menu_id=menu.id)
        assert got.id == menu.id
        flex_svc.update_menu(db, tenant_id=tid, menu_id=menu.id, is_active=False)
        assert flex_svc.get_menu(db, tenant_id=tid, menu_id=menu.id).is_active is False

    def test_get_missing_404(self, db):
        tid = _tenant(db)
        with pytest.raises(HTTPException) as ei:
            flex_svc.get_menu(db, tenant_id=tid, menu_id=999)
        assert ei.value.status_code == 404


class TestCardCrud:
    def test_add_and_list_cards(self, db):
        tid = _tenant(db)
        menu = flex_svc.create_menu(db, tenant_id=tid)
        c = flex_svc.add_card(
            db,
            tenant_id=tid,
            menu_id=menu.id,
            title="預約",
            action_type="postback",
            action_data="action=book",
            subtitle="立即預約",
        )
        assert c.title == "預約"
        cards = flex_svc.list_cards(db, tenant_id=tid, menu_id=menu.id)
        assert len(cards) == 1

    def test_invalid_action_type_422(self, db):
        tid = _tenant(db)
        menu = flex_svc.create_menu(db, tenant_id=tid)
        with pytest.raises(HTTPException) as ei:
            flex_svc.add_card(
                db, tenant_id=tid, menu_id=menu.id, title="x",
                action_type="bogus", action_data="d",
            )
        assert ei.value.status_code == 422

    def test_twelve_card_cap(self, db):
        tid = _tenant(db)
        menu = flex_svc.create_menu(db, tenant_id=tid)
        for i in range(12):
            flex_svc.add_card(
                db, tenant_id=tid, menu_id=menu.id, title=f"卡{i}",
                action_type="message", action_data=f"m{i}",
            )
        with pytest.raises(HTTPException) as ei:
            flex_svc.add_card(
                db, tenant_id=tid, menu_id=menu.id, title="第13",
                action_type="message", action_data="m13",
            )
        assert ei.value.status_code == 422

    def test_get_one_card(self, db):
        tid = _tenant(db)
        menu = flex_svc.create_menu(db, tenant_id=tid)
        c = flex_svc.add_card(
            db, tenant_id=tid, menu_id=menu.id, title="單查",
            action_type="message", action_data="hi",
        )
        got = flex_svc.get_card(db, tenant_id=tid, menu_id=menu.id, card_id=c.id)
        assert got.id == c.id and got.title == "單查"
        with pytest.raises(HTTPException) as ei:
            flex_svc.get_card(db, tenant_id=tid, menu_id=menu.id, card_id=999999)
        assert ei.value.status_code == 404
        # 跨租戶 → 404
        tid_b = _tenant(db, name="flex-b")
        with pytest.raises(HTTPException) as ei:
            flex_svc.get_card(db, tenant_id=tid_b, menu_id=menu.id, card_id=c.id)
        assert ei.value.status_code == 404

    def test_delete_card(self, db):
        tid = _tenant(db)
        menu = flex_svc.create_menu(db, tenant_id=tid)
        c = flex_svc.add_card(
            db, tenant_id=tid, menu_id=menu.id, title="x",
            action_type="uri", action_data="https://e.com",
        )
        flex_svc.delete_card(db, tenant_id=tid, menu_id=menu.id, card_id=c.id)
        assert flex_svc.list_cards(db, tenant_id=tid, menu_id=menu.id) == []


class TestPayload:
    def test_build_flex_payload_shape(self, db):
        tid = _tenant(db)
        menu = flex_svc.create_menu(db, tenant_id=tid, title="導覽")
        flex_svc.add_card(
            db, tenant_id=tid, menu_id=menu.id, title="預約",
            action_type="postback", action_data="action=book",
            image_url="https://img/x.png", subtitle="副標", bg_color="#FFEEDD",
        )
        flex_svc.add_card(
            db, tenant_id=tid, menu_id=menu.id, title="官網",
            action_type="uri", action_data="https://shop.example",
        )
        cards = flex_svc.list_cards(db, tenant_id=tid, menu_id=menu.id)
        payload = flex_svc.build_flex_payload(menu, cards)
        assert payload["type"] == "flex"
        assert payload["contents"]["type"] == "carousel"
        bubbles = payload["contents"]["contents"]
        assert len(bubbles) == 2 and len(bubbles) <= 12
        # 第一張帶 hero 圖、postback 按鈕
        assert bubbles[0]["hero"]["url"] == "https://img/x.png"
        act0 = bubbles[0]["footer"]["contents"][0]["action"]
        assert act0["type"] == "postback" and act0["data"] == "action=book"
        # 第二張 uri 按鈕
        act1 = bubbles[1]["footer"]["contents"][0]["action"]
        assert act1["type"] == "uri" and act1["uri"] == "https://shop.example"

    def test_payload_caps_at_12_bubbles(self, db):
        tid = _tenant(db)
        menu = flex_svc.create_menu(db, tenant_id=tid)
        # 直接以 list 模擬 >12（service 層上限另測）
        from saas_mvp.models.flex_menu_card import FlexMenuCard
        cards = [
            FlexMenuCard(
                tenant_id=tid, menu_id=menu.id, title=f"c{i}",
                action_type="message", action_data=f"m{i}", sort_order=i,
            )
            for i in range(20)
        ]
        payload = flex_svc.build_flex_payload(menu, cards)
        assert len(payload["contents"]["contents"]) == 12


class TestTenantIsolation:
    def test_cross_tenant_menu_404(self, db):
        t1 = _tenant(db, "a")
        t2 = _tenant(db, "b")
        menu = flex_svc.create_menu(db, tenant_id=t1)
        with pytest.raises(HTTPException) as ei:
            flex_svc.get_menu(db, tenant_id=t2, menu_id=menu.id)
        assert ei.value.status_code == 404

    def test_active_menu_per_tenant(self, db):
        t1 = _tenant(db, "a")
        t2 = _tenant(db, "b")
        m1 = flex_svc.create_menu(db, tenant_id=t1, is_active=True)
        flex_svc.create_menu(db, tenant_id=t2, is_active=True)
        active = flex_svc.get_active_menu(db, tenant_id=t1)
        assert active is not None and active.id == m1.id
