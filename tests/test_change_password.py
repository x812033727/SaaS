"""變更密碼：API（/auth/change-password）+ 後台 UI（/ui/account）。"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault(
    "SAAS_LINE_CHANNEL_ENCRYPT_KEY",
    "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=",
)

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db, import_all_models

import_all_models()

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
Base.metadata.create_all(bind=_engine)
_app = create_app()


def _override_get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


_app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture()
def client():
    with TestClient(_app, raise_server_exceptions=True) as c:
        yield c


def _register(client: TestClient, password="OldPass123"):
    uid = uuid.uuid4().hex[:8]
    email = f"u_{uid}@example.com"
    r = client.post("/auth/register", json={
        "email": email, "password": password, "tenant_name": f"t_{uid}",
    })
    assert r.status_code == 201, r.text
    return email, password, r.json()["access_token"]


# ── API ──────────────────────────────────────────────────────────────────────

def test_api_change_password_success_and_login(client):
    email, old, token = _register(client)
    r = client.post(
        "/auth/change-password",
        json={"current_password": old, "new_password": "BrandNew456"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204, r.text
    # 新密碼可登入
    ok = client.post("/auth/token", data={"username": email, "password": "BrandNew456"})
    assert ok.status_code == 200
    # 舊密碼失效
    bad = client.post("/auth/token", data={"username": email, "password": old})
    assert bad.status_code == 401


def test_api_wrong_current_password_rejected(client):
    email, old, token = _register(client)
    r = client.post(
        "/auth/change-password",
        json={"current_password": "WrongCurrent9", "new_password": "BrandNew456"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    # 密碼未變：舊密碼仍可登入
    assert client.post("/auth/token", data={"username": email, "password": old}).status_code == 200


def test_api_new_password_too_short_422(client):
    _email, old, token = _register(client)
    r = client.post(
        "/auth/change-password",
        json={"current_password": old, "new_password": "short"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422  # schema min_length=8


def test_api_new_must_differ_from_current(client):
    _email, old, token = _register(client)
    r = client.post(
        "/auth/change-password",
        json={"current_password": old, "new_password": old},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


def test_api_change_password_requires_auth(client):
    r = client.post(
        "/auth/change-password",
        json={"current_password": "x", "new_password": "BrandNew456"},
    )
    assert r.status_code == 401


# ── UI（cookie 後台）─────────────────────────────────────────────────────────

def _ui_login(client: TestClient, email: str, password: str):
    r = client.post("/ui/login", data={"email": email, "password": password})
    assert r.status_code in (200, 303)


def test_ui_account_page_renders(client):
    email, old, _ = _register(client)
    _ui_login(client, email, old)
    r = client.get("/ui/account")
    assert r.status_code == 200
    assert "變更密碼" in r.text


def test_ui_change_password_success(client):
    email, old, _ = _register(client)
    _ui_login(client, email, old)
    r = client.post("/ui/account/password", data={
        "current_password": old,
        "new_password": "BrandNew456",
        "confirm_password": "BrandNew456",
    })
    assert r.status_code == 200
    assert "密碼已更新" in r.text
    # 新密碼可登入（用新 client 確保乾淨 cookie jar）
    with TestClient(_app, raise_server_exceptions=True) as c2:
        assert c2.post("/auth/token", data={"username": email, "password": "BrandNew456"}).status_code == 200


def test_ui_change_password_wrong_current(client):
    email, old, _ = _register(client)
    _ui_login(client, email, old)
    r = client.post("/ui/account/password", data={
        "current_password": "Nope12345",
        "new_password": "BrandNew456",
        "confirm_password": "BrandNew456",
    })
    assert r.status_code == 200
    assert "目前密碼不正確" in r.text


def test_ui_change_password_mismatch(client):
    email, old, _ = _register(client)
    _ui_login(client, email, old)
    r = client.post("/ui/account/password", data={
        "current_password": old,
        "new_password": "BrandNew456",
        "confirm_password": "Different789",
    })
    assert r.status_code == 200
    assert "不一致" in r.text


def test_ui_account_requires_login(client):
    # 未登入 → 重導登入頁（UI 行為）
    r = client.get("/ui/account", follow_redirects=False)
    assert r.status_code == 303
    assert "/ui/login" in r.headers.get("location", "")
