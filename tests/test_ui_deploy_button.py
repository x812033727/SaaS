"""後台「重新佈署」按鈕（平台管理員）：

  - 權限：未登入 → 303 /ui/login；非管理員 → 403；管理員 → 可觸發。
  - 觸發：寫出觸發檔（原子 rename），內容含操作者 email。
  - 顯示：account 頁僅在 is_admin 且已設定觸發路徑時才出現按鈕。
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
os.environ.setdefault(
    "SAAS_LINE_CHANNEL_ENCRYPT_KEY",
    "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=",
)

from saas_mvp.app import create_app
from saas_mvp.config import settings
from saas_mvp.db import Base, get_db, import_all_models
from saas_mvp.models.user import User

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


@pytest.fixture()
def trigger_path(tmp_path, monkeypatch):
    p = tmp_path / "requests" / "deploy.request"
    monkeypatch.setattr(settings, "deploy_trigger_path", str(p))
    return p


def _register(client: TestClient):
    uid = uuid.uuid4().hex[:8]
    email = f"u_{uid}@example.com"
    password = "Test1234!"
    r = client.post("/auth/register", json={
        "email": email, "password": password, "tenant_name": f"t_{uid}",
    })
    assert r.status_code == 201, r.text
    return email, password


def _make_admin(email: str):
    db = _Session()
    try:
        user = db.query(User).filter(User.email == email).first()
        user.is_admin = True
        db.commit()
    finally:
        db.close()


def _login(client: TestClient, email: str, password: str):
    assert client.post("/ui/login", data={"email": email, "password": password}).status_code in (200, 303)


def test_admin_deploy_writes_trigger_file(client, trigger_path):
    email, password = _register(client)
    _make_admin(email)
    _login(client, email, password)
    r = client.post("/ui/admin/deploy")
    assert r.status_code == 200, r.text
    assert "已觸發重新佈署" in r.text
    assert trigger_path.exists()
    assert email in trigger_path.read_text(encoding="utf-8")


def test_non_admin_cannot_deploy(client, trigger_path):
    email, password = _register(client)  # 一般商家，不提權
    _login(client, email, password)
    r = client.post("/ui/admin/deploy")
    assert r.status_code == 403
    assert not trigger_path.exists()


def test_unauthenticated_deploy_redirects_to_login(client, trigger_path):
    r = client.post("/ui/admin/deploy", follow_redirects=False)
    assert r.status_code == 303
    assert "/ui/login" in r.headers.get("location", "")
    assert not trigger_path.exists()


def test_deploy_unconfigured_returns_error_no_file(client, monkeypatch):
    monkeypatch.setattr(settings, "deploy_trigger_path", "")
    email, password = _register(client)
    _make_admin(email)
    _login(client, email, password)
    r = client.post("/ui/admin/deploy")
    assert r.status_code == 200
    assert "未設定部署觸發路徑" in r.text


def test_account_page_shows_button_only_for_admin(client, trigger_path):
    # 管理員：看得到按鈕
    a_email, a_pw = _register(client)
    _make_admin(a_email)
    _login(client, a_email, a_pw)
    assert "立即重新佈署" in client.get("/ui/account").text

    # 一般商家（新 client，乾淨 cookie）：看不到按鈕
    with TestClient(_app, raise_server_exceptions=True) as c2:
        u_email, u_pw = _register(c2)
        _login(c2, u_email, u_pw)
        assert "立即重新佈署" not in c2.get("/ui/account").text
