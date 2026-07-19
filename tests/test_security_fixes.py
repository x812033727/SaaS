"""安全強化回歸測試（S1–S4）。

S1：付費功能旁路 — 功能旗標未開通時，state-changing 的 /ui POST 也應回 feature-locked
    （此前只擋 GET 頁）。鏡像 tests/test_ui_new_features.py 的 in-memory + cookie 設定。
S2：送往付費 LLM 的 prompt 上限 + /ai 速率限制。
S3：公開端點 IP 速率限制（/p/{slug} 列舉防護）。
S4：theme_color CSS 注入 — upsert 應拒絕非法色碼。
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

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import (  # noqa: E402
    FakeLinePushClient,
    FakeLineRichMenuClient,
    StubLineBotInfoClient,
    get_bot_info_client,
    get_push_client,
    get_rich_menu_client,
)
from saas_mvp.models.user import User  # noqa: E402

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
_app.dependency_overrides[get_bot_info_client] = (
    lambda: StubLineBotInfoClient("U" + uuid.uuid4().hex)
)
_app.dependency_overrides[get_rich_menu_client] = lambda: FakeLineRichMenuClient()
_app.dependency_overrides[get_push_client] = lambda: FakeLinePushClient()


@pytest.fixture()
def client():
    with TestClient(_app, raise_server_exceptions=True) as c:
        yield c


def _login(client) -> str:
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    r = client.post("/ui/login", data={"email": email, "password": "Test1234!"})
    assert r.status_code == 200
    return email


def _disable(client, feature: str) -> None:
    """R12-C3a:/ui/features 頁已刪,改走 service 層關閉最近登入租戶的功能。"""
    del client
    from saas_mvp.services import features as features_svc

    db = _Session()
    try:
        user = db.query(User).order_by(User.id.desc()).first()
        features_svc.set_enabled(
            db, user.tenant_id, feature, False, actor_user_id=None, source="test"
        )
        db.commit()
    finally:
        db.close()


# ── S1：付費功能旁路 — POST 也要擋 ───────────────────────────────────────────

LOCKED_MARKER = "尚未開通"


class TestS1PaywallBypass:
    def test_faq_ask_locked_when_ai_disabled(self, client):
        """最重要：faq_ask 會呼叫付費 MiniMax API，未開通必須擋下。"""
        _login(client)
        _disable(client, "AI_ASSISTANT")
        r = client.post("/ui/faq/ask", data={"question": "營業時間？"})
        assert r.status_code == 200
        assert LOCKED_MARKER in r.text

    def test_enabled_still_works(self, client):
        """已訂閱租戶不受影響（預設全開）：faq_ask 正常回應、非 locked。"""
        _login(client)
        r = client.post("/ui/faq/ask", data={"question": "營業時間？"})
        assert r.status_code == 200
        assert LOCKED_MARKER not in r.text


# ── S2：付費 LLM prompt 上限 + /ai 速率限制 ──────────────────────────────────

class TestS2PromptCap:
    def test_ask_request_rejects_long_question(self):
        import pydantic

        from saas_mvp.routers.ai import AskRequest

        AskRequest(question="x" * 2000)  # 邊界內 OK
        with pytest.raises(pydantic.ValidationError):
            AskRequest(question="x" * 2001)

    def test_ask_request_still_requires_non_empty(self):
        import pydantic

        from saas_mvp.routers.ai import AskRequest

        with pytest.raises(pydantic.ValidationError):
            AskRequest(question="")

    def test_ai_router_has_rate_limit_dependency(self):
        from saas_mvp.auth.ratelimit import require_rate_limit
        from saas_mvp.routers.ai import router

        deps = [d.dependency for d in router.dependencies]
        assert require_rate_limit in deps

    def test_faq_ask_caps_overlong_question(self, client):
        _login(client)
        r = client.post("/ui/faq/ask", data={"question": "字" * 2001})
        assert r.status_code == 200
        assert "過長" in r.text


# ── S3：公開端點 IP 速率限制 ─────────────────────────────────────────────────

class TestS3PublicRateLimit:
    def test_public_router_has_limiter_dependency(self):
        from saas_mvp.auth.ratelimit import public_limiter
        from saas_mvp.routers import calendar as calendar_router
        from saas_mvp.routers import pii as pii_router
        from saas_mvp.routers import public as public_router

        for mod in (public_router, calendar_router, pii_router):
            deps = [d.dependency for d in mod.router.dependencies]
            assert public_limiter in deps, mod.__name__

    def test_public_profile_404_under_limit_when_disabled(self, client):
        """rate_limit_enabled=false（測試預設）：照常運作，未知 slug → 404。"""
        r = client.get("/p/does-not-exist")
        assert r.status_code == 404

    def test_limiter_returns_429_over_cap_when_enabled(self):
        """直接驗 SlidingWindowRateLimiter：開啟時超過 cap → 429。"""
        from fastapi import HTTPException

        from saas_mvp.auth.ratelimit import SlidingWindowRateLimiter

        lim = SlidingWindowRateLimiter(max_calls=3, window_seconds=60)
        for _ in range(3):
            lim._check_rate_limit("1.2.3.4")
        with pytest.raises(HTTPException) as exc:
            lim._check_rate_limit("1.2.3.4")
        assert exc.value.status_code == 429


# ── S4：theme_color CSS 注入 ─────────────────────────────────────────────────

class TestS4ThemeColorInjection:
    def test_upsert_rejects_injection_payload(self):
        from saas_mvp.db import Base as _Base
        from saas_mvp.models.tenant import Tenant
        from saas_mvp.services import profile as profile_svc

        _Base.metadata.create_all(bind=_engine)
        db = _Session()
        try:
            tenant = Tenant(name="t_" + uuid.uuid4().hex[:8])
            db.add(tenant)
            db.commit()
            with pytest.raises(profile_svc.InvalidThemeColorError):
                profile_svc.upsert(
                    db, tenant.id, slug="s-" + uuid.uuid4().hex[:8],
                    theme_color="#000;}body{x",
                )
            # 確認沒有寫入 verbatim payload
            saved = profile_svc.get_by_tenant(db, tenant.id)
            assert saved is None or "}body{" not in (saved.theme_color or "")
        finally:
            db.close()

    def test_upsert_accepts_valid_hex(self):
        from saas_mvp.models.tenant import Tenant
        from saas_mvp.services import profile as profile_svc

        db = _Session()
        try:
            tenant = Tenant(name="t_" + uuid.uuid4().hex[:8])
            db.add(tenant)
            db.commit()
            p = profile_svc.upsert(
                db, tenant.id, slug="s-" + uuid.uuid4().hex[:8],
                theme_color="#1a2b3c",
            )
            assert p.theme_color == "#1a2b3c"
        finally:
            db.close()
