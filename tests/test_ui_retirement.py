"""R11-D — /ui 已遷移頁退役重導(旗標開啟時)。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.config import settings
from saas_mvp.db import Base, get_db
from saas_mvp.ui_retirement import _REDIRECT_MAP


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)
    app = create_app()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def retired(monkeypatch):
    monkeypatch.setattr(settings, "ui_retired", True)


class TestRetirementRedirects:
    def test_every_migrated_page_redirects(self, client, retired):
        for ui_path, console_path in _REDIRECT_MAP.items():
            r = client.get(ui_path, follow_redirects=False)
            assert r.status_code == 302, f"{ui_path} → {r.status_code}"
            assert r.headers["location"] == console_path, ui_path

    def test_subpaths_redirect_to_section_root(self, client, retired):
        r = client.get("/ui/customers/123", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/console/customers"

    def test_ui_home_redirects_to_dashboard(self, client, retired):
        r = client.get("/ui/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/console/dashboard"

    def test_exempt_paths_untouched(self, client, retired):
        # 認證/公開/管理/精靈:不得重導(login 頁應正常渲染 200)
        assert client.get("/ui/login", follow_redirects=False).status_code == 200
        assert client.get("/ui/register", follow_redirects=False).status_code == 200
        # admin 未登入 → 既有行為(303 → /ui/login),但絕不是導 console
        r = client.get("/ui/admin", follow_redirects=False)
        assert not r.headers.get("location", "").startswith("/console")
        # join 公開頁(壞 token 也回頁面,非 console 重導)
        r2 = client.get("/ui/join/badtoken", follow_redirects=False)
        assert not r2.headers.get("location", "").startswith("/console")

    def test_post_never_redirected(self, client, retired):
        # 過渡期舊分頁的表單送出不得被 302 吞掉(此處未登入 → 認證層行為,
        # 但絕不是 retirement 的 /console 重導)
        r = client.post("/ui/booking", follow_redirects=False)
        assert not (
            r.status_code == 302
            and r.headers.get("location", "").startswith("/console")
        )

    def test_flag_off_serves_pages(self, client, monkeypatch):
        monkeypatch.setattr(settings, "ui_retired", False)
        r = client.get("/ui/login", follow_redirects=False)
        assert r.status_code == 200
        # 已遷頁在旗標關閉時照舊(未登入 → 導 /ui/login 而非 console)
        r2 = client.get("/ui/booking", follow_redirects=False)
        assert not r2.headers.get("location", "").startswith("/console")
