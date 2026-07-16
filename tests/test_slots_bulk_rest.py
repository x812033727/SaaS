"""R4-C2 — POST /booking/slots/bulk REST 端點(包 bulk_generate_slots)。"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c


def _auth(client) -> dict[str, str]:
    r = client.post("/auth/register", json={
        "email": f"bk_{uuid.uuid4().hex[:8]}@x.tw",
        "password": "Test1234!",
        "tenant_name": f"bk_{uuid.uuid4().hex[:8]}",
    })
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


_BODY = {
    "date_start": "2031-04-07",  # 週一
    "date_end": "2031-04-13",    # 週日
    "time_start": "10:00",
    "time_end": "12:00",
    "interval_minutes": 60,
    "max_capacity": 4,
}


def test_bulk_creates_and_skips_existing(client):
    headers = _auth(client)
    r = client.post("/booking/slots/bulk", json=_BODY, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 14, "skipped": 0, "total": 14}  # 7 天 × 2 段
    # 重跑冪等:全部 skipped
    r2 = client.post("/booking/slots/bulk", json=_BODY, headers=headers)
    assert r2.json() == {"created": 0, "skipped": 14, "total": 14}


def test_bulk_weekdays_filter(client):
    headers = _auth(client)
    r = client.post(
        "/booking/slots/bulk",
        json={**_BODY, "weekdays": [0, 2]},  # 只有週一/週三
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["created"] == 4  # 2 天 × 2 段


def test_bulk_validation_422(client):
    headers = _auth(client)
    bad = {**_BODY, "date_end": "2031-04-01"}  # 區間反向
    assert client.post("/booking/slots/bulk", json=bad, headers=headers).status_code == 422
    bad2 = {**_BODY, "interval_minutes": 0}
    assert client.post("/booking/slots/bulk", json=bad2, headers=headers).status_code == 422


def test_bulk_tenant_isolation(client):
    headers_a = _auth(client)
    headers_b = _auth(client)
    client.post("/booking/slots/bulk", json=_BODY, headers=headers_a)
    r = client.get(
        "/booking/slots/?date_from=2031-04-01&date_to=2031-04-30", headers=headers_b
    )
    assert r.json() == []


def test_bulk_requires_auth(client):
    assert client.post("/booking/slots/bulk", json=_BODY).status_code == 401
