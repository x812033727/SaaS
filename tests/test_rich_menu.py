"""Rich Menu 服務測試 — PNG 產生、payload、套用流程、清除、錯誤分支。"""

from __future__ import annotations

import struct

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t  # noqa: F401
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401

from saas_mvp.db import Base
from saas_mvp.line_client import FakeLineRichMenuClient
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import rich_menu as rm

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


def _seed(db, *, with_cfg=True) -> int:
    t = Tenant(name="rm_test", plan="free")
    db.add(t)
    db.flush()
    if with_cfg:
        cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
        cfg.channel_secret = "s" * 32
        cfg.access_token = "a" * 40
        db.add(cfg)
    db.commit()
    return t.id


class TestPng:
    def test_solid_png_signature_and_dims(self):
        png = rm.solid_png(2500, 843, (6, 199, 85))
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        # IHDR width/height 位於 byte 16..24
        width, height = struct.unpack(">II", png[16:24])
        assert (width, height) == (2500, 843)
        # 純色高度壓縮率高，輸出應遠小於原始 raw
        assert len(png) < 2500 * 843 * 3


class TestPayload:
    def test_build_payload_areas_count_matches_buttons(self):
        payload, image = rm.build_rich_menu_payload("booking4", "ocean_blue")
        assert payload["size"] == {"width": 2500, "height": 1686}
        # selected=true 才會在 LINE 聊天室預設展開圖片；false 只顯示底部文字列。
        assert payload["selected"] is True
        assert len(payload["areas"]) == 4
        datas = [a["action"]["data"] for a in payload["areas"]]
        assert "action=book" in datas and "action=slots" in datas
        assert image[:8] == b"\x89PNG\r\n\x1a\n"

    def test_areas_tile_full_width(self):
        payload, _ = rm.build_rich_menu_payload("booking3", "dark")
        # 三欄應覆蓋整個寬度（最後一欄補餘數）
        total = sum(a["bounds"]["width"] for a in payload["areas"])
        assert total == 2500


class TestApply:
    def test_apply_calls_all_steps_and_stores(self, db):
        tid = _seed(db)
        client = FakeLineRichMenuClient()
        status = rm.apply_rich_menu(db, tid, template="booking3", theme="line_green", client=client)
        assert status["applied"] is True
        assert status["template"] == "booking3"
        # client 四步：create + upload + set_default（無舊選單故不 delete）
        assert len(client.created) == 1
        assert len(client.uploaded) == 1
        assert len(client.defaulted) == 1
        assert client.deleted == []
        # cfg 已存 rich_menu_id
        cfg = db.query(LineChannelConfig).filter_by(tenant_id=tid).one()
        assert cfg.rich_menu_id == status["rich_menu_id"]

    def test_reapply_deletes_old(self, db):
        tid = _seed(db)
        client = FakeLineRichMenuClient()
        first = rm.apply_rich_menu(db, tid, template="booking3", theme="dark", client=client)
        second = rm.apply_rich_menu(db, tid, template="booking4", theme="ocean_blue", client=client)
        assert client.deleted == [first["rich_menu_id"]]  # 重套刪舊
        assert second["template"] == "booking4"

    def test_invalid_template_400(self, db):
        tid = _seed(db)
        with pytest.raises(HTTPException) as ei:
            rm.apply_rich_menu(db, tid, template="nope", theme="dark", client=FakeLineRichMenuClient())
        assert ei.value.status_code == 400

    def test_invalid_theme_400(self, db):
        tid = _seed(db)
        with pytest.raises(HTTPException) as ei:
            rm.apply_rich_menu(db, tid, template="booking3", theme="neon", client=FakeLineRichMenuClient())
        assert ei.value.status_code == 400

    def test_no_line_config_404(self, db):
        tid = _seed(db, with_cfg=False)
        with pytest.raises(HTTPException) as ei:
            rm.apply_rich_menu(db, tid, template="booking3", theme="dark", client=FakeLineRichMenuClient())
        assert ei.value.status_code == 404

    def test_api_failure_502(self, db):
        tid = _seed(db)
        client = FakeLineRichMenuClient(fail_on="create")
        with pytest.raises(HTTPException) as ei:
            rm.apply_rich_menu(db, tid, template="booking3", theme="dark", client=client)
        assert ei.value.status_code == 502


class TestClear:
    def test_clear_deletes_and_resets(self, db):
        tid = _seed(db)
        client = FakeLineRichMenuClient()
        applied = rm.apply_rich_menu(db, tid, template="booking3", theme="dark", client=client)
        status = rm.clear_rich_menu(db, tid, client=client)
        assert status["applied"] is False
        assert applied["rich_menu_id"] in client.deleted
        cfg = db.query(LineChannelConfig).filter_by(tenant_id=tid).one()
        assert cfg.rich_menu_id is None
