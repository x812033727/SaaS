"""「我的預約」Flex Carousel 測試（對標 vibeaico；含取消按鈕、12 張上限）。"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.db import Base, import_all_models  # noqa: E402

import_all_models()

from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.routers import line_webhook as lw  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402

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


def _tenant(db) -> int:
    t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
    db.add(t)
    db.flush()
    return t.id


def _book(db, tid, *, line_user_id, when):
    slot = BookingSlot(tenant_id=tid, slot_start=when, max_capacity=10)
    db.add(slot)
    db.commit()
    return booking_svc.book_slot(
        db, tenant_id=tid, slot_id=slot.id, party_size=2, line_user_id=line_user_id
    )


def test_my_returns_flex_carousel_with_cancel(db):
    tid = _tenant(db)
    resv = _book(db, tid, line_user_id="Ume", when=datetime.datetime(2030, 7, 1, 15, 0))
    out = lw._try_conversational(db, tid, "my", {}, "Ume")
    assert out is not None
    text, quick, flex = out
    assert text is None and quick is None
    assert flex["contents"]["type"] == "carousel"
    bubbles = flex["contents"]["contents"]
    assert len(bubbles) == 1
    # 卡片含預約編號與時間
    body_texts = [c["text"] for c in bubbles[0]["body"]["contents"]]
    assert any(f"#{resv.id}" in t for t in body_texts)
    assert any("07/01 15:00" in t for t in body_texts)
    # footer 兩顆按鈕：改期（primary）在前、取消在後，postback 各帶 reservation_id
    buttons = bubbles[0]["footer"]["contents"]
    datas = [b["action"]["data"] for b in buttons]
    assert f"action=reschedule&reservation_id={resv.id}" in datas
    assert f"action=cancel&reservation_id={resv.id}" in datas


def test_my_no_reservations_text(db):
    tid = _tenant(db)
    out = lw._try_conversational(db, tid, "my", {}, "Unobody")
    assert out is not None
    text, quick, flex = out
    assert flex is None
    assert "沒有預約" in text


def test_my_caps_at_12(db):
    tid = _tenant(db)
    base = datetime.datetime(2030, 8, 1, 9, 0)
    for i in range(15):
        _book(db, tid, line_user_id="Umany", when=base + datetime.timedelta(days=i))
    out = lw._try_conversational(db, tid, "my", {}, "Umany")
    _, _, flex = out
    assert len(flex["contents"]["contents"]) == 12  # carousel 上限
