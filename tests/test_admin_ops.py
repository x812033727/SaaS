"""R4-P2 — admin 營運總覽頁(MRR/扣款成功率/租戶健康)。"""

from __future__ import annotations

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.config import settings
from saas_mvp.db import Base, get_db
from saas_mvp.models.daily_tenant_stat import DailyTenantStat
from saas_mvp.models.feature_subscription import SUB_ACTIVE, FeatureSubscription
from saas_mvp.models.subscription_charge import SubscriptionCharge
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.services import admin as admin_svc

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "ui_csrf_enabled", True)
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        with _Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _login(client, *, admin: bool) -> str:
    email = f"ops_{uuid.uuid4().hex[:8]}@x.tw"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!",
        "tenant_name": f"Ops {uuid.uuid4().hex[:6]}",
    })
    assert r.status_code == 201
    if admin:
        with _Session() as db:
            u = db.query(User).filter_by(email=email).one()
            u.is_admin = True
            db.commit()
    assert client.post("/ui/login", data={"email": email, "password": "Test1234!"}).status_code == 303
    return email


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def test_regular_user_cannot_view_ops(client):
    _login(client, admin=False)
    assert client.get("/ui/admin/ops").status_code == 403


def test_ops_page_renders_mrr_and_health(client):
    _login(client, admin=True)
    with _Session() as db:
        t = db.query(Tenant).first()
        db.add(FeatureSubscription(
            tenant_id=t.id, feature="bundle_pro", status=SUB_ACTIVE,
            period_amount_cents=89900, activated_at=_now(),
            merchant_trade_no=f"MT{uuid.uuid4().hex[:12]}",
        ))
        db.add(DailyTenantStat(
            tenant_id=t.id, stat_date=_now().date(), bookings_total=5,
            computed_at=_now(),
        ))
        db.commit()
    r = client.get("/ui/admin/ops")
    assert r.status_code == 200
    assert "營運總覽" in r.text
    assert "月經常性收入" in r.text
    assert "NT$899" in r.text  # MRR
    assert "租戶健康" in r.text


class TestRevenueOverview:
    def test_mrr_only_counts_active(self, client):
        _login(client, admin=True)
        with _Session() as db:
            t = db.query(Tenant).first()
            db.add_all([
                FeatureSubscription(tenant_id=t.id, feature="bundle_pro",
                                    status=SUB_ACTIVE, period_amount_cents=89900,
                                    activated_at=_now(), merchant_trade_no=f"MT{uuid.uuid4().hex[:12]}"),
                FeatureSubscription(tenant_id=t.id, feature="bundle_standard",
                                    status="cancelled", period_amount_cents=39900,
                                    activated_at=_now(), merchant_trade_no=f"MT{uuid.uuid4().hex[:12]}"),
            ])
            db.commit()
            rev = admin_svc.revenue_overview(db)
        assert rev["mrr_total_cents"] == 89900  # cancelled 不計
        assert len(rev["mrr_by_plan"]) == 1

    def test_charge_success_rate_30d(self, client):
        _login(client, admin=True)
        with _Session() as db:
            t = db.query(Tenant).first()
            sub = FeatureSubscription(tenant_id=t.id, feature="bundle_pro",
                                      status=SUB_ACTIVE, period_amount_cents=89900,
                                      activated_at=_now(), merchant_trade_no=f"MT{uuid.uuid4().hex[:12]}")
            db.add(sub)
            db.flush()
            db.add_all([
                SubscriptionCharge(tenant_id=t.id, subscription_id=sub.id,
                                   period_no=1, success=True, amount_cents=89900,
                                   charged_at=_now()),
                SubscriptionCharge(tenant_id=t.id, subscription_id=sub.id,
                                   period_no=2, success=False, amount_cents=89900,
                                   charged_at=_now()),
            ])
            db.commit()
            rev = admin_svc.revenue_overview(db)
        assert rev["charges_30d"] == 2
        assert rev["charge_success_rate_30d"] == 0.5

    def test_upcoming_renewal_within_14d(self, client):
        _login(client, admin=True)
        with _Session() as db:
            t = db.query(Tenant).first()
            # 上次扣款在 ~28 天前 → 下次約 2 天後(月週期),落在 14 天窗
            db.add(FeatureSubscription(
                tenant_id=t.id, feature="bundle_pro", status=SUB_ACTIVE,
                period_amount_cents=89900,
                last_charged_at=_now() - datetime.timedelta(days=28),
                merchant_trade_no=f"MT{uuid.uuid4().hex[:12]}",
            ))
            db.commit()
            rev = admin_svc.revenue_overview(db)
        assert len(rev["upcoming_renewals"]) == 1


def test_tenant_health_rows_uses_preaggregation(client):
    _login(client, admin=True)
    with _Session() as db:
        t = db.query(Tenant).first()
        db.add(DailyTenantStat(
            tenant_id=t.id, stat_date=_now().date(), bookings_total=7,
            computed_at=_now(),
        ))
        db.commit()
        rows = admin_svc.tenant_health_rows(db)
    assert any(r["tenant_id"] == t.id and r["bookings_30d"] == 7 for r in rows)
