"""公開店家頁 + 管理 upsert 測試 — slug 解析、404、SEO meta、租戶隔離、slug 衝突。"""

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
from saas_mvp.models import business_profile as _bp  # noqa: F401,E402
from saas_mvp.models import service_category as _sc, service as _svc  # noqa: F401,E402
from saas_mvp.models import product as _prod  # noqa: F401,E402
from saas_mvp.models import coupon as _coupon  # noqa: F401,E402
from saas_mvp.models import portfolio_category as _pc, portfolio_item as _pi  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf, feature_change_history as _fch  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.services import catalog as catalog_svc  # noqa: E402
from saas_mvp.services import coupons as coupons_svc  # noqa: E402
from saas_mvp.services import portfolio as portfolio_svc  # noqa: E402
from saas_mvp.services import shop as shop_svc  # noqa: E402

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


def _seed_tenant_content(tid: int, *, prefix: str) -> None:
    """為某租戶填入服務 / 商品 / 優惠券 / 作品。"""
    db = _Session()
    try:
        catalog_svc.create_service(
            db, tenant_id=tid, name=f"{prefix}-服務", duration_minutes=30, price_cents=50000
        )
        shop_svc.create_product(
            db, tenant_id=tid, name=f"{prefix}-商品", price_cents=12000
        )
        coupons_svc.create_coupon(
            db, tenant_id=tid, code=f"{prefix}CODE", name=f"{prefix}-優惠",
            discount_type="percent", discount_value=10,
        )
        portfolio_svc.create_item(
            db, tenant_id=tid, image_url=f"https://img/{prefix}.jpg", caption=f"{prefix}-作品"
        )
    finally:
        db.close()


def _upsert_profile(client, token, **fields):
    return client.put("/booking/profile", headers=_auth(token), json=fields)


class TestPublicProfile:
    def test_published_slug_renders_content(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        _seed_tenant_content(tid, prefix="alpha")
        slug = f"slug-{uuid.uuid4().hex[:8]}"
        r = _upsert_profile(
            client, token,
            slug=slug, display_name="Alpha 店",
            seo_title="Alpha SEO 標題", seo_description="Alpha SEO 說明",
            social_links='{"Instagram": "https://ig/alpha"}',
            is_published=True,
        )
        assert r.status_code == 200, r.text

        r = client.get(f"/p/{slug}")
        assert r.status_code == 200
        html = r.text
        # 服務 / 商品 / 優惠券 / 作品皆出現
        assert "alpha-服務" in html
        assert "alpha-商品" in html
        assert "alphaCODE" in html or "alpha-優惠" in html
        assert "alpha-作品" in html or "https://img/alpha.jpg" in html
        # 加入 Google 行事曆按鈕
        assert "calendar.google.com" in html

    def test_seo_meta_present(self, client):
        token = _register(client)
        slug = f"seo-{uuid.uuid4().hex[:8]}"
        r = _upsert_profile(
            client, token,
            slug=slug, display_name="SEO 店",
            seo_title="我的 SEO 標題", seo_description="我的 SEO 描述",
            is_published=True,
        )
        assert r.status_code == 200, r.text
        html = client.get(f"/p/{slug}").text
        assert "<title>我的 SEO 標題</title>" in html
        assert 'name="description" content="我的 SEO 描述"' in html
        assert 'property="og:title" content="我的 SEO 標題"' in html

    def test_unknown_slug_404(self, client):
        assert client.get("/p/does-not-exist-xyz").status_code == 404

    def test_unpublished_slug_404(self, client):
        token = _register(client)
        slug = f"draft-{uuid.uuid4().hex[:8]}"
        r = _upsert_profile(
            client, token, slug=slug, display_name="草稿店", is_published=False
        )
        assert r.status_code == 200, r.text
        assert client.get(f"/p/{slug}").status_code == 404

    def test_tenant_isolation(self, client):
        token_a = _register(client)
        token_b = _register(client)
        tid_a = _tenant_id_of(token_a)
        tid_b = _tenant_id_of(token_b)
        _seed_tenant_content(tid_a, prefix="iso_a")
        _seed_tenant_content(tid_b, prefix="iso_b")

        slug_a = f"isoa-{uuid.uuid4().hex[:8]}"
        r = _upsert_profile(
            client, token_a, slug=slug_a, display_name="ISO-A", is_published=True
        )
        assert r.status_code == 200, r.text

        html = client.get(f"/p/{slug_a}").text
        # A 的頁面只含 A 的資料，絕不洩漏 B
        assert "iso_a-服務" in html
        assert "iso_b" not in html


class TestProfileManagement:
    def test_owner_upsert_and_get(self, client):
        token = _register(client)
        slug = f"mgmt-{uuid.uuid4().hex[:8]}"
        r = _upsert_profile(client, token, slug=slug, display_name="管理店")
        assert r.status_code == 200, r.text
        assert r.json()["slug"] == slug

        # 再次 upsert 更新欄位
        r = _upsert_profile(client, token, intro="歡迎光臨")
        assert r.status_code == 200 and r.json()["intro"] == "歡迎光臨"

        # GET 讀回
        r = client.get("/booking/profile", headers=_auth(token))
        assert r.status_code == 200 and r.json()["display_name"] == "管理店"

    def test_slug_collision_409(self, client):
        token_a = _register(client)
        token_b = _register(client)
        slug = f"dup-{uuid.uuid4().hex[:8]}"
        assert _upsert_profile(client, token_a, slug=slug).status_code == 200
        r = _upsert_profile(client, token_b, slug=slug)
        assert r.status_code == 409

    def test_get_before_setup_404(self, client):
        token = _register(client)
        assert client.get("/booking/profile", headers=_auth(token)).status_code == 404


def _seed_staff_with_shift(tid: int, *, name: str) -> None:
    from saas_mvp.services import staff as staff_svc
    db = _Session()
    try:
        s = staff_svc.create_staff(db, tenant_id=tid, name=name)
        staff_svc.create_shift(
            db, tenant_id=tid, staff_id=s.id,
            weekday=0, start_time="10:00", end_time="18:00",
        )
    finally:
        db.close()


class TestPublicProfileNewFeatures:
    def test_announcement_and_jsonld_and_team(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        _seed_tenant_content(tid, prefix="beta")
        _seed_staff_with_shift(tid, name="設計師Amy")
        slug = f"slug-{uuid.uuid4().hex[:8]}"
        r = _upsert_profile(
            client, token,
            slug=slug, display_name="Beta 店",
            announcement="本週四公休一天",
            is_published=True,
        )
        assert r.status_code == 200, r.text
        html = client.get(f"/p/{slug}").text
        # 公告
        assert "本週四公休一天" in html
        # JSON-LD 結構化資料
        assert 'application/ld+json' in html
        assert '"@type": "LocalBusiness"' in html
        # 員工排班即時顯示
        assert "團隊排班" in html
        assert "設計師Amy" in html
        assert "週一" in html and "10:00" in html

    def test_announcement_optional(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        slug = f"slug-{uuid.uuid4().hex[:8]}"
        _upsert_profile(client, token, slug=slug, display_name="無公告店", is_published=True)
        html = client.get(f"/p/{slug}").text
        assert "class=\"announce\"" not in html  # 無公告不渲染公告區
        assert 'application/ld+json' in html  # JSON-LD 仍輸出
