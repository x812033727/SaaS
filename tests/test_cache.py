"""R6-C1 — TTLCache 快取邏輯 + admin 儀表板快取整合。"""

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
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.services import cache as cache_svc  # noqa: E402
from saas_mvp.services.cache import TTLCache  # noqa: E402


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class TestTTLCache:
    def test_hit_within_ttl_does_not_recompute(self):
        clock = _Clock()
        c = TTLCache(clock=clock)
        calls = []

        def compute():
            calls.append(1)
            return len(calls)

        assert c.get_or_compute("k", 30, compute) == 1
        clock.advance(10)
        assert c.get_or_compute("k", 30, compute) == 1  # 命中,不重算
        assert len(calls) == 1

    def test_recompute_after_expiry(self):
        clock = _Clock()
        c = TTLCache(clock=clock)
        calls = []

        def compute():
            calls.append(1)
            return len(calls)

        assert c.get_or_compute("k", 30, compute) == 1
        clock.advance(31)  # 過期
        assert c.get_or_compute("k", 30, compute) == 2
        assert len(calls) == 2

    def test_ttl_zero_disables(self):
        c = TTLCache()
        calls = []
        for _ in range(3):
            c.get_or_compute("k", 0, lambda: calls.append(1))
        assert len(calls) == 3  # 每次都算

    def test_keys_isolated(self):
        c = TTLCache()
        assert c.get_or_compute("a", 30, lambda: "va") == "va"
        assert c.get_or_compute("b", 30, lambda: "vb") == "vb"
        assert c.get_or_compute("a", 30, lambda: "changed") == "va"  # a 仍快取

    def test_invalidate(self):
        c = TTLCache()
        c.get_or_compute("k", 30, lambda: "v1")
        c.invalidate("k")
        assert c.get_or_compute("k", 30, lambda: "v2") == "v2"

    def test_parity_cached_equals_direct(self):
        """快取值與直接計算相等(不改變結果,只避免重算)。"""
        c = TTLCache()
        direct = {"a": 1, "b": [2, 3]}
        cached = c.get_or_compute("k", 30, lambda: {"a": 1, "b": [2, 3]})
        assert cached == direct


_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


class TestAdminDashboardCacheIntegration:
    @pytest.fixture()
    def client(self, monkeypatch):
        Base.metadata.drop_all(bind=_engine)
        Base.metadata.create_all(bind=_engine)
        cache_svc.admin_dashboard_cache.invalidate()  # 跨測試不互污
        monkeypatch.setattr(settings, "admin_dashboard_cache_ttl_seconds", 60)
        app = create_app()

        def override_db():
            s = _Session()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db] = override_db
        with TestClient(app, follow_redirects=False) as c:
            yield c

    def _admin(self) -> str:
        from saas_mvp.auth.security import hash_password
        email = f"a_{uuid.uuid4().hex[:8]}@example.com"
        db = _Session()
        try:
            t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
            db.add(t)
            db.flush()
            db.add(User(email=email, hashed_password=hash_password("Test1234!"),
                        tenant_id=t.id, is_admin=True))
            db.commit()
        finally:
            db.close()
        return email

    def test_overview_computed_once_within_ttl(self, client, monkeypatch):
        """TTL=60 內兩次請求 admin 總覽:聚合只計算一次(第二次走快取)。"""
        from saas_mvp.services import admin as admin_svc
        calls = []
        real = admin_svc.platform_overview
        monkeypatch.setattr(
            admin_svc, "platform_overview",
            lambda db: (calls.append(1), real(db))[1],
        )
        email = self._admin()
        client.post("/ui/login", data={"email": email, "password": "Test1234!"})
        assert client.get("/ui/admin").status_code == 200
        assert client.get("/ui/admin").status_code == 200
        assert len(calls) == 1  # 第二次命中快取,未重算
        # 失效後重算
        cache_svc.admin_dashboard_cache.invalidate("platform_overview")
        assert client.get("/ui/admin").status_code == 200
        assert len(calls) == 2
