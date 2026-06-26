"""伺服器渲染管理 UI（/ui/*）測試。

涵蓋：
  - 登入設 cookie + 303 重導；密碼錯誤重渲染；登出清 cookie
  - 受保護頁未登入 → 303 重導 /ui/login
  - 店家 dashboard（有/無 LINE 設定）；LINE 設定儲存+驗證流程；店家類型設定
  - 平台管理總覽渲染；HTMX 篩選回 partial；非 admin → 403
  - 管理員調整租戶（plan）
  - 停用租戶 → 403 停用頁（非重導登入）
  - 隔離鐵則：API 路徑 (/tenants/me) 不吃 cookie，cookie-only 請求仍 401
  - 安全：HTML 永不輸出明文 secret/token

全部離線，in-memory SQLite，bot/info 用 stub。
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

from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import StubLineBotInfoClient, get_bot_info_client

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


# 每次請求回傳唯一 uid 的 stub，避免跨租戶 line_bot_user_id 唯一鍵衝突。
_app.dependency_overrides[get_db] = _override_get_db
_app.dependency_overrides[get_bot_info_client] = (
    lambda: StubLineBotInfoClient("U" + uuid.uuid4().hex)
)


@pytest.fixture()
def client():
    # 每個測試用新的 TestClient → 全新 cookie jar，彼此不污染。
    with TestClient(_app, raise_server_exceptions=True) as c:
        yield c


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register_api(client: TestClient) -> tuple[str, str, str, int]:
    """用 API 註冊，回傳 (email, password, token, tenant_id)。"""
    email = f"user_{_uid()}@example.com"
    password = "Test1234!"
    tn = f"tenant_{_uid()}"
    r = client.post("/auth/register", json={
        "email": email, "password": password, "tenant_name": tn,
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    tid = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"}).json()["id"]
    return email, password, token, tid


def _make_admin(email: str) -> None:
    from saas_mvp.models.user import User
    db = _Session()
    try:
        user = db.query(User).filter(User.email == email).first()
        user.is_admin = True
        db.commit()
    finally:
        db.close()


def _login_ui(client: TestClient, email: str, password: str) -> None:
    r = client.post("/ui/login", data={"email": email, "password": password})
    assert r.status_code == 200  # follow_redirects → 最終 dashboard 200


# ── 認證流程 ────────────────────────────────────────────────────────────────

def test_login_sets_cookie_and_redirects(client):
    email, password, _, _ = _register_api(client)
    r = client.post("/ui/login", data={"email": email, "password": password},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/"
    sc = r.headers.get("set-cookie", "")
    assert "access_token=" in sc and "httponly" in sc.lower()


def test_login_bad_password_rerenders(client):
    email, _, _, _ = _register_api(client)
    r = client.post("/ui/login", data={"email": email, "password": "wrong-pass"},
                    follow_redirects=False)
    assert r.status_code == 401
    assert "錯誤" in r.text
    assert "set-cookie" not in r.headers


def test_protected_redirects_when_unauthenticated(client):
    r = client.get("/ui/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_logout_clears_cookie(client):
    email, password, _, _ = _register_api(client)
    _login_ui(client, email, password)
    r = client.get("/ui/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"
    assert 'access_token=""' in r.headers.get("set-cookie", "") or \
           "access_token=;" in r.headers.get("set-cookie", "")


# ── 店家自助 ────────────────────────────────────────────────────────────────

def test_dashboard_without_config(client):
    email, password, _, _ = _register_api(client)
    _login_ui(client, email, password)
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "儀表板" in r.text
    assert "尚未設定" in r.text


def test_dashboard_with_config_masks_secret(client):
    email, password, token, _ = _register_api(client)
    # 透過 API 設定憑證（stub 回 uid → valid）
    r = client.put("/tenants/me/line-config",
                   headers={"Authorization": f"Bearer {token}"},
                   json={"channel_secret": "s" * 32, "access_token": "tok-secret-XYZ"})
    assert r.status_code == 200, r.text
    _login_ui(client, email, password)
    page = client.get("/ui/")
    assert page.status_code == 200
    assert "valid" in page.text
    # 安全：明文憑證絕不出現在 HTML
    assert "tok-secret-XYZ" not in page.text
    assert "s" * 32 not in page.text


def test_line_config_save_and_verify_flow(client):
    email, password, _, _ = _register_api(client)
    _login_ui(client, email, password)
    # 儲存（HTMX partial）
    r = client.post("/ui/line-config", data={
        "channel_secret": "c" * 32, "access_token": "tok-AAA", "default_target_lang": "zh-TW",
    })
    assert r.status_code == 200
    assert "valid" in r.text
    assert "tok-AAA" not in r.text  # 不洩漏明文
    # 重新驗證
    r2 = client.post("/ui/line-config/verify")
    assert r2.status_code == 200
    assert "憑證狀態" in r2.text


def test_settings_save_store_type(client):
    email, password, _, _ = _register_api(client)
    _login_ui(client, email, password)
    r = client.post("/ui/settings", data={"store_type": "Restaurant"})
    assert r.status_code == 200
    assert "restaurant" in r.text  # normalize → 小寫
    assert "已儲存" in r.text


# ── 平台管理 ────────────────────────────────────────────────────────────────

def test_admin_overview_renders(client):
    email, password, _, _ = _register_api(client)
    _make_admin(email)
    _login_ui(client, email, password)
    r = client.get("/ui/admin/bots")
    assert r.status_code == 200
    assert "跨店家" in r.text
    assert "<table" in r.text


def test_admin_filter_returns_partial_for_htmx(client):
    email, password, _, _ = _register_api(client)
    _make_admin(email)
    _login_ui(client, email, password)
    r = client.get("/ui/admin/bots?is_active=true", headers={"HX-Request": "true"})
    assert r.status_code == 200
    # partial：不含完整 HTML 文件骨架
    assert "<html" not in r.text.lower()


def test_non_admin_blocked_from_admin(client):
    email, password, _, _ = _register_api(client)  # 非 admin
    _login_ui(client, email, password)
    r = client.get("/ui/admin/bots", follow_redirects=False)
    assert r.status_code == 403


def test_admin_patch_updates_plan(client):
    email, password, _, _ = _register_api(client)
    _make_admin(email)
    _login_ui(client, email, password)
    _, _, _, target_tid = _register_api(client)  # 另一個被管理的租戶
    r = client.post(f"/ui/admin/tenants/{target_tid}/patch", data={
        "plan": "pro", "is_active": "true", "store_type": "retail",
    })
    assert r.status_code == 200
    assert "pro" in r.text
    assert "retail" in r.text
    assert "已儲存" in r.text


# ── 隔離 / 安全鐵則 ───────────────────────────────────────────────────────────

def test_api_path_ignores_ui_cookie(client):
    """登入後 cookie 在 jar 內，但 API /tenants/me 只認 header → cookie-only 仍 401。"""
    email, password, _, _ = _register_api(client)
    _login_ui(client, email, password)
    # 不帶 Authorization header，只靠 cookie jar 自動帶上的 access_token cookie
    r = client.get("/tenants/me")
    assert r.status_code == 401


def test_inactive_tenant_shows_disabled_page(client):
    email, password, _, tid = _register_api(client)
    _login_ui(client, email, password)
    # 停用該租戶
    from saas_mvp.models.tenant import Tenant
    db = _Session()
    try:
        db.get(Tenant, tid).is_active = False
        db.commit()
    finally:
        db.close()
    r = client.get("/ui/", follow_redirects=False)
    assert r.status_code == 403
    assert "停用" in r.text


# ── AI 客服浮動 widget ───────────────────────────────────────────────────────
def test_ai_widget_present_on_authed_pages(client):
    email, password, _, _ = _register_api(client)
    _login_ui(client, email, password)
    html = client.get("/ui/").text
    assert "aiw-fab" in html
    assert "/ui/ai-widget/ask" in html


def test_ai_widget_ask_returns_answer(client):
    email, password, _, _ = _register_api(client)
    _login_ui(client, email, password)
    r = client.post("/ui/ai-widget/ask", data={"question": "如何設定 LINE？"})
    assert r.status_code == 200
    # stub 助手會給出回覆，或在未開通時給友善訊息；至少回 200 partial
    assert "aiw-answer" in r.text


def test_ai_widget_requires_login(client):
    r = client.post("/ui/ai-widget/ask", data={"question": "hi"}, follow_redirects=False)
    assert r.status_code in (302, 303)
