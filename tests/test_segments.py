"""顧客標籤 / 分眾測試 — 標籤 CRUD、掛載冪等、各分眾 filter、跨租戶隔離。"""

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
from saas_mvp.models import customer_tag as _ctag, customer_tag_link as _ctl  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.auth.security import decode_access_token  # noqa: E402

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


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register(client) -> str:
    r = client.post("/auth/register", json={
        "email": f"u_{_uid()}@example.com",
        "password": "Test1234!",
        "tenant_name": f"t_{_uid()}",
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _tenant_id_of(token: str) -> int:
    db = _Session()
    try:
        payload = decode_access_token(token)
        return db.query(User).filter(User.id == int(payload["sub"])).first().tenant_id
    finally:
        db.close()


def _seed_customer(
    tenant_id: int,
    *,
    line_user_id: str | None = None,
    booking_count: int = 0,
    tier: str = "regular",
    last_booked_at: datetime.datetime | None = None,
    location_id: int | None = None,
) -> int:
    db = _Session()
    try:
        c = Customer(
            tenant_id=tenant_id,
            line_user_id=line_user_id or f"U{_uid()}",
            booking_count=booking_count,
            tier=tier,
            last_booked_at=last_booked_at,
            location_id=location_id,
        )
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


# ── 標籤 CRUD ────────────────────────────────────────────────────────────────

class TestTagCRUD:
    def test_create_list_delete(self, client):
        token = _register(client)
        r = client.post(
            "/booking/customers/tags",
            headers=_auth(token),
            json={"name": "VIP", "color": "#ff0000"},
        )
        assert r.status_code == 201, r.text
        tag_id = r.json()["id"]
        assert r.json()["name"] == "VIP" and r.json()["color"] == "#ff0000"

        r = client.get("/booking/customers/tags", headers=_auth(token))
        assert r.status_code == 200
        assert any(t["id"] == tag_id for t in r.json())

        r = client.delete(f"/booking/customers/tags/{tag_id}", headers=_auth(token))
        assert r.status_code == 204
        r = client.get("/booking/customers/tags", headers=_auth(token))
        assert all(t["id"] != tag_id for t in r.json())

    def test_duplicate_name_409(self, client):
        token = _register(client)
        client.post(
            "/booking/customers/tags", headers=_auth(token), json={"name": "Dup"}
        )
        r = client.post(
            "/booking/customers/tags", headers=_auth(token), json={"name": "Dup"}
        )
        assert r.status_code == 409

    def test_delete_unknown_404(self, client):
        token = _register(client)
        assert client.delete(
            "/booking/customers/tags/999999", headers=_auth(token)
        ).status_code == 404


# ── 掛 / 卸標籤 ──────────────────────────────────────────────────────────────

class TestAttachDetach:
    def test_attach_idempotent(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        cid = _seed_customer(tid)
        tag_id = client.post(
            "/booking/customers/tags", headers=_auth(token), json={"name": "Repeat"}
        ).json()["id"]

        r1 = client.post(
            f"/booking/customers/{cid}/tags/{tag_id}", headers=_auth(token)
        )
        assert r1.status_code == 201
        r2 = client.post(
            f"/booking/customers/{cid}/tags/{tag_id}", headers=_auth(token)
        )
        assert r2.status_code == 201  # 冪等不報錯

        tags = client.get(
            f"/booking/customers/{cid}/tags", headers=_auth(token)
        ).json()
        assert sum(1 for t in tags if t["id"] == tag_id) == 1  # 只有一筆

    def test_detach_idempotent(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        cid = _seed_customer(tid)
        tag_id = client.post(
            "/booking/customers/tags", headers=_auth(token), json={"name": "Det"}
        ).json()["id"]
        client.post(f"/booking/customers/{cid}/tags/{tag_id}", headers=_auth(token))

        assert client.delete(
            f"/booking/customers/{cid}/tags/{tag_id}", headers=_auth(token)
        ).status_code == 204
        # 再卸一次仍 204（no-op）
        assert client.delete(
            f"/booking/customers/{cid}/tags/{tag_id}", headers=_auth(token)
        ).status_code == 204
        tags = client.get(
            f"/booking/customers/{cid}/tags", headers=_auth(token)
        ).json()
        assert all(t["id"] != tag_id for t in tags)

    def test_attach_unknown_customer_404(self, client):
        token = _register(client)
        tag_id = client.post(
            "/booking/customers/tags", headers=_auth(token), json={"name": "X"}
        ).json()["id"]
        assert client.post(
            f"/booking/customers/999999/tags/{tag_id}", headers=_auth(token)
        ).status_code == 404


# ── 分眾 filter ──────────────────────────────────────────────────────────────

class TestSegment:
    def test_by_tag(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        c1 = _seed_customer(tid)
        _seed_customer(tid)  # 無標籤
        tag_id = client.post(
            "/booking/customers/tags", headers=_auth(token), json={"name": "Seg"}
        ).json()["id"]
        client.post(f"/booking/customers/{c1}/tags/{tag_id}", headers=_auth(token))

        r = client.get(
            "/booking/customers/segment",
            headers=_auth(token),
            params={"tag_ids": str(tag_id)},
        )
        assert r.status_code == 200
        ids = {c["id"] for c in r.json()}
        assert ids == {c1}

    def test_by_tier(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        gold = _seed_customer(tid, tier="gold")
        _seed_customer(tid, tier="regular")
        r = client.get(
            "/booking/customers/segment", headers=_auth(token), params={"tier": "gold"}
        )
        ids = {c["id"] for c in r.json()}
        assert gold in ids and all(
            c["tier"] == "gold" for c in r.json()
        )

    def test_by_min_bookings(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        loyal = _seed_customer(tid, booking_count=5)
        _seed_customer(tid, booking_count=1)
        r = client.get(
            "/booking/customers/segment",
            headers=_auth(token),
            params={"min_bookings": 3},
        )
        ids = {c["id"] for c in r.json()}
        assert loyal in ids and all(c["booking_count"] >= 3 for c in r.json())

    def test_by_last_booked_before(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        old = _seed_customer(
            tid,
            last_booked_at=datetime.datetime(
                2020, 1, 1, tzinfo=datetime.timezone.utc
            ),
        )
        _seed_customer(
            tid,
            last_booked_at=datetime.datetime(
                2030, 1, 1, tzinfo=datetime.timezone.utc
            ),
        )
        r = client.get(
            "/booking/customers/segment",
            headers=_auth(token),
            params={"last_booked_before": "2025-01-01"},
        )
        ids = {c["id"] for c in r.json()}
        assert old in ids

    def test_by_location(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        here = _seed_customer(tid, location_id=7)
        _seed_customer(tid, location_id=8)
        r = client.get(
            "/booking/customers/segment",
            headers=_auth(token),
            params={"location_id": 7},
        )
        ids = {c["id"] for c in r.json()}
        assert ids == {here}

    def test_combined_filters_and_semantics(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        target = _seed_customer(tid, tier="gold", booking_count=10)
        _seed_customer(tid, tier="gold", booking_count=1)
        tag_id = client.post(
            "/booking/customers/tags", headers=_auth(token), json={"name": "Combo"}
        ).json()["id"]
        client.post(
            f"/booking/customers/{target}/tags/{tag_id}", headers=_auth(token)
        )
        r = client.get(
            "/booking/customers/segment",
            headers=_auth(token),
            params={"tag_ids": str(tag_id), "tier": "gold", "min_bookings": 5},
        )
        ids = {c["id"] for c in r.json()}
        assert ids == {target}

    def test_tenant_isolation(self, client):
        token_a = _register(client)
        token_b = _register(client)
        tid_a = _tenant_id_of(token_a)
        ca = _seed_customer(tid_a, tier="gold")
        # B 的分眾查 gold 不應看到 A 的顧客。
        r = client.get(
            "/booking/customers/segment",
            headers=_auth(token_b),
            params={"tier": "gold"},
        )
        ids = {c["id"] for c in r.json()}
        assert ca not in ids
