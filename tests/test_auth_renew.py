"""R4-C1 — 滑動續期:/auth/renew 端點 + /ui middleware 續期。"""

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

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.auth.security import create_access_token, decode_access_token  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
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


def _register(client) -> tuple[int, int, str]:
    """回 (user_id, tenant_id, token)。"""
    r = client.post("/auth/register", json={
        "email": f"rn_{uuid.uuid4().hex[:8]}@x.tw",
        "password": "Test1234!",
        "tenant_name": f"rn_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    payload = decode_access_token(token)
    return int(payload["sub"]), payload["tenant_id"], token


def _now_ts() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


class TestRenewEndpoint:
    def test_renew_keeps_identity_and_sets_oa(self, client):
        user_id, tenant_id, token = _register(client)
        r = client.post("/auth/renew", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        payload = decode_access_token(r.json()["access_token"])
        assert int(payload["sub"]) == user_id
        assert payload["tenant_id"] == tenant_id
        # 首次續期以「當下」為 oa 起點(舊票無 oa claim)
        assert abs(payload["oa"] - _now_ts()) < 60

    def test_renew_preserves_original_oa(self, client):
        user_id, tenant_id, _ = _register(client)
        oa = _now_ts() - 3600  # 一小時前登入
        token = create_access_token(user_id, tenant_id, original_auth_ts=oa)
        r = client.post("/auth/renew", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert decode_access_token(r.json()["access_token"])["oa"] == oa

    def test_expired_token_rejected(self, client):
        user_id, tenant_id, _ = _register(client)
        token = create_access_token(
            user_id, tenant_id,
            expires_delta=datetime.timedelta(minutes=-1),
        )
        r = client.post("/auth/renew", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_impersonation_token_403(self, client):
        user_id, tenant_id, _ = _register(client)
        token = create_access_token(user_id, tenant_id, impersonator_id=999)
        r = client.post("/auth/renew", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_session_window_cap(self, client):
        """oa 超過 session_renew_max_hours → 401 強制重登。"""
        user_id, tenant_id, _ = _register(client)
        oa = _now_ts() - (settings.session_renew_max_hours * 3600 + 60)
        token = create_access_token(user_id, tenant_id, original_auth_ts=oa)
        r = client.post("/auth/renew", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_no_token_401(self, client):
        assert client.post("/auth/renew").status_code == 401


class TestUiSlidingRenew:
    def _login_cookies(self, client) -> tuple[str, str]:
        email = f"ui_{uuid.uuid4().hex[:8]}@x.tw"
        client.post("/auth/register", json={
            "email": email, "password": "Test1234!",
            "tenant_name": f"ui_{uuid.uuid4().hex[:8]}",
        })
        client.post("/ui/login", data={"email": email, "password": "Test1234!"})
        return email, client.cookies.get("csrf_token")

    def test_near_expiry_cookie_renewed_and_csrf_preserved(self, client):
        email, csrf = self._login_cookies(client)
        # 換一顆快過期的票(10 分 < 30 分門檻)
        old = decode_access_token(client.cookies.get("access_token"))
        near = create_access_token(
            int(old["sub"]), old["tenant_id"],
            expires_delta=datetime.timedelta(minutes=10),
        )
        client.cookies.delete("access_token")
        client.cookies.set("access_token", near)
        r = client.get("/ui/", follow_redirects=False)
        assert r.status_code == 200
        set_cookies = r.headers.get_list("set-cookie")
        auth_sc = [c for c in set_cookies if c.startswith("access_token=")]
        assert auth_sc and any(c.startswith("saas_access_token=") for c in set_cookies)
        new_token = auth_sc[0].split("=", 1)[1].split(";", 1)[0].strip('"')
        assert new_token != near
        payload = decode_access_token(new_token)
        assert payload["exp"] > decode_access_token(near)["exp"]
        assert "oa" in payload
        # csrf 沿用舊值不輪替(壞掉 in-flight 表單)
        csrf_sc = [c for c in set_cookies if c.startswith("csrf_token=")]
        assert csrf_sc and csrf_sc[0].split("=", 1)[1].split(";", 1)[0].strip('"') == csrf

    def test_fresh_token_not_renewed(self, client):
        self._login_cookies(client)
        before = client.cookies.get("access_token")
        r = client.get("/ui/", follow_redirects=False)
        assert r.status_code == 200
        assert client.cookies.get("access_token") == before

    def test_impersonation_cookie_not_renewed(self, client):
        self._login_cookies(client)
        old = decode_access_token(client.cookies.get("access_token"))
        imp = create_access_token(
            int(old["sub"]), old["tenant_id"],
            expires_delta=datetime.timedelta(minutes=10),
            impersonator_id=42,
        )
        client.cookies.delete("access_token")
        client.cookies.set("access_token", imp)
        r = client.get("/ui/", follow_redirects=False)
        assert not any(
            c.startswith("access_token=") for c in r.headers.get_list("set-cookie")
        )
