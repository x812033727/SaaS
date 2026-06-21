"""GET /admin/line-bots — 跨店家 LINE bot 總覽測試。

涵蓋：
  - 未登入 → 401；非 admin → 403
  - admin happy path：回應為遮罩列（不含 channel_secret/access_token 明文、含 has_*）
  - 依 store_type 篩選
  - 依 is_active 篩選
  - uncategorized 篩選（store_type 為 NULL）
  - 分頁 skip/limit
  - 今日用量正確反映 ApiUsage
  - 無 LINE config 的租戶：has_line_config=false、credential_status=None、不崩潰

全部離線，in-memory SQLite。
"""

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
from saas_mvp.models.usage import ApiUsage

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
    # bot/info 用 stub，PUT line-config 不打真實網路。
    app.dependency_overrides[get_bot_info_client] = (
        lambda: StubLineBotInfoClient("U" + uuid.uuid4().hex)
    )
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register(client: TestClient, store_type: str | None = None) -> tuple[str, int]:
    email = f"user_{_uid()}@example.com"
    tn = f"tenant_{_uid()}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn,
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    tid = client.get("/tenants/me", headers=_auth(token)).json()["id"]
    if store_type is not None:
        client.put("/tenants/me", headers=_auth(token), json={"store_type": store_type})
    return token, tid


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


def _set_usage(tenant_id: int, count: int, chars: int) -> None:
    db = _Session()
    try:
        row = ApiUsage(
            tenant_id=tenant_id,
            period=datetime.date.today(),
            count=count,
            char_count=chars,
        )
        db.add(row)
        db.commit()
    finally:
        db.close()


@pytest.fixture(scope="module")
def admin_token(client):
    token, _ = _register(client)
    _make_admin(token)
    return token


# ── 授權邊界 ────────────────────────────────────────────────────────────────

def test_no_auth_401(client):
    r = client.get("/admin/line-bots")
    assert r.status_code == 401


def test_normal_user_403(client):
    token, _ = _register(client)
    r = client.get("/admin/line-bots", headers=_auth(token))
    assert r.status_code == 403


# ── happy path / 遮罩 ─────────────────────────────────────────────────────────

def test_overview_masks_secrets_and_has_flags(client, admin_token):
    token, tid = _register(client, store_type="restaurant")
    # 設定 LINE config（透過 admin PUT，stub 回 uid → valid）
    r = client.put(
        f"/admin/line-configs/{tid}",
        headers=_auth(admin_token),
        json={"channel_secret": "s" * 32, "access_token": "tok-secret"},
    )
    assert r.status_code == 200, r.text

    rows = client.get("/admin/line-bots", headers=_auth(admin_token)).json()
    row = next(r for r in rows if r["tenant_id"] == tid)
    # 遮罩：絕不含明文憑證
    assert "channel_secret" not in row
    assert "access_token" not in row
    # has_* 正確
    assert row["has_line_config"] is True
    assert row["has_channel_secret"] is True
    assert row["has_access_token"] is True
    assert row["store_type"] == "restaurant"
    assert row["credential_status"] == "valid"
    assert row["line_bot_user_id"] is not None


def test_tenant_without_config_renders(client, admin_token):
    _, tid = _register(client)
    rows = client.get("/admin/line-bots", headers=_auth(admin_token)).json()
    row = next(r for r in rows if r["tenant_id"] == tid)
    assert row["has_line_config"] is False
    assert row["has_channel_secret"] is False
    assert row["credential_status"] is None
    assert row["line_bot_user_id"] is None
    assert row["default_target_lang"] is None
    assert row["today_count"] == 0
    assert row["today_chars"] == 0


def test_usage_reflected(client, admin_token):
    _, tid = _register(client)
    _set_usage(tid, count=7, chars=123)
    rows = client.get("/admin/line-bots", headers=_auth(admin_token)).json()
    row = next(r for r in rows if r["tenant_id"] == tid)
    assert row["today_count"] == 7
    assert row["today_chars"] == 123


# ── 篩選 ────────────────────────────────────────────────────────────────────

def test_filter_by_store_type(client, admin_token):
    _, tid = _register(client, store_type="retail")
    rows = client.get(
        "/admin/line-bots?store_type=retail", headers=_auth(admin_token)
    ).json()
    assert all(r["store_type"] == "retail" for r in rows)
    assert any(r["tenant_id"] == tid for r in rows)


def test_filter_uncategorized(client, admin_token):
    _, tid = _register(client)  # 未設 store_type
    rows = client.get(
        "/admin/line-bots?uncategorized=true", headers=_auth(admin_token)
    ).json()
    assert all(r["store_type"] is None for r in rows)
    assert any(r["tenant_id"] == tid for r in rows)


def test_filter_by_is_active(client, admin_token):
    _, tid = _register(client)
    client.patch(
        f"/admin/tenants/{tid}", headers=_auth(admin_token), json={"is_active": False}
    )
    rows = client.get(
        "/admin/line-bots?is_active=false", headers=_auth(admin_token)
    ).json()
    assert all(r["is_active"] is False for r in rows)
    assert any(r["tenant_id"] == tid for r in rows)


def test_pagination(client, admin_token):
    # limit=1 應只回一列
    rows = client.get("/admin/line-bots?skip=0&limit=1", headers=_auth(admin_token)).json()
    assert len(rows) == 1
