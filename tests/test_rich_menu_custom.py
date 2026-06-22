"""Rich Menu 客製化擴充測試 — 6 主題 / 7+ 版型 / 3 模式 + list_* 輔助。"""

from __future__ import annotations

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


def _seed(db) -> int:
    t = Tenant(name="rmc", plan="free")
    db.add(t)
    db.flush()
    cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
    cfg.channel_secret = "s" * 32
    cfg.access_token = "a" * 40
    db.add(cfg)
    db.commit()
    return t.id


class TestCatalog:
    def test_six_themes(self):
        assert len(rm.THEMES) >= 6
        assert "rose_pink" in rm.THEMES
        # 既有主題保留
        for k in ("line_green", "ocean_blue", "dark"):
            assert k in rm.THEMES

    def test_seven_templates(self):
        assert len(rm.TEMPLATES) >= 7
        assert "booking3" in rm.TEMPLATES and "booking4" in rm.TEMPLATES

    def test_three_modes(self):
        assert set(rm.list_modes()) >= {"template", "custom_image", "vector"}

    def test_list_helpers(self):
        assert set(rm.list_themes()) == set(rm.THEMES.keys())
        assert set(rm.list_templates()) == set(rm.TEMPLATES.keys())


class TestModes:
    def test_template_mode_png(self):
        payload, image = rm.build_rich_menu_payload(
            "booking3", "rose_pink", mode="template"
        )
        assert image[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(image) > 0

    def test_vector_differs_from_template(self):
        _, tmpl = rm.build_rich_menu_payload("booking4", "ocean_blue", mode="template")
        _, vec = rm.build_rich_menu_payload("booking4", "ocean_blue", mode="vector")
        assert vec[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(vec) > 0
        assert vec != tmpl  # 分區色塊與純色不同

    def test_custom_image_passthrough(self):
        raw = b"\x89PNG\r\n\x1a\n custom-bytes"
        payload, image = rm.build_rich_menu_payload(
            "booking3", "dark", mode="custom_image", image_bytes=raw
        )
        assert image == raw

    def test_custom_image_requires_bytes(self):
        with pytest.raises(HTTPException) as ei:
            rm.build_rich_menu_payload("booking3", "dark", mode="custom_image")
        assert ei.value.status_code == 400


class TestApplyModes:
    def test_apply_vector_mode(self, db):
        tid = _seed(db)
        client = FakeLineRichMenuClient()
        status = rm.apply_rich_menu(
            db, tid, template="grid2x3", theme="rose_pink",
            client=client, mode="vector",
        )
        assert status["applied"] is True
        assert client.uploaded and client.uploaded[0][1] > 0  # image len > 0

    def test_apply_custom_image_mode(self, db):
        tid = _seed(db)
        client = FakeLineRichMenuClient()
        raw = rm.solid_png(100, 100, (1, 2, 3))
        rm.apply_rich_menu(
            db, tid, template="booking3", theme="dark",
            client=client, mode="custom_image", image_bytes=raw,
        )
        assert client.uploaded[0][1] == len(raw)

    def test_unknown_mode_400(self, db):
        tid = _seed(db)
        with pytest.raises(HTTPException) as ei:
            rm.apply_rich_menu(
                db, tid, template="booking3", theme="dark",
                client=FakeLineRichMenuClient(), mode="bogus",
            )
        assert ei.value.status_code == 400

    def test_new_template_applies(self, db):
        tid = _seed(db)
        client = FakeLineRichMenuClient()
        status = rm.apply_rich_menu(
            db, tid, template="grid1x2", theme="line_green", client=client
        )
        assert status["template"] == "grid1x2"
        assert len(client.created) == 1
