"""R5-C1 — 平台月結對帳(monthly_statement / revenue_series / CSV 匯出)。"""

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
from saas_mvp.models.feature_subscription import (
    SUB_ACTIVE,
    SUB_CANCELLED,
    FeatureSubscription,
)
from saas_mvp.models.invoice import INVOICE_FAILED, INVOICE_ISSUED, Invoice
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

# 固定基準月,避免任何本地/UTC 邊界(R4 #137 教訓:測試日期一律顯式)。
_YEAR, _MONTH = 2030, 6
_IN_MONTH = datetime.datetime(2030, 6, 15, 10, 0)
_PREV_MONTH = datetime.datetime(2030, 5, 20, 10, 0)


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
    email = f"stmt_{uuid.uuid4().hex[:8]}@x.tw"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!",
        "tenant_name": f"Stmt {uuid.uuid4().hex[:6]}",
    })
    assert r.status_code == 201
    if admin:
        with _Session() as db:
            u = db.query(User).filter_by(email=email).one()
            u.is_admin = True
            db.commit()
    assert client.post(
        "/ui/login", data={"email": email, "password": "Test1234!"}
    ).status_code == 303
    return email


def _seed_charges(*, invoice_for_first=True):
    """種子:當月成功×2(8990+3990)/失敗×1/上月成功×1;churn+activated 各一。

    回傳 (tenant_id, first_charge_id)。第一筆成功扣款可選配 issued 發票,
    第二筆故意無發票(對帳應抓到)。
    """
    with _Session() as db:
        t = db.query(Tenant).first()
        sub_pro = FeatureSubscription(
            tenant_id=t.id, feature="bundle_pro", status=SUB_ACTIVE,
            period_amount_cents=89900, activated_at=_IN_MONTH,
            merchant_trade_no=f"MT{uuid.uuid4().hex[:12]}",
        )
        sub_std = FeatureSubscription(
            tenant_id=t.id, feature="bundle_standard", status=SUB_CANCELLED,
            period_amount_cents=39900, activated_at=_PREV_MONTH,
            cancelled_at=_IN_MONTH,
            merchant_trade_no=f"MT{uuid.uuid4().hex[:12]}",
        )
        db.add_all([sub_pro, sub_std])
        db.flush()
        c1 = SubscriptionCharge(
            tenant_id=t.id, subscription_id=sub_pro.id, period_no=1, success=True,
            amount_cents=89900, charged_at=_IN_MONTH,
        )
        c2 = SubscriptionCharge(
            tenant_id=t.id, subscription_id=sub_std.id, period_no=2, success=True,
            amount_cents=39900, charged_at=_IN_MONTH,
        )
        c3 = SubscriptionCharge(
            tenant_id=t.id, subscription_id=sub_pro.id, period_no=2, success=False,
            amount_cents=89900, charged_at=_IN_MONTH,
        )
        c_prev = SubscriptionCharge(
            tenant_id=t.id, subscription_id=sub_std.id, period_no=1, success=True,
            amount_cents=39900, charged_at=_PREV_MONTH,
        )
        db.add_all([c1, c2, c3, c_prev])
        db.flush()
        if invoice_for_first:
            db.add(Invoice(
                tenant_id=t.id, subscription_charge_id=c1.id,
                relate_number=f"RN{uuid.uuid4().hex[:10]}",
                amount_cents=89900, status=INVOICE_ISSUED,
                invoice_no="AB12345678",
            ))
        db.commit()
        return t.id, c1.id


class TestAccess:
    def test_regular_user_403(self, client):
        _login(client, admin=False)
        assert client.get("/ui/admin/statement").status_code == 403
        assert client.get(
            "/ui/admin/statement.csv?year=2030&month=6"
        ).status_code == 403


class TestMonthlyStatement:
    def test_aggregates_and_month_boundary(self, client):
        _login(client, admin=True)
        _seed_charges()
        with _Session() as db:
            st = admin_svc.monthly_statement(db, year=_YEAR, month=_MONTH)
        assert st["revenue_cents"] == 89900 + 39900  # 上月那筆不計
        assert st["charge_success"] == 2
        assert st["charge_failures"] == 1
        assert st["paying_tenants"] == 1
        assert st["arpu_cents"] == 89900 + 39900
        assert st["churned_count"] == 1
        assert st["churned_mrr_cents"] == 39900
        assert st["activated_count"] == 1
        assert st["activated_mrr_cents"] == 89900

    def test_missing_invoice_detection(self, client):
        _login(client, admin=True)
        tid, c1 = _seed_charges()
        with _Session() as db:
            st = admin_svc.monthly_statement(db, year=_YEAR, month=_MONTH)
        # c1 有 issued 發票;c2(39900)缺票 → 恰好一筆
        assert len(st["missing_invoices"]) == 1
        assert st["missing_invoices"][0]["amount_cents"] == 39900

    def test_failed_invoice_counts_as_missing(self, client):
        _login(client, admin=True)
        tid, c1 = _seed_charges(invoice_for_first=False)
        with _Session() as db:
            db.add(Invoice(
                tenant_id=tid, subscription_charge_id=c1,
                relate_number=f"RN{uuid.uuid4().hex[:10]}",
                amount_cents=89900, status=INVOICE_FAILED,
            ))
            db.commit()
            st = admin_svc.monthly_statement(db, year=_YEAR, month=_MONTH)
        assert len(st["missing_invoices"]) == 2  # failed 也算缺

    def test_parity_with_detail_rows(self, client):
        """彙總 == 明細加總(對帳口徑自洽)。"""
        _login(client, admin=True)
        _seed_charges()
        with _Session() as db:
            st = admin_svc.monthly_statement(db, year=_YEAR, month=_MONTH)
            rows = admin_svc.statement_charge_rows(db, year=_YEAR, month=_MONTH)
        assert sum(
            r["amount_cents"] for r in rows if r["success"]
        ) == st["revenue_cents"]
        assert sum(1 for r in rows if not r["success"]) == st["charge_failures"]


class TestRevenueSeries:
    def test_buckets_and_amounts(self, client):
        _login(client, admin=True)
        _seed_charges()
        now = datetime.datetime(2030, 7, 1, tzinfo=datetime.timezone.utc)
        with _Session() as db:
            series = admin_svc.revenue_series(db, months=3, now=now)
        assert [(p["year"], p["month"]) for p in series] == [
            (2030, 5), (2030, 6), (2030, 7),
        ]
        assert series[0]["revenue_cents"] == 39900
        assert series[1]["revenue_cents"] == 89900 + 39900
        assert series[1]["failures"] == 1
        assert series[2]["revenue_cents"] == 0


class TestPageAndCsv:
    def test_page_renders(self, client):
        _login(client, admin=True)
        _seed_charges()
        r = client.get(f"/ui/admin/statement?year={_YEAR}&month={_MONTH}")
        assert r.status_code == 200
        assert "月結對帳" in r.text
        assert "NT$1298" in r.text  # 129800 cents
        assert "缺有效發票" in r.text

    def test_csv_export(self, client):
        _login(client, admin=True)
        _seed_charges()
        r = client.get(f"/ui/admin/statement.csv?year={_YEAR}&month={_MONTH}")
        assert r.status_code == 200
        assert r.content.startswith(b"\xef\xbb\xbf")
        body = r.content.decode("utf-8-sig")
        assert "charge_id" in body
        assert body.count("\n") >= 4  # header + 3 筆當月
        assert "899" in body
