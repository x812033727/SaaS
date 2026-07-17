"""R6-B3 — 可設定 loyalty 分級/折扣/集點率。"""

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
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.tenant_loyalty_config import TenantLoyaltyConfig  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.services import loyalty_config as loyalty_svc  # noqa: E402
from saas_mvp.services import membership as membership_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture(autouse=True)
def _fresh():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


class TestPureFunctions:
    def test_recompute_tier_custom_thresholds(self):
        th = [(300, "gold"), (50, "silver"), (0, "regular")]
        assert membership_svc.recompute_tier(0, thresholds=th) == "regular"
        assert membership_svc.recompute_tier(50, thresholds=th) == "silver"
        assert membership_svc.recompute_tier(299, thresholds=th) == "silver"
        assert membership_svc.recompute_tier(300, thresholds=th) == "gold"

    def test_recompute_tier_default_when_none(self):
        assert membership_svc.recompute_tier(500) == "gold"
        assert membership_svc.recompute_tier(100) == "silver"
        assert membership_svc.recompute_tier(0) == "regular"

    def test_tier_discount_custom(self):
        d = {"gold": 20, "silver": 8, "regular": 2}
        assert membership_svc.tier_discount_percent("gold", discounts=d) == 20
        assert membership_svc.tier_discount_for("silver", 10000, discounts=d) == 800


class TestConfigService:
    def _tenant(self) -> int:
        db = _Session()
        try:
            t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
            db.add(t)
            db.commit()
            return t.id
        finally:
            db.close()

    def test_save_and_get(self):
        tid = self._tenant()
        db = _Session()
        try:
            loyalty_svc.save_config(
                db, tenant_id=tid, silver_threshold=50, gold_threshold=300,
                regular_discount_pct=1, silver_discount_pct=8, gold_discount_pct=20,
                points_per_booking=5,
            )
            cfg = loyalty_svc.get_config(db, tid)
            assert cfg.gold_threshold == 300 and cfg.gold_discount_pct == 20
            assert loyalty_svc.thresholds_for(cfg) == [(300, "gold"), (50, "silver"), (0, "regular")]
            assert loyalty_svc.discounts_for(cfg)["silver"] == 8
            assert loyalty_svc.points_per_booking_for(cfg) == 5
        finally:
            db.close()

    def test_defaults_when_no_config(self):
        tid = self._tenant()
        db = _Session()
        try:
            assert loyalty_svc.get_config(db, tid) is None
            assert loyalty_svc.thresholds_for(None) == [(500, "gold"), (100, "silver"), (0, "regular")]
            assert loyalty_svc.points_per_booking_for(None) == 10
        finally:
            db.close()

    def test_validation(self):
        tid = self._tenant()
        db = _Session()
        try:
            with pytest.raises(loyalty_svc.LoyaltyConfigError):
                loyalty_svc.save_config(
                    db, tenant_id=tid, silver_threshold=500, gold_threshold=100,
                    regular_discount_pct=0, silver_discount_pct=5, gold_discount_pct=10,
                    points_per_booking=10,
                )
            with pytest.raises(loyalty_svc.LoyaltyConfigError):
                loyalty_svc.save_config(
                    db, tenant_id=tid, silver_threshold=50, gold_threshold=300,
                    regular_discount_pct=0, silver_discount_pct=150, gold_discount_pct=10,
                    points_per_booking=10,
                )
        finally:
            db.close()


class TestEarnWithCustomThresholds:
    def test_earn_recomputes_tier_with_tenant_thresholds(self):
        db = _Session()
        try:
            t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
            db.add(t)
            db.flush()
            db.add(TenantLoyaltyConfig(
                tenant_id=t.id, silver_threshold=10, gold_threshold=30,
                regular_discount_pct=0, silver_discount_pct=5, gold_discount_pct=10,
                points_per_booking=10,
            ))
            c = Customer(tenant_id=t.id, line_user_id="Uloy", display_name="A")
            db.add(c)
            db.commit()
            # earn 15 → 自訂門檻下應為 silver(≥10),而非全域(需 100)
            membership_svc.earn_points(db, tenant_id=t.id, customer=c, delta=15, reason="booking")
            db.commit()
            assert c.tier == "silver"
        finally:
            db.close()


_CLIENT_ENGINE = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_CSession = sessionmaker(autocommit=False, autoflush=False, bind=_CLIENT_ENGINE)


class TestLoyaltyUI:
    @pytest.fixture()
    def client(self):
        Base.metadata.drop_all(bind=_CLIENT_ENGINE)
        Base.metadata.create_all(bind=_CLIENT_ENGINE)
        app = create_app()

        def override_db():
            s = _CSession()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db] = override_db
        with TestClient(app, follow_redirects=False) as c:
            yield c

    def _owner(self) -> str:
        from saas_mvp.auth.security import hash_password
        email = f"o_{uuid.uuid4().hex[:8]}@example.com"
        db = _CSession()
        try:
            t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
            db.add(t)
            db.flush()
            db.add(User(email=email, hashed_password=hash_password("Test1234!"),
                        tenant_id=t.id, role="owner"))
            db.commit()
        finally:
            db.close()
        return email

    def test_owner_can_save_config(self, client):
        email = self._owner()
        client.post("/ui/login", data={"email": email, "password": "Test1234!"})
        r = client.get("/ui/loyalty")
        assert r.status_code == 200 and "會員分級設定" in r.text
        r = client.post("/ui/loyalty", data={
            "silver_threshold": 50, "gold_threshold": 300,
            "regular_discount_pct": 0, "silver_discount_pct": 8, "gold_discount_pct": 20,
            "points_per_booking": 5,
        })
        assert r.status_code == 200 and "已儲存" in r.text

    def test_invalid_thresholds_400(self, client):
        email = self._owner()
        client.post("/ui/login", data={"email": email, "password": "Test1234!"})
        r = client.post("/ui/loyalty", data={
            "silver_threshold": 500, "gold_threshold": 100,
            "regular_discount_pct": 0, "silver_discount_pct": 5, "gold_discount_pct": 10,
            "points_per_booking": 10,
        })
        assert r.status_code == 400

    def test_staff_forbidden(self, client):
        from saas_mvp.auth.security import hash_password
        email = f"s_{uuid.uuid4().hex[:8]}@example.com"
        db = _CSession()
        try:
            t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
            db.add(t)
            db.flush()
            db.add(User(email=email, hashed_password=hash_password("Test1234!"),
                        tenant_id=t.id, role="staff"))
            db.commit()
        finally:
            db.close()
        client.post("/ui/login", data={"email": email, "password": "Test1234!"})
        r = client.get("/ui/loyalty")
        assert r.status_code == 403
