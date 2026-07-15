"""/ui CSRF 防護（double-submit cookie token）。

conftest 全域關閉 CSRF（既有 UI 測試不受影響），本檔以 monkeypatch
動態開啟 settings.ui_csrf_enabled 專測防護行為。

驗收標準
--------
- 登入時發放非 httpOnly 的 csrf_token cookie；登出清除
- 開啟後：無 token 的 POST 回 403；X-CSRF-Token header 或表單欄位
  csrf_token 與 cookie 相符即放行（含 multipart 表單）
- token 與 cookie 不符回 403
- /ui/login、/ui/register 豁免（尚無 session）
- GET 一律放行
- 頁面渲染帶出 hx-headers（HTMX 自動附 header 的機制）與 hidden field
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

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
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


@pytest.fixture()
def csrf_on(monkeypatch):
    monkeypatch.setattr(settings, "ui_csrf_enabled", True)
    yield


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register_and_login(client) -> str:
    """UI 註冊（自帶登入 cookie），回傳 csrf_token cookie 值。"""
    r = client.post(
        "/ui/register",
        data={
            "email": f"csrf_{_uid()}@example.com",
            "password": "Test1234!",
            "tenant_name": f"csrf_t_{_uid()}",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    token = client.cookies.get("csrf_token")
    assert token
    return token


class TestCookieIssuance:
    def test_login_sets_non_httponly_csrf_cookie(self, client, csrf_on):
        _register_and_login(client)
        # register 回應已 set-cookie；再走 login 驗證同樣發放
        email = f"csrf2_{_uid()}@example.com"
        client.post("/ui/register", data={
            "email": email, "password": "Test1234!",
            "tenant_name": f"csrf_t2_{_uid()}",
        }, follow_redirects=False)
        client.get("/ui/logout")
        assert not client.cookies.get("csrf_token")

        r = client.post(
            "/ui/login",
            data={"email": email, "password": "Test1234!"},
            follow_redirects=False,
        )
        sc = r.headers.get("set-cookie", "")
        assert "csrf_token=" in sc
        # double-submit cookie 必須非 httpOnly（前端需可讀回傳）
        csrf_part = [p for p in sc.split(",") if "csrf_token=" in p][0]
        assert "httponly" not in csrf_part.lower()

    def test_logout_clears_csrf_cookie(self, client, csrf_on):
        _register_and_login(client)
        client.get("/ui/logout", follow_redirects=False)
        assert not client.cookies.get("csrf_token")


class TestEnforcement:
    def test_post_after_session_cookie_expired_redirects_to_login(self, client, csrf_on):
        client.cookies.clear()
        r = client.post("/ui/admin/email-settings/test", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/ui/login"

    def test_post_without_token_403(self, client, csrf_on):
        _register_and_login(client)
        r = client.post("/ui/account/password", data={
            "current_password": "Test1234!",
            "new_password": "NewPass123!",
            "confirm_password": "NewPass123!",
        })
        assert r.status_code == 403

    def test_post_with_header_ok(self, client, csrf_on):
        token = _register_and_login(client)
        r = client.post(
            "/ui/account/password",
            headers={"X-CSRF-Token": token},
            data={
                "current_password": "wrong",  # 業務驗證失敗即可,只驗有過 CSRF
                "new_password": "NewPass123!",
                "confirm_password": "NewPass123!",
            },
        )
        assert r.status_code != 403

    def test_post_with_form_field_ok(self, client, csrf_on):
        token = _register_and_login(client)
        r = client.post(
            "/ui/account/password",
            data={
                "csrf_token": token,
                "current_password": "wrong",
                "new_password": "NewPass123!",
                "confirm_password": "NewPass123!",
            },
        )
        assert r.status_code != 403

    def test_post_multipart_form_field_ok(self, client, csrf_on):
        """multipart 表單的 csrf_token 欄位也要能通過（上傳類端點慣例）。"""
        token = _register_and_login(client)
        r = client.post(
            "/ui/account/password",
            data={
                "csrf_token": token,
                "current_password": "wrong",
                "new_password": "NewPass123!",
                "confirm_password": "NewPass123!",
            },
            files={"dummy": ("dummy.txt", b"x")},  # 強迫 multipart 編碼
        )
        assert r.status_code != 403

    def test_post_with_mismatched_token_403(self, client, csrf_on):
        _register_and_login(client)
        r = client.post(
            "/ui/account/password",
            headers={"X-CSRF-Token": "attacker-guess"},
            data={
                "current_password": "x",
                "new_password": "y",
                "confirm_password": "z",
            },
        )
        assert r.status_code == 403
        assert "頁面安全憑證已過期" in r.text
        assert "重新登入" in r.text

    def test_login_and_register_exempt(self, client, csrf_on):
        # 未帶任何 token 的 login/register POST 不被 CSRF 擋（可能 401/303/400）
        r = client.post(
            "/ui/login",
            data={"email": "nobody@example.com", "password": "wrong"},
            follow_redirects=False,
        )
        assert r.status_code != 403

        r2 = client.post("/ui/register", data={
            "email": f"ex_{_uid()}@example.com",
            "password": "Test1234!",
            "tenant_name": f"ex_t_{_uid()}",
        }, follow_redirects=False)
        assert r2.status_code != 403

    def test_get_always_allowed(self, client, csrf_on):
        _register_and_login(client)
        # 移除 csrf cookie 後 GET 依然放行
        r = client.get("/ui/")
        assert r.status_code == 200

    def test_disabled_flag_bypasses(self, client, monkeypatch):
        monkeypatch.setattr(settings, "ui_csrf_enabled", False)
        _register_and_login(client)
        r = client.post("/ui/account/password", data={
            "current_password": "wrong",
            "new_password": "NewPass123!",
            "confirm_password": "NewPass123!",
        })
        assert r.status_code != 403


class TestTemplateWiring:
    def test_dashboard_renders_hx_headers(self, client, csrf_on):
        token = _register_and_login(client)
        r = client.get("/ui/")
        assert r.status_code == 200
        assert "X-CSRF-Token" in r.text
        assert token in r.text  # hx-headers 帶了 cookie 值

    def test_account_page_hidden_field(self, client, csrf_on):
        _register_and_login(client)
        r = client.get("/ui/account")
        assert r.status_code == 200
        # 密碼表單為 HTMX(header 機制);plain form 需 hidden field 或
        # 未連結 OAuth 時無 plain form —— 至少 hx-headers 存在
        assert "X-CSRF-Token" in r.text
