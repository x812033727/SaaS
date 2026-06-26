"""員工排班（staff scheduling）測試 — CRUD + 班表/請假 + 衝突矩陣 + 指派。"""

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

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
from saas_mvp.models import point_transaction as _pt  # noqa: F401,E402
from saas_mvp.models import location as _loc  # noqa: F401,E402
from saas_mvp.models import staff as _staff, staff_shift as _ss, staff_leave as _sl  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf, feature_change_history as _fch  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.reservation import Reservation, RESERVATION_CONFIRMED  # noqa: E402
from saas_mvp.models.staff import Staff  # noqa: E402
from saas_mvp.services import staff as staff_svc  # noqa: E402

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


def _tenant_id_of(token) -> int:
    db = _Session()
    try:
        from saas_mvp.models.user import User
        from saas_mvp.auth.security import decode_access_token
        payload = decode_access_token(token)
        user = db.query(User).filter(User.id == int(payload["sub"])).first()
        return user.tenant_id
    finally:
        db.close()


class TestStaffCrud:
    def test_crud_and_rotate_token(self, client):
        token = _register(client)
        r = client.post("/booking/staff/", headers=_auth(token), json={
            "name": "小美", "role": "設計師", "booking_mode": "one_to_one",
        })
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        # 建立即發 capability token（員工專屬連結開箱即用）
        created_token = r.json()["access_token"]
        assert created_token
        assert r.json()["booking_mode"] == "one_to_one"
        # rotate token：產生新 token 並作廢舊的
        rot = client.post(f"/booking/staff/{sid}/rotate-token", headers=_auth(token))
        tok1 = rot.json()["access_token"]
        assert tok1
        assert tok1 != created_token
        rot2 = client.post(f"/booking/staff/{sid}/rotate-token", headers=_auth(token))
        assert rot2.json()["access_token"] != tok1
        # update
        u = client.put(f"/booking/staff/{sid}", headers=_auth(token), json={"name": "小美美"})
        assert u.json()["name"] == "小美美"
        # invalid booking_mode → 422
        bad = client.post("/booking/staff/", headers=_auth(token), json={
            "name": "x", "booking_mode": "weird",
        })
        assert bad.status_code == 422

    def test_shift_and_leave_crud(self, client):
        token = _register(client)
        sid = client.post("/booking/staff/", headers=_auth(token),
                          json={"name": "阿宏"}).json()["id"]
        # shift
        sh = client.post(f"/booking/staff/{sid}/shifts", headers=_auth(token), json={
            "weekday": 0, "start_time": "09:00", "end_time": "18:00", "rotation": "day",
        })
        assert sh.status_code == 201, sh.text
        shid = sh.json()["id"]
        assert len(client.get(f"/booking/staff/{sid}/shifts", headers=_auth(token)).json()) == 1
        # duplicate (same staff/weekday/start) → 409
        dup = client.post(f"/booking/staff/{sid}/shifts", headers=_auth(token), json={
            "weekday": 0, "start_time": "09:00", "end_time": "12:00",
        })
        assert dup.status_code == 409
        assert client.delete(f"/booking/staff/{sid}/shifts/{shid}", headers=_auth(token)).status_code == 204
        # leave
        lv = client.post(f"/booking/staff/{sid}/leaves", headers=_auth(token), json={
            "start_at": "2030-07-01T00:00:00+00:00",
            "end_at": "2030-07-02T00:00:00+00:00",
            "reason": "休假",
        })
        assert lv.status_code == 201, lv.text
        lvid = lv.json()["id"]
        assert len(client.get(f"/booking/staff/{sid}/leaves", headers=_auth(token)).json()) == 1
        assert client.delete(f"/booking/staff/{sid}/leaves/{lvid}", headers=_auth(token)).status_code == 204

    def test_tenant_isolation(self, client):
        token_a = _register(client)
        sid = client.post("/booking/staff/", headers=_auth(token_a),
                          json={"name": "A員"}).json()["id"]
        token_b = _register(client)
        assert client.get("/booking/staff/", headers=_auth(token_b)).json() == []
        assert client.get(f"/booking/staff/{sid}", headers=_auth(token_b)).status_code == 404


