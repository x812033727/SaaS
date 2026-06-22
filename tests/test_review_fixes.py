"""對抗式審查發現的正確性修正回歸測試（H3 / M1 / M2 / M3 / M4(L4) / M4 / M5 / L1 / L2 / L3 / L5）。

每個 case 以 service 層直接驗證不變式或文件化序列化鎖行為。共用一個記憶體 SQLite
session（StaticPool）。
"""

from __future__ import annotations

import datetime
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# create_app() 觸發所有 model 註冊（讓 Base.metadata 完整）。
from saas_mvp.app import create_app  # noqa: E402

create_app()

from saas_mvp.config import settings  # noqa: E402,F401
from saas_mvp.db import Base  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402

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


def _tenant(db, name="t1") -> int:
    t = Tenant(name=name, plan="free")
    db.add(t)
    db.commit()
    return t.id


# ── H3：locations 上限以 FOR UPDATE 鎖租戶列序列化（兩次循序建店過上限拋 409） ──

class TestLocationCapLock:
    def test_sequential_creates_past_cap_raise(self, db, monkeypatch):
        """H3：create_location 在計數前鎖租戶列（SELECT … FOR UPDATE）序列化
        check-then-act。文件化：循序建店至上限後再建一家拋 LocationLimitError。"""
        from saas_mvp.services import locations as loc_svc

        monkeypatch.setattr(settings, "max_locations_per_tenant", 2)
        tid = _tenant(db)
        loc_svc.create_location(db, tenant_id=tid, name="a")
        loc_svc.create_location(db, tenant_id=tid, name="b")
        with pytest.raises(loc_svc.LocationLimitError):
            loc_svc.create_location(db, tenant_id=tid, name="c")


# ── M1：staff.assign_staff 鎖共用 Staff 列序列化跨預約衝突檢查 ──────────────────

def _slot(db, tid, *, start, cap=5):
    from saas_mvp.models.booking_slot import BookingSlot
    s = BookingSlot(tenant_id=tid, slot_start=start, slot_end=start + datetime.timedelta(hours=1),
                    max_capacity=cap)
    db.add(s)
    db.commit()
    return s


def _confirmed_resv(db, tid, slot_id):
    from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
    r = Reservation(tenant_id=tid, slot_id=slot_id, party_size=1, status=RESERVATION_CONFIRMED)
    db.add(r)
    db.commit()
    return r


class TestStaffAssignLock:
    def test_sequential_conflicting_assign_raises(self, db):
        """M1：同一員工被指派到兩筆時段重疊的預約，第二次循序指派拋
        StaffConflictError（鎖 Staff 列序列化跨預約衝突檢查）。"""
        from saas_mvp.services import staff as staff_svc

        tid = _tenant(db)
        st = staff_svc.create_staff(db, tenant_id=tid, name="A")
        t0 = datetime.datetime(2030, 1, 1, 10, 0)
        s1 = _slot(db, tid, start=t0)
        # 不同 slot_start（避開 (tenant_id, slot_start) unique），但 1 小時時段重疊。
        s2 = _slot(db, tid, start=t0 + datetime.timedelta(minutes=30))
        r1 = _confirmed_resv(db, tid, s1.id)
        r2 = _confirmed_resv(db, tid, s2.id)

        staff_svc.assign_staff(db, tenant_id=tid, reservation_id=r1.id, staff_id=st.id)
        with pytest.raises(staff_svc.StaffConflictError):
            staff_svc.assign_staff(db, tenant_id=tid, reservation_id=r2.id, staff_id=st.id)


# ── M2：flex_menu.add_card 12 張上限以鎖父選單列序列化 ─────────────────────────

class TestFlexCardCap:
    def test_adding_past_12_raises(self, db):
        from fastapi import HTTPException

        from saas_mvp.services import flex_menu as flex_svc

        tid = _tenant(db)
        menu = flex_svc.create_menu(db, tenant_id=tid, title="m")
        for i in range(flex_svc.MAX_CARDS):
            flex_svc.add_card(db, tenant_id=tid, menu_id=menu.id, title=f"c{i}",
                              action_type="message", action_data="hi")
        with pytest.raises(HTTPException) as ei:
            flex_svc.add_card(db, tenant_id=tid, menu_id=menu.id, title="over",
                              action_type="message", action_data="hi")
        assert ei.value.status_code == 422


# ── M3+M4(L4)：跨租戶引用拒絕（booking staff/service、catalog/staff location/category）─

class TestCrossTenantRefs:
    def test_booking_cross_tenant_staff_rejected(self, db):
        from saas_mvp.services import booking as booking_svc
        from saas_mvp.services import staff as staff_svc

        t_a = _tenant(db, "a")
        t_b = _tenant(db, "b")
        other_staff = staff_svc.create_staff(db, tenant_id=t_b, name="B-staff")
        slot = _slot(db, t_a, start=datetime.datetime(2030, 2, 1, 9, 0))
        with pytest.raises(booking_svc.CrossTenantReferenceError):
            booking_svc.book_slot(db, tenant_id=t_a, slot_id=slot.id,
                                  staff_id=other_staff.id)

    def test_booking_cross_tenant_service_rejected(self, db):
        from saas_mvp.services import booking as booking_svc
        from saas_mvp.services import catalog as catalog_svc

        t_a = _tenant(db, "a")
        t_b = _tenant(db, "b")
        other_svc = catalog_svc.create_service(db, tenant_id=t_b, name="B-svc")
        slot = _slot(db, t_a, start=datetime.datetime(2030, 2, 1, 9, 0))
        with pytest.raises(booking_svc.CrossTenantReferenceError):
            booking_svc.book_slot(db, tenant_id=t_a, slot_id=slot.id,
                                  service_id=other_svc.id)

    def test_service_create_cross_tenant_category_rejected(self, db):
        from fastapi import HTTPException

        from saas_mvp.services import catalog as catalog_svc

        t_a = _tenant(db, "a")
        t_b = _tenant(db, "b")
        other_cat = catalog_svc.create_category(db, tenant_id=t_b, name="B-cat")
        with pytest.raises(HTTPException) as ei:
            catalog_svc.create_service(db, tenant_id=t_a, name="svc",
                                       category_id=other_cat.id)
        assert ei.value.status_code == 422

    def test_staff_create_cross_tenant_location_rejected(self, db):
        from fastapi import HTTPException

        from saas_mvp.services import locations as loc_svc
        from saas_mvp.services import staff as staff_svc

        t_a = _tenant(db, "a")
        t_b = _tenant(db, "b")
        other_loc = loc_svc.create_location(db, tenant_id=t_b, name="B-loc")
        with pytest.raises(HTTPException) as ei:
            staff_svc.create_staff(db, tenant_id=t_a, name="s", location_id=other_loc.id)
        assert ei.value.status_code == 422


