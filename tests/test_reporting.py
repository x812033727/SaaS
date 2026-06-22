"""進階報表 service 測試（指標正確性）+ 匯出端點 + feature 閘門。"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t  # noqa: F401,E402
from saas_mvp.models import customer as _c  # noqa: F401,E402
from saas_mvp.models import booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r  # noqa: F401,E402
from saas_mvp.models import service as _svc, staff as _staff  # noqa: F401,E402
from saas_mvp.models import product as _p, order as _o, order_item as _oi  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf, feature_change_history as _fch  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.order import Order  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.service import Service  # noqa: E402
from saas_mvp.models.staff import Staff  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import reporting as rep  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _utc(y, m, d, h=12):
    return datetime.datetime(y, m, d, h, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db, name="rep") -> int:
    t = Tenant(name=name, plan="free")
    db.add(t)
    db.commit()
    return t.id


def _slot(db, tid, start) -> int:
    slot = BookingSlot(tenant_id=tid, slot_start=start, max_capacity=10)
    db.add(slot)
    db.commit()
    return slot.id


def _service(db, tid, name, price_cents, location_id=None) -> int:
    s = Service(tenant_id=tid, name=name, price_cents=price_cents, location_id=location_id)
    db.add(s)
    db.commit()
    return s.id


def _staff_row(db, tid, name) -> int:
    s = Staff(tenant_id=tid, name=name)
    db.add(s)
    db.commit()
    return s.id


def _resv(db, tid, slot_id, *, service_id=None, staff_id=None, line_user_id=None,
          status="confirmed"):
    r = Reservation(
        tenant_id=tid, slot_id=slot_id, service_id=service_id, staff_id=staff_id,
        line_user_id=line_user_id, status=status, party_size=1,
    )
    db.add(r)
    db.commit()
    return r


def _paid_order(db, tid, total_cents, paid_at):
    o = Order(tenant_id=tid, status="paid", total_cents=total_cents, currency="TWD",
              paid_at=paid_at)
    db.add(o)
    db.commit()
    return o


class TestPopularServices:
    def test_ranking(self, db):
        tid = _tenant(db)
        sid = _slot(db, tid, _utc(2030, 1, 1))
        svc_a = _service(db, tid, "剪髮", 50000)
        svc_b = _service(db, tid, "染髮", 120000)
        # A x3, B x1
        for _ in range(3):
            _resv(db, tid, sid, service_id=svc_a, line_user_id="U" + uuid.uuid4().hex[:4])
        _resv(db, tid, sid, service_id=svc_b, line_user_id="Ux")
        out = rep.popular_services(db, tenant_id=tid)
        assert [r["service_id"] for r in out] == [svc_a, svc_b]
        assert out[0]["reservation_count"] == 3
        assert out[0]["service_name"] == "剪髮"

    def test_location_scope(self, db):
        tid = _tenant(db)
        sid = _slot(db, tid, _utc(2030, 1, 1))
        svc_loc1 = _service(db, tid, "L1服務", 100, location_id=1)
        svc_loc2 = _service(db, tid, "L2服務", 100, location_id=2)
        _resv(db, tid, sid, service_id=svc_loc1, line_user_id="A")
        _resv(db, tid, sid, service_id=svc_loc2, line_user_id="B")
        out = rep.popular_services(db, tenant_id=tid, location_id=1)
        assert len(out) == 1 and out[0]["service_id"] == svc_loc1

    def test_tenant_isolation(self, db):
        a = _tenant(db, "A")
        b = _tenant(db, "B")
        sa = _slot(db, a, _utc(2030, 1, 1))
        svc = _service(db, a, "A服務", 100)
        _resv(db, a, sa, service_id=svc, line_user_id="U1")
        assert rep.popular_services(db, tenant_id=b) == []


class TestStaffPerformance:
    def test_counts_and_revenue(self, db):
        tid = _tenant(db)
        sid = _slot(db, tid, _utc(2030, 1, 1))
        svc = _service(db, tid, "剪髮", 50000)
        st1 = _staff_row(db, tid, "Amy")
        st2 = _staff_row(db, tid, "Bob")
        _resv(db, tid, sid, service_id=svc, staff_id=st1, line_user_id="U1")
        _resv(db, tid, sid, service_id=svc, staff_id=st1, line_user_id="U2")
        _resv(db, tid, sid, service_id=svc, staff_id=st2, line_user_id="U3")
        out = rep.staff_performance(db, tenant_id=tid)
        assert out[0]["staff_id"] == st1 and out[0]["reservation_count"] == 2
        assert out[0]["revenue_cents"] == 100000
        assert out[1]["staff_id"] == st2 and out[1]["revenue_cents"] == 50000


class TestRevenueTrend:
    def test_daily_buckets(self, db):
        tid = _tenant(db)
        _paid_order(db, tid, 10000, _utc(2030, 3, 1, 9))
        _paid_order(db, tid, 5000, _utc(2030, 3, 1, 18))
        _paid_order(db, tid, 7000, _utc(2030, 3, 2, 10))
        out = rep.revenue_trend(db, tenant_id=tid)
        assert [b["day"] for b in out] == ["2030-03-01", "2030-03-02"]
        assert out[0]["revenue_cents"] == 15000 and out[0]["order_count"] == 2
        assert out[1]["revenue_cents"] == 7000

    def test_excludes_unpaid(self, db):
        tid = _tenant(db)
        o = Order(tenant_id=tid, status="pending", total_cents=9999, currency="TWD")
        db.add(o)
        db.commit()
        assert rep.revenue_trend(db, tenant_id=tid) == []


class TestReturnRate:
    def test_math(self, db):
        tid = _tenant(db)
        sid = _slot(db, tid, _utc(2030, 1, 1))
        # U1 x2 (repeat), U2 x1, U3 x3 (repeat) => 2 repeat / 3 total
        for _ in range(2):
            _resv(db, tid, sid, line_user_id="U1")
        _resv(db, tid, sid, line_user_id="U2")
        for _ in range(3):
            _resv(db, tid, sid, line_user_id="U3")
        out = rep.return_rate(db, tenant_id=tid)
        assert out["total_customers"] == 3
        assert out["repeat_customers"] == 2
        assert out["return_rate"] == round(2 / 3, 4)

    def test_empty(self, db):
        tid = _tenant(db)
        out = rep.return_rate(db, tenant_id=tid)
        assert out == {"total_customers": 0, "repeat_customers": 0, "return_rate": 0.0}


class TestExportFns:
    def test_to_xlsx_bytes(self, db):
        pytest.importorskip("openpyxl")
        content = rep.to_xlsx([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])
        assert isinstance(content, bytes) and len(content) > 0
        # xlsx 是 zip → 以 PK 開頭
        assert content[:2] == b"PK"

    def test_to_pdf_bytes(self, db):
        pytest.importorskip("fpdf")
        content = rep.to_pdf([{"service_name": "剪髮", "reservation_count": 3}])
        assert isinstance(content, bytes) and len(content) > 0
        assert content[:4] == b"%PDF"


# ── 匯出端點 + feature 閘門（HTTP） ──────────────────────────────────────────


@pytest.fixture()
def http():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_get_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _register(http) -> tuple[str, str]:
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    r = http.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return email, r.json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _set_feature(email, enabled: bool):
    s = _Session()
    try:
        u = s.query(User).filter(User.email == email).first()
        features_svc.set_enabled(
            s, u.tenant_id, features_svc.ADVANCED_REPORTING, enabled,
            actor_user_id=u.id, source="admin",
        )
    finally:
        s.close()


class TestEndpoints:
    def test_xlsx_endpoint(self, http):
        pytest.importorskip("openpyxl")
        email, token = _register(http)
        _set_feature(email, True)
        r = http.get("/booking/analytics/report.xlsx", headers=_auth(token))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert "attachment" in r.headers["content-disposition"]
        assert len(r.content) > 0

    def test_pdf_endpoint(self, http):
        pytest.importorskip("fpdf")
        email, token = _register(http)
        _set_feature(email, True)
        r = http.get("/booking/analytics/report.pdf", headers=_auth(token))
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content[:4] == b"%PDF"

    def test_metric_endpoint_gated_403_when_disabled(self, http):
        email, token = _register(http)
        _set_feature(email, False)
        r = http.get("/booking/analytics/report/popular-services", headers=_auth(token))
        assert r.status_code == 403

    def test_metric_endpoint_ok_when_enabled(self, http):
        email, token = _register(http)
        _set_feature(email, True)
        r = http.get("/booking/analytics/report/return-rate", headers=_auth(token))
        assert r.status_code == 200
        assert "return_rate" in r.json()
