"""List 端點分頁（limit/offset + X-Total-Count）。

驗收標準
--------
- GET /booking/reservations/、/booking/customers/、/booking/customers/segment、
  /booking/customers/{id}/points 支援 limit/offset
- 回應帶 X-Total-Count header（未分頁前的總筆數,供呼叫端偵測截斷）
- limit 越界（0 或 >500）回 422
- 不帶參數時預設 limit=100（既有小量資料呼叫端行為不變）
"""

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

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402

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


def _register(client) -> tuple[str, int]:
    r = client.post("/auth/register", json={
        "email": f"u_{_uid()}@example.com",
        "password": "Test1234!",
        "tenant_name": f"t_{_uid()}",
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    me = client.get("/tenants/me", headers=_auth(token))
    return token, me.json()["id"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_slot(client, token, *, max_capacity=50, start="2030-06-01T18:00:00+00:00"):
    r = client.post(
        "/booking/slots/",
        headers=_auth(token),
        json={"slot_start": start, "max_capacity": max_capacity},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _seed_customers(tenant_id: int, n: int) -> None:
    db = _Session()
    try:
        for i in range(n):
            db.add(Customer(
                tenant_id=tenant_id,
                line_user_id=f"U-page-{tenant_id}-{i}",
                display_name=f"客人{i}",
            ))
        db.commit()
    finally:
        db.close()


class TestReservationsPagination:
    def test_limit_offset_and_total_count(self, client):
        token, _tid = _register(client)
        slot = _make_slot(client, token)
        for _ in range(5):
            r = client.post(
                "/booking/reservations/",
                headers=_auth(token),
                json={"slot_id": slot["id"], "party_size": 1},
            )
            assert r.status_code == 201, r.text

        r = client.get(
            "/booking/reservations/?limit=2&offset=0", headers=_auth(token)
        )
        assert r.status_code == 200
        assert len(r.json()) == 2
        assert r.headers["X-Total-Count"] == "5"

        r2 = client.get(
            "/booking/reservations/?limit=2&offset=4", headers=_auth(token)
        )
        assert len(r2.json()) == 1  # 最後一頁
        # offset 分頁不重複：id 遞增排序
        ids_p1 = [x["id"] for x in r.json()]
        ids_p2 = [x["id"] for x in r2.json()]
        assert not set(ids_p1) & set(ids_p2)

    def test_default_returns_all_small_dataset(self, client):
        token, _tid = _register(client)
        slot = _make_slot(client, token)
        for _ in range(3):
            client.post(
                "/booking/reservations/",
                headers=_auth(token),
                json={"slot_id": slot["id"], "party_size": 1},
            )
        r = client.get("/booking/reservations/", headers=_auth(token))
        assert len(r.json()) == 3
        assert r.headers["X-Total-Count"] == "3"

    def test_limit_bounds_422(self, client):
        token, _tid = _register(client)
        assert client.get(
            "/booking/reservations/?limit=0", headers=_auth(token)
        ).status_code == 422
        assert client.get(
            "/booking/reservations/?limit=501", headers=_auth(token)
        ).status_code == 422
        assert client.get(
            "/booking/reservations/?offset=-1", headers=_auth(token)
        ).status_code == 422


class TestCustomersPagination:
    def test_limit_offset_and_total_count(self, client):
        token, tid = _register(client)
        _seed_customers(tid, 7)

        r = client.get("/booking/customers/?limit=3", headers=_auth(token))
        assert r.status_code == 200
        assert len(r.json()) == 3
        assert r.headers["X-Total-Count"] == "7"

        r2 = client.get(
            "/booking/customers/?limit=3&offset=6", headers=_auth(token)
        )
        assert len(r2.json()) == 1

    def test_segment_pagination(self, client):
        token, tid = _register(client)
        _seed_customers(tid, 4)
        r = client.get(
            "/booking/customers/segment?limit=2", headers=_auth(token)
        )
        assert r.status_code == 200
        assert len(r.json()) == 2
        assert r.headers["X-Total-Count"] == "4"

    def test_points_ledger_pagination(self, client):
        token, tid = _register(client)
        _seed_customers(tid, 1)
        cid = client.get(
            "/booking/customers/", headers=_auth(token)
        ).json()[0]["id"]
        for i in range(4):
            r = client.post(
                f"/booking/customers/{cid}/points",
                headers=_auth(token),
                json={"delta": 10, "reason": f"test-{i}"},
            )
            assert r.status_code == 200, r.text
        r = client.get(
            f"/booking/customers/{cid}/points?limit=3", headers=_auth(token)
        )
        assert len(r.json()) == 3
        assert r.headers["X-Total-Count"] == "4"

    def test_tenant_isolation_total_count(self, client):
        token_a, tid_a = _register(client)
        token_b, _tid_b = _register(client)
        _seed_customers(tid_a, 2)
        r = client.get("/booking/customers/", headers=_auth(token_b))
        assert r.headers["X-Total-Count"] == "0"
        assert r.json() == []