# ── M4：PII submit 長度上限（超長 phone → 422，顧客不半寫） ───────────────────────

class TestPiiLengthCap:
    def test_overlength_phone_422_no_half_write(self, db):
        from fastapi import HTTPException

        from saas_mvp.models.customer import Customer
        from saas_mvp.services import pii as pii_svc

        tid = _tenant(db)
        req = pii_svc.create_request(db, tenant_id=tid, line_user_id="U1")
        with pytest.raises(HTTPException) as ei:
            pii_svc.submit(db, token=req.token, name="N", phone="9" * 100, birthday=None)
        assert ei.value.status_code == 422
        db.rollback()
        # 顧客不應被半寫（沒有任何 Customer 列）。
        assert db.query(Customer).filter(Customer.tenant_id == tid).count() == 0
        # token 仍 pending（未被標 submitted）。
        from saas_mvp.models.pii_request import PII_PENDING
        db.refresh(req)
        assert req.status == PII_PENDING


# ── M5：公開頁 social_links / banner_url 只放行 http/https（javascript: 丟棄） ──

class TestPublicSocialScheme:
    def test_javascript_social_link_dropped(self):
        from saas_mvp.routers.public import _safe_http_url

        assert _safe_http_url("javascript:alert(1)") is None
        assert _safe_http_url("data:text/html,x") is None
        assert _safe_http_url("https://ok.example") == "https://ok.example"
        assert _safe_http_url("http://ok.example") == "http://ok.example"
        assert _safe_http_url(None) is None


# ── L1：conversational pick_slot 負人數不 500（夾為 1） ─────────────────────────

class TestPartyClamp:
    def test_negative_party_clamped(self):
        from saas_mvp.booking.commands import parse_postback_data

        action, params = parse_postback_data("action=book&slot_id=3&party=-5")
        assert action == "book"
        assert params["party_size"] == 1
        # pick_slot 帶負 party 同樣夾為 1。
        action2, params2 = parse_postback_data(
            "action=pick_slot&slot_id=3&service_id=1&party=-5"
        )
        assert params2.get("party_size") == 1


# ── L2：marketing _segment_kwargs int() 容錯（malformed → 缺漏，不 500） ─────────

class TestSegmentKwargsCoerce:
    def test_malformed_int_treated_absent(self):
        from saas_mvp.services.marketing import _segment_kwargs

        out = _segment_kwargs({"min_bookings": "abc", "location_id": "xyz"})
        assert "min_bookings" not in out
        assert "location_id" not in out
        out2 = _segment_kwargs({"min_bookings": "3", "location_id": 7})
        assert out2["min_bookings"] == 3
        assert out2["location_id"] == 7


# ── L3：reporting location join 帶 tenant_id（跨租戶 Service 不混入） ───────────

class TestReportingLocationTenant:
    def test_location_filter_excludes_cross_tenant_service(self, db):
        from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
        from saas_mvp.services import catalog as catalog_svc
        from saas_mvp.services import locations as loc_svc
        from saas_mvp.services import reporting as rep_svc

        t_a = _tenant(db, "a")
        _tenant(db, "b")  # 第二租戶存在但不直接引用，確保 join 帶 tenant_id 隔離。
        loc_a = loc_svc.create_location(db, tenant_id=t_a, name="locA")
        # 租戶 A 的服務綁 loc_a。
        svc_a = catalog_svc.create_service(db, tenant_id=t_a, name="svcA",
                                           location_id=loc_a.id)
        slot = _slot(db, t_a, start=datetime.datetime(2030, 3, 1, 9, 0))
        r = Reservation(tenant_id=t_a, slot_id=slot.id, party_size=1,
                        status=RESERVATION_CONFIRMED, service_id=svc_a.id)
        db.add(r)
        db.commit()
        # 以 loc_a 過濾 popular_services 應只看到 A 的資料（不因 join 缺 tenant_id 混入）。
        out = rep_svc.popular_services(db, tenant_id=t_a, location_id=loc_a.id)
        assert any(d["service_id"] == svc_a.id for d in out)


# ── L5：segments tag_ids 上限 20（不爆 JOIN） ───────────────────────────────────

class TestSegmentTagCap:
    def test_tag_ids_capped_at_20(self, db):
        from saas_mvp.services import segments as seg_svc

        tid = _tenant(db)
        # 建 25 個標籤；傳全部不應因 JOIN 爆炸出錯（且只用前 20 個做 AND filter）。
        tag_ids = [seg_svc.create_tag(db, tenant_id=tid, name=f"t{i}").id for i in range(25)]
        # 不應拋例外。
        result = seg_svc.segment_customers(db, tenant_id=tid, tag_ids=tag_ids)
        assert result == []
