"""店家類型（store_type）測試。

涵蓋：
  - Tenant.store_type 欄位存在、預設 NULL
  - PUT /tenants/me 設定 store_type；GET /tenants/me 反映
  - normalize：strip + lowercase；空字串 → null
  - admin PATCH /admin/tenants/{id} 設定 / 清空 store_type
  - 未知值仍接受（軟驗證）
  - 過長（>32）→ 422
  - 不送 store_type 的 PATCH 不動既有值

全部離線，in-memory SQLite。
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

from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db

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


def _register(client: TestClient) -> tuple[str, int]:
    """(token, tenant_id)"""
    email = f"user_{_uid()}@example.com"
    tn = f"tenant_{_uid()}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn,
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    me = client.get("/tenants/me", headers=_auth(token))
    return token, me.json()["id"]


def _make_admin(token: str) -> None:
    from saas_mvp.auth.security import decode_access_token
    from saas_mvp.models.user import User
    payload = decode_access_token(token)
    db = _Session()
    try:
        user = db.get(User, int(payload["sub"]))
        user.is_admin = True
        db.commit()
    finally:
        db.close()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── model 層 ────────────────────────────────────────────────────────────────

def test_model_has_store_type_column():
    from saas_mvp.models.tenant import Tenant
    assert "store_type" in Tenant.__table__.columns


def test_normalize_store_type():
    from saas_mvp.models.tenant import normalize_store_type
    assert normalize_store_type(None) is None
    assert normalize_store_type("") is None
    assert normalize_store_type("   ") is None
    assert normalize_store_type("  Restaurant ") == "restaurant"
    assert normalize_store_type("RETAIL") == "retail"


# ── 租戶自助 ────────────────────────────────────────────────────────────────

def test_new_tenant_store_type_is_null(client):
    token, _ = _register(client)
    r = client.get("/tenants/me", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["store_type"] is None


def test_put_me_sets_store_type(client):
    token, _ = _register(client)
    r = client.put("/tenants/me", headers=_auth(token), json={"store_type": "Restaurant"})
    assert r.status_code == 200, r.text
    assert r.json()["store_type"] == "restaurant"  # normalized
    # GET 反映
    me = client.get("/tenants/me", headers=_auth(token))
    assert me.json()["store_type"] == "restaurant"


def test_put_me_unknown_value_accepted(client):
    token, _ = _register(client)
    r = client.put("/tenants/me", headers=_auth(token), json={"store_type": "cafe-and-bar"})
    assert r.status_code == 200
    assert r.json()["store_type"] == "cafe-and-bar"


def test_put_me_empty_string_becomes_null(client):
    token, _ = _register(client)
    client.put("/tenants/me", headers=_auth(token), json={"store_type": "retail"})
    r = client.put("/tenants/me", headers=_auth(token), json={"store_type": "  "})
    assert r.status_code == 200
    assert r.json()["store_type"] is None


def test_put_me_omitted_field_preserves_store_type(client):
    """空 body / 省略 store_type → 不動既有值（與 admin PATCH 一致）。"""
    token, _ = _register(client)
    client.put("/tenants/me", headers=_auth(token), json={"store_type": "retail"})
    # 省略 store_type 的 PUT 不應清空
    r = client.put("/tenants/me", headers=_auth(token), json={})
    assert r.status_code == 200, r.text
    assert r.json()["store_type"] == "retail"


def test_put_me_over_length_422(client):
    token, _ = _register(client)
    r = client.put("/tenants/me", headers=_auth(token), json={"store_type": "x" * 33})
    assert r.status_code == 422


# ── admin PATCH ───────────────────────────────────────────────────────────────

def test_admin_patch_sets_and_clears_store_type(client):
    admin_token, _ = _register(client)
    _make_admin(admin_token)
    _, target_tid = _register(client)

    # 設定
    r = client.patch(
        f"/admin/tenants/{target_tid}",
        headers=_auth(admin_token),
        json={"store_type": "Service"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["store_type"] == "service"

    # 不送 store_type → 不動（只改 plan）
    r2 = client.patch(
        f"/admin/tenants/{target_tid}",
        headers=_auth(admin_token),
        json={"plan": "pro"},
    )
    assert r2.status_code == 200
    assert r2.json()["store_type"] == "service"

    # 送 null → 清空
    r3 = client.patch(
        f"/admin/tenants/{target_tid}",
        headers=_auth(admin_token),
        json={"store_type": None},
    )
    assert r3.status_code == 200
    assert r3.json()["store_type"] is None