class TestCheckConflict:
    """直接以 service + 測試 session 驗證衝突矩陣。"""

    def _new_staff(self, db, tenant_id) -> Staff:
        s = Staff(tenant_id=tenant_id, name="排班員", booking_mode="one_to_one")
        db.add(s)
        db.commit()
        db.refresh(s)
        return s

    def test_leave_overlap_blocks(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        db = _Session()
        try:
            staff = self._new_staff(db, tid)
            staff_svc.create_leave(
                db, tenant_id=tid, staff_id=staff.id,
                start_at=datetime.datetime(2030, 8, 1, 9, 0),
                end_at=datetime.datetime(2030, 8, 1, 18, 0),
            )
            ok, reason = staff_svc.check_conflict(
                db, tenant_id=tid, staff_id=staff.id,
                start_at=datetime.datetime(2030, 8, 1, 10, 0),
                end_at=datetime.datetime(2030, 8, 1, 11, 0),
            )
            assert ok is False and reason == "staff on leave"
            # 不重疊的時間 → ok
            ok2, _ = staff_svc.check_conflict(
                db, tenant_id=tid, staff_id=staff.id,
                start_at=datetime.datetime(2030, 8, 2, 10, 0),
                end_at=datetime.datetime(2030, 8, 2, 11, 0),
            )
            assert ok2 is True
        finally:
            db.close()

    def test_outside_shift_blocks(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        db = _Session()
        try:
            staff = self._new_staff(db, tid)
            # 2030-08-05 是週一（weekday 0）
            assert datetime.date(2030, 8, 5).weekday() == 0
            staff_svc.create_shift(
                db, tenant_id=tid, staff_id=staff.id,
                weekday=0, start_time="09:00", end_time="12:00",
            )
            # 班內 → ok
            ok_in, _ = staff_svc.check_conflict(
                db, tenant_id=tid, staff_id=staff.id,
                start_at=datetime.datetime(2030, 8, 5, 10, 0),
                end_at=datetime.datetime(2030, 8, 5, 11, 0),
            )
            assert ok_in is True
            # 班外（下午）→ blocked
            ok_out, reason = staff_svc.check_conflict(
                db, tenant_id=tid, staff_id=staff.id,
                start_at=datetime.datetime(2030, 8, 5, 14, 0),
                end_at=datetime.datetime(2030, 8, 5, 15, 0),
            )
            assert ok_out is False and reason == "outside staff shift"
        finally:
            db.close()

    def test_double_book_blocks(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        db = _Session()
        try:
            from saas_mvp.models.booking_slot import BookingSlot
            staff = self._new_staff(db, tid)
            slot = BookingSlot(
                tenant_id=tid,
                slot_start=datetime.datetime(2030, 9, 1, 10, 0),
                slot_end=datetime.datetime(2030, 9, 1, 11, 0),
                max_capacity=5,
            )
            db.add(slot)
            db.commit()
            db.refresh(slot)
            resv = Reservation(
                tenant_id=tid, slot_id=slot.id, party_size=1,
                status=RESERVATION_CONFIRMED, staff_id=staff.id,
            )
            db.add(resv)
            db.commit()
            ok, reason = staff_svc.check_conflict(
                db, tenant_id=tid, staff_id=staff.id,
                start_at=datetime.datetime(2030, 9, 1, 10, 30),
                end_at=datetime.datetime(2030, 9, 1, 10, 45),
            )
            assert ok is False and reason == "staff already booked"
        finally:
            db.close()


class TestAssignStaff:
    def test_assign_happy_and_conflict(self, client):
        token = _register(client)
        # 建時段 + 建單（capacity 模式，不掛 staff）
        slot = client.post("/booking/slots/", headers=_auth(token), json={
            "slot_start": "2030-10-01T10:00:00+00:00",
            "slot_end": "2030-10-01T11:00:00+00:00",
            "max_capacity": 5,
        }).json()
        resv = client.post("/booking/reservations/", headers=_auth(token), json={
            "slot_id": slot["id"], "party_size": 1,
        }).json()
        sid = client.post("/booking/staff/", headers=_auth(token),
                          json={"name": "指派員"}).json()["id"]
        # happy：指派成功
        r = client.post(f"/booking/staff/{sid}/assign", headers=_auth(token),
                        json={"reservation_id": resv["id"]})
        assert r.status_code == 200, r.text
        assert r.json()["staff_id"] == sid
        # 再建第二張同員工同時段預約並指派 → 衝突 409
        resv2 = client.post("/booking/reservations/", headers=_auth(token), json={
            "slot_id": slot["id"], "party_size": 1,
        }).json()
        c = client.post(f"/booking/staff/{sid}/assign", headers=_auth(token),
                        json={"reservation_id": resv2["id"]})
        assert c.status_code == 409, c.text

    def test_assign_unknown_404(self, client):
        token = _register(client)
        sid = client.post("/booking/staff/", headers=_auth(token),
                          json={"name": "x"}).json()["id"]
        r = client.post(f"/booking/staff/{sid}/assign", headers=_auth(token),
                        json={"reservation_id": 999999})
        assert r.status_code == 404


class TestStaffLimit:
    """免費版 3 員工上限 + UNLIMITED_STAFF 解除（對標 vibeaico「無限員工」）。"""

    def _set_unlimited(self, tenant_id: int, enabled: bool) -> None:
        from saas_mvp.services import features as features_svc
        db = _Session()
        try:
            features_svc.set_enabled(
                db, tenant_id, features_svc.UNLIMITED_STAFF, enabled,
                actor_user_id=None, source="admin",
            )
        finally:
            db.close()

    def test_free_tier_limit_then_unlock(self, client):
        from saas_mvp.config import settings

        token = _register(client)
        tid = _tenant_id_of(token)
        # 明確關閉 UNLIMITED_STAFF（預設 features_default_enabled=True 會放行）
        self._set_unlimited(tid, False)

        # 建到上限（free_staff_limit，預設 3）皆成功
        for i in range(settings.free_staff_limit):
            r = client.post("/booking/staff/", headers=_auth(token),
                            json={"name": f"staff{i}"})
            assert r.status_code == 201, r.text
        # 第 N+1 位 → 402 Payment Required
        over = client.post("/booking/staff/", headers=_auth(token),
                           json={"name": "overflow"})
        assert over.status_code == 402, over.text
        assert "無限員工" in over.json()["detail"]

        # 停用一位後可再建一位（停用者不佔額度）
        sid_first = client.get("/booking/staff/", headers=_auth(token)).json()[0]["id"]
        client.put(f"/booking/staff/{sid_first}", headers=_auth(token),
                   json={"is_active": False})
        again = client.post("/booking/staff/", headers=_auth(token),
                            json={"name": "after-deactivate"})
        assert again.status_code == 201, again.text

        # 開通 UNLIMITED_STAFF → 不再受限
        self._set_unlimited(tid, True)
        unlocked = client.post("/booking/staff/", headers=_auth(token),
                               json={"name": "unlimited"})
        assert unlocked.status_code == 201, unlocked.text
