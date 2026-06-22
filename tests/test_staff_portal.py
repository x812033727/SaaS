"""員工自助入口（public, token-based）測試。"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
from saas_mvp.models import point_transaction as _pt  # noqa: F401,E402
from saas_mvp.models import location as _loc  # noqa: F401,E402
from saas_mvp.models import staff as _staff, staff_shift as _ss, staff_leave as _sl  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf, feature_change_history as _fch  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_get_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _register(client) -> str:
    r = client.post("/auth/register", json={
        "email": f"u_{uuid.uuid4().hex[:8]}@example.com",
        "password": "Test1234!",
        "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _make_staff_with_token(client, token, name="員工") -> tuple[int, str]:
    sid = client.post("/booking/staff/", headers=_auth(token), json={"name": name}).json()["id"]
    tok = client.post(f"/booking/staff/{sid}/rotate-token", headers=_auth(token)).json()["access_token"]
    return sid, tok


class TestStaffPortal:
    def test_valid_token_serves_data(self, client):
        token = _register(client)
        sid, stoken = _make_staff_with_token(client, token, name="王小明")
        # 給該員工一張預約
        slot = client.post("/booking/slots/", headers=_auth(token), json={
            "slot_start": "2031-01-01T10:00:00+00:00",
            "slot_end": "2031-01-01T11:00:00+00:00",
            "max_capacity": 5,
        }).json()
        resv = client.post("/booking/reservations/", headers=_auth(token), json={
            "slot_id": slot["id"], "party_size": 2,
        }).json()
        client.post(f"/booking/staff/{sid}/assign", headers=_auth(token),
                    json={"reservation_id": resv["id"]})

        page = client.get(f"/s/{stoken}")
        assert page.status_code == 200
        assert "王小明" in page.text
        # JSON bookings endpoint
        b = client.get(f"/s/{stoken}/bookings")
        assert b.status_code == 200
        assert any(item["id"] == resv["id"] for item in b.json())

    def test_unknown_token_404(self, client):
        assert client.get("/s/not-a-real-token").status_code == 404
        assert client.get("/s/not-a-real-token/bookings").status_code == 404

    def test_token_never_exposes_other_tenant(self, client):
        token_a = _register(client)
        _sid_a, stoken_a = _make_staff_with_token(client, token_a, name="A租戶員工")
        token_b = _register(client)
        # B 租戶建立預約並指派給 B 員工
        sid_b, _stoken_b = _make_staff_with_token(client, token_b, name="B租戶員工")
        slot_b = client.post("/booking/slots/", headers=_auth(token_b), json={
            "slot_start": "2031-02-01T10:00:00+00:00",
            "slot_end": "2031-02-01T11:00:00+00:00",
            "max_capacity": 5,
        }).json()
        resv_b = client.post("/booking/reservations/", headers=_auth(token_b), json={
            "slot_id": slot_b["id"], "party_size": 1,
        }).json()
        client.post(f"/booking/staff/{sid_b}/assign", headers=_auth(token_b),
                    json={"reservation_id": resv_b["id"]})
        # A 的 token 只看到 A 員工自己的（空）資料，看不到 B 的預約
        b = client.get(f"/s/{stoken_a}/bookings")
        assert b.status_code == 200
        assert all(item["id"] != resv_b["id"] for item in b.json())
        page = client.get(f"/s/{stoken_a}")
        assert "B租戶員工" not in page.text


def test_create_staff_issues_token_and_portal_works_without_rotate(client):
    """建立員工即發 access_token；員工專屬連結 /s/{token} 開箱即用（免先 rotate）。"""
    token = _register(client)
    created = client.post("/booking/staff/", headers=_auth(token),
                          json={"name": "新進設計師"})
    assert created.status_code == 201, created.text
    portal_token = created.json().get("access_token")
    assert portal_token, "create_staff 應自動產生 access_token"
    # 直接用建立回傳的 token 進入專屬入口，不需呼叫 rotate-token
    page = client.get(f"/s/{portal_token}")
    assert page.status_code == 200
    # rotate 後舊 token 失效、新 token 可用
    rotated = client.post(
        f"/booking/staff/{created.json()['id']}/rotate-token", headers=_auth(token)
    ).json()["access_token"]
    assert rotated != portal_token
    assert client.get(f"/s/{rotated}").status_code == 200
    assert client.get(f"/s/{portal_token}").status_code == 404
