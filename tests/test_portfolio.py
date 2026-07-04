"""作品集（portfolio）REST + service 測試 — CRUD、list_public 排序/啟用篩選、租戶隔離。"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import portfolio_category as _pc, portfolio_item as _pi  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf, feature_change_history as _fch  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.services import portfolio as portfolio_svc  # noqa: E402

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


def _register(client) -> str:
    r = client.post("/auth/register", json={
        "email": f"u_{uuid.uuid4().hex[:8]}@example.com",
        "password": "Test1234!",
        "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _tenant_id_of(token) -> int:
    db = _Session()
    try:
        from saas_mvp.models.user import User
        from saas_mvp.auth.security import decode_access_token
        payload = decode_access_token(token)
        return db.query(User).filter(User.id == int(payload["sub"])).first().tenant_id
    finally:
        db.close()


class TestPortfolioCRUD:
    def test_category_and_item_crud(self, client):
        token = _register(client)
        # 建分類
        r = client.post(
            "/booking/portfolio/categories",
            headers=_auth(token),
            json={"name": "美甲", "sort_order": 1},
        )
        assert r.status_code == 201, r.text
        cat_id = r.json()["id"]

        # 建作品
        r = client.post(
            "/booking/portfolio/items",
            headers=_auth(token),
            json={
                "image_url": "https://img.example.com/a.jpg",
                "category_id": cat_id,
                "caption": "作品 A",
                "sort_order": 2,
            },
        )
        assert r.status_code == 201, r.text
        item_id = r.json()["id"]
        assert r.json()["image_url"] == "https://img.example.com/a.jpg"

        # 列出作品
        r = client.get("/booking/portfolio/items", headers=_auth(token))
        assert r.status_code == 200 and len(r.json()) == 1

        # 更新作品
        r = client.put(
            f"/booking/portfolio/items/{item_id}",
            headers=_auth(token),
            json={"caption": "改後"},
        )
        assert r.status_code == 200 and r.json()["caption"] == "改後"

        # 刪除作品
        r = client.delete(f"/booking/portfolio/items/{item_id}", headers=_auth(token))
        assert r.status_code == 204
        r = client.get("/booking/portfolio/items", headers=_auth(token))
        assert r.json() == []

        # 刪分類
        r = client.delete(
            f"/booking/portfolio/categories/{cat_id}", headers=_auth(token)
        )
        assert r.status_code == 204

    def test_unknown_item_404(self, client):
        token = _register(client)
        r = client.get("/booking/portfolio/items/999999", headers=_auth(token))
        assert r.status_code == 404

    def test_get_one_category(self, client):
        token = _register(client)
        cat_id = client.post(
            "/booking/portfolio/categories",
            headers=_auth(token),
            json={"name": "單查", "sort_order": 3},
        ).json()["id"]
        r = client.get(
            f"/booking/portfolio/categories/{cat_id}", headers=_auth(token)
        )
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "單查" and r.json()["sort_order"] == 3
        # 查無 → 404
        assert client.get(
            "/booking/portfolio/categories/999999", headers=_auth(token)
        ).status_code == 404
        # 跨租戶 → 404
        token_b = _register(client)
        assert client.get(
            f"/booking/portfolio/categories/{cat_id}", headers=_auth(token_b)
        ).status_code == 404


class TestListPublic:
    def test_ordering_and_active_filter(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        db = _Session()
        try:
            # 建立三張作品，sort_order 亂序，其中一張停用。
            portfolio_svc.create_item(
                db, tenant_id=tid, image_url="https://e/2.jpg", sort_order=2
            )
            portfolio_svc.create_item(
                db, tenant_id=tid, image_url="https://e/1.jpg", sort_order=1
            )
            inactive = portfolio_svc.create_item(
                db, tenant_id=tid, image_url="https://e/9.jpg", sort_order=0
            )
            portfolio_svc.update_item(
                db, tenant_id=tid, item_id=inactive.id, is_active=False
            )

            public = portfolio_svc.list_public(db, tid)
            # 停用張被濾掉；剩兩張依 sort_order 升序。
            urls = [it.image_url for it in public]
            assert urls == ["https://e/1.jpg", "https://e/2.jpg"]
        finally:
            db.close()


class TestTenantIsolation:
    def test_item_isolation(self, client):
        token_a = _register(client)
        token_b = _register(client)
        # A 建作品
        r = client.post(
            "/booking/portfolio/items",
            headers=_auth(token_a),
            json={"image_url": "https://a/secret.jpg"},
        )
        a_item = r.json()["id"]

        # B 看不到 A 的作品（list 為空、直接取為 404）。
        r = client.get("/booking/portfolio/items", headers=_auth(token_b))
        assert r.status_code == 200 and r.json() == []
        r = client.get(f"/booking/portfolio/items/{a_item}", headers=_auth(token_b))
        assert r.status_code == 404
