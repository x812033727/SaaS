"""方案 bundle 訂閱（B2）測試 — stub 立即生效、ecpay 回調改 plan、退訂寬限。"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf, feature_change_history as _fch  # noqa: F401,E402
from saas_mvp.models import feature_subscription as _fs  # noqa: F401,E402
from saas_mvp.models import plan_change_history as _pch  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.plan_change_history import PlanChangeHistory  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import billing as billing_svc  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import subscriptions as subs_svc  # noqa: E402
from saas_mvp.services.payment_ecpay import EcpayClient  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_NOW = datetime.datetime(2030, 6, 15, 9, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db, **kw) -> Tenant:
    t = Tenant(name=f"bb_{uuid.uuid4().hex[:8]}", plan=kw.pop("plan", "free"), **kw)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _history_reasons(db, tenant_id) -> list[str]:
    return [
        h.reason or ""
        for h in db.execute(
            select(PlanChangeHistory)
            .where(PlanChangeHistory.tenant_id == tenant_id)
            .order_by(PlanChangeHistory.id)
        ).scalars()
    ]


# ── stub 模式（service 層）────────────────────────────────────────────────────

class TestStubBundle:
    def test_subscribe_changes_plan_and_clears_trial(self, db, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "stub")
        t = _tenant(
            db, trial_plan="pro",
            trial_ends_at=_NOW + datetime.timedelta(days=7),
        )
        r = billing_svc.subscribe_bundle(db, t, features_svc.BUNDLE_PRO, 1)
        assert r.mode == "stub" and r.enabled
        db.refresh(t)
        assert t.plan == "pro"
        assert t.trial_plan is None and t.trial_ends_at is None  # 轉正清試用
        assert len(_history_reasons(db, t.id)) == 1

    def test_subscribe_same_plan_rejected(self, db, monkeypatch):
        from fastapi import HTTPException

        monkeypatch.setattr(settings, "payment_provider", "stub")
        t = _tenant(db, plan="pro")
        with pytest.raises(HTTPException):
            billing_svc.subscribe_bundle(db, t, features_svc.BUNDLE_PRO, 1)

    def test_subscribe_unknown_bundle_rejected(self, db):
        from fastapi import HTTPException

        t = _tenant(db)
        with pytest.raises(HTTPException):
            billing_svc.subscribe_bundle(db, t, "BUNDLE_NOPE", 1)

    def test_unsubscribe_downgrades_with_grace(self, db, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "stub")
        t = _tenant(db, plan="standard")
        billing_svc.unsubscribe_bundle(db, t, 1)
        db.refresh(t)
        assert t.plan == "free"
        # 寬限：原方案以 trial 機制保留（stub 無扣款紀錄 → 以現在起算 31 天）
        assert t.trial_plan == "standard"
        assert t.trial_ends_at is not None

    def test_downgrade_keeps_old_plan_grace(self, db, monkeypatch):
        """C3:pro→standard 降級,pro 以寬限保留至最後扣款+31 天。"""
        monkeypatch.setattr(settings, "payment_provider", "stub")
        t = _tenant(db, plan="pro")
        r = billing_svc.subscribe_bundle(db, t, features_svc.BUNDLE_STANDARD, 1)
        assert r.enabled
        db.refresh(t)
        assert t.plan == "standard"          # 新方案立即開始
        assert t.trial_plan == "pro"         # 舊方案寬限保留
        assert t.trial_ends_at is not None
        from saas_mvp.services import plans as plans_svc

        assert plans_svc.effective_plan(t) == "pro"   # 寬限期內功能仍 pro

    def test_downgrade_ecpay_activation_does_not_clear_grace(self, db, monkeypatch):
        """C3:降級後新 standard 訂閱首期回調 activate,不得誤清 pro 寬限。"""
        import saas_mvp.services.payment_ecpay as pe

        monkeypatch.setattr(settings, "payment_provider", "ecpay")
        monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
        monkeypatch.setattr(pe, "_urllib_post", lambda url, data: "RtnCode=1&RtnMsg=OK")
        t = _tenant(db, plan="pro")
        old = subs_svc.create_subscription(
            db, tenant_id=t.id, feature=features_svc.BUNDLE_PRO, amount_cents=89900
        )
        subs_svc.activate(db, old)

        billing_svc.subscribe_bundle(db, t, features_svc.BUNDLE_STANDARD, 1)
        db.refresh(t)
        assert t.trial_plan == "pro"
        new_sub = subs_svc.latest_active_for(db, t.id, features_svc.BUNDLE_STANDARD)
        # 首期授權成功 → plan=standard,寬限欄位保留(rank standard < pro 不清)
        billing_svc.apply_bundle_activation(db, new_sub)
        db.refresh(t)
        assert t.plan == "standard"
        assert t.trial_plan == "pro" and t.trial_ends_at is not None

    def test_upgrade_clears_grace(self, db, monkeypatch):
        """升級(standard→pro)清 trial(轉正,既有行為不回歸)。"""
        monkeypatch.setattr(settings, "payment_provider", "stub")
        t = _tenant(
            db, plan="standard", trial_plan="pro",
            trial_ends_at=_NOW + datetime.timedelta(days=10),
        )
        billing_svc.subscribe_bundle(db, t, features_svc.BUNDLE_PRO, 1)
        db.refresh(t)
        assert t.plan == "pro"
        assert t.trial_plan is None and t.trial_ends_at is None

    def test_unsubscribe_free_noop_grace(self, db, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "stub")
        t = _tenant(db)
        billing_svc.unsubscribe_bundle(db, t, 1)
        db.refresh(t)
        assert t.plan == "free" and t.trial_plan is None


# ── ecpay 模式（回調端到端）──────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    monkeypatch.setattr(settings, "payment_provider", "ecpay")
    monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
    monkeypatch.setattr(settings, "features_default_enabled", False)
    app = create_app()

    def override_get_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _make_pending_bundle_sub(bundle=None, amount_cents=89900) -> tuple[str, int]:
    db = _Session()
    try:
        t = Tenant(name=f"bshop_{uuid.uuid4().hex[:6]}", plan="free")
        db.add(t)
        db.flush()
        sub = subs_svc.create_subscription(
            db, tenant_id=t.id,
            feature=bundle or features_svc.BUNDLE_PRO,
            amount_cents=amount_cents,
        )
        return sub.merchant_trade_no, t.id
    finally:
        db.close()


def _signed(params: dict) -> dict:
    params["CheckMacValue"] = EcpayClient().check_mac_value(params)
    return params


def _first_auth_params(trade_no, rtn_code="1"):
    return _signed({
        "MerchantID": "2000132",
        "MerchantTradeNo": trade_no,
        "RtnCode": rtn_code,
        "RtnMsg": "Succeeded" if rtn_code == "1" else "Failed",
        "Gwsr": "11122233",
        "AuthCode": "777777",
        "Amount": "899",
        "PeriodType": "M",
        "Frequency": "1",
        "ExecTimes": "99",
        "ProcessDate": "2024/01/01 12:00:00",
        "TotalSuccessTimes": "1",
    })


def _period_params(trade_no, rtn_code="1", total="2"):
    return _signed({
        "MerchantID": "2000132",
        "MerchantTradeNo": trade_no,
        "RtnCode": rtn_code,
        "RtnMsg": "Succeeded" if rtn_code == "1" else "Failed",
        "Gwsr": "11122244",
        "Amount": "899",
        "ProcessDate": "2024/02/01 12:00:00",
        "TotalSuccessTimes": total,
    })


def _plan_of(tenant_id) -> str:
    db = _Session()
    try:
        return db.get(Tenant, tenant_id).plan
    finally:
        db.close()


class TestEcpayBundleCallbacks:
    def test_first_auth_success_activates_plan(self, client):
        trade_no, tid = _make_pending_bundle_sub()
        r = client.post("/payments/ecpay/subscribe-callback", data=_first_auth_params(trade_no))
        assert r.text == "1|OK"
        assert _plan_of(tid) == "pro"
        # bundle 生效後,pro 內含功能經 is_enabled 第 2 層放行(嚴格 freemium 下)
        db = _Session()
        try:
            assert features_svc.is_enabled(db, tid, features_svc.COUPON_SYSTEM) is True
        finally:
            db.close()

    def test_first_auth_failure_keeps_free(self, client):
        trade_no, tid = _make_pending_bundle_sub()
        r = client.post(
            "/payments/ecpay/subscribe-callback", data=_first_auth_params(trade_no, rtn_code="0")
        )
        assert r.text == "1|OK"
        assert _plan_of(tid) == "free"

    def test_period_failure_downgrades_to_free_with_history(self, client):
        trade_no, tid = _make_pending_bundle_sub()
        client.post("/payments/ecpay/subscribe-callback", data=_first_auth_params(trade_no))
        assert _plan_of(tid) == "pro"
        r = client.post(
            "/payments/ecpay/period-callback", data=_period_params(trade_no, rtn_code="0")
        )
        assert r.text == "1|OK"
        assert _plan_of(tid) == "free"
        db = _Session()
        try:
            reasons = _history_reasons(db, tid)
            assert any(rs.startswith("bundle_charge_failed:") for rs in reasons)
        finally:
            db.close()

    def test_period_success_keeps_plan(self, client):
        trade_no, tid = _make_pending_bundle_sub()
        client.post("/payments/ecpay/subscribe-callback", data=_first_auth_params(trade_no))
        r = client.post("/payments/ecpay/period-callback", data=_period_params(trade_no))
        assert r.text == "1|OK"
        assert _plan_of(tid) == "pro"

    def test_bad_signature_no_plan_change(self, client):
        trade_no, tid = _make_pending_bundle_sub()
        params = _first_auth_params(trade_no)
        params["CheckMacValue"] = "0" * 64
        r = client.post("/payments/ecpay/subscribe-callback", data=params)
        assert r.text.startswith("0|")
        assert _plan_of(tid) == "free"


class TestEcpaySubscribeBundleService:
    def test_creates_pending_and_cancels_old(self, db, monkeypatch):
        import saas_mvp.services.payment_ecpay as pe

        monkeypatch.setattr(settings, "payment_provider", "ecpay")
        monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
        monkeypatch.setattr(pe, "_urllib_post", lambda url, data: "RtnCode=1&RtnMsg=OK")

        t = _tenant(db, plan="standard")
        old = subs_svc.create_subscription(
            db, tenant_id=t.id, feature=features_svc.BUNDLE_STANDARD, amount_cents=39900
        )
        subs_svc.activate(db, old)

        r = billing_svc.subscribe_bundle(db, t, features_svc.BUNDLE_PRO, 1)
        assert r.mode == "ecpay" and not r.enabled
        assert "/payments/ecpay/subscribe/" in (r.checkout_url or "")
        db.refresh(old)
        assert old.status == "cancelled"  # 換約先停舊
        db.refresh(t)
        assert t.plan == "standard"  # 新方案待首期回調才生效


# ── REST 端點 ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def api_client(monkeypatch):
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    monkeypatch.setattr(settings, "payment_provider", "stub")
    app = create_app()

    def override_get_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _register(client) -> str:
    email = f"o_{uuid.uuid4().hex[:8]}@x.tw"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!",
        "tenant_name": f"bt_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


class TestPlanEndpoints:
    def test_subscribe_plan_stub(self, api_client):
        token = _register(api_client)
        headers = {"Authorization": f"Bearer {token}"}
        r = api_client.post("/billing/plans/standard/subscribe", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["plan"] == "standard" and body["payment_id"]

    def test_subscribe_unknown_plan_400(self, api_client):
        token = _register(api_client)
        headers = {"Authorization": f"Bearer {token}"}
        r = api_client.post("/billing/plans/platinum/subscribe", headers=headers)
        assert r.status_code == 400

    def test_unsubscribe_plan(self, api_client):
        token = _register(api_client)
        headers = {"Authorization": f"Bearer {token}"}
        api_client.post("/billing/plans/pro/subscribe", headers=headers)
        r = api_client.post("/billing/plans/unsubscribe", headers=headers)
        assert r.status_code == 200

    def test_list_plans(self, api_client):
        token = _register(api_client)
        headers = {"Authorization": f"Bearer {token}"}
        r = api_client.get("/billing/plans", headers=headers)
        assert r.status_code == 200
        keys = [p["key"] for p in r.json()]
        assert keys == ["free", "standard", "pro"]


class TestPlanUI:
    def test_ui_subscribe_stub_redirects_and_changes_plan(self, api_client):
        email = f"ui_{uuid.uuid4().hex[:8]}@x.tw"
        name = f"uishop_{uuid.uuid4().hex[:8]}"
        api_client.post("/auth/register", json={
            "email": email, "password": "Test1234!", "tenant_name": name,
        })
        api_client.post("/ui/login", data={"email": email, "password": "Test1234!"})
        r = api_client.post("/ui/plan/standard/subscribe", follow_redirects=False)
        assert r.status_code == 303
        db = _Session()
        try:
            t = db.query(Tenant).filter(Tenant.name == name).one()
            assert t.plan == "standard"
        finally:
            db.close()

    def test_ui_billing_page_renders(self, api_client):
        email = f"bl_{uuid.uuid4().hex[:8]}@x.tw"
        name = f"blshop_{uuid.uuid4().hex[:8]}"
        api_client.post("/auth/register", json={
            "email": email, "password": "Test1234!", "tenant_name": name,
        })
        api_client.post("/ui/login", data={"email": email, "password": "Test1234!"})
        r = api_client.get("/ui/billing")
        assert r.status_code == 200
        assert "目前方案" in r.text
