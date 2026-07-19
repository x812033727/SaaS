"""訂閱帳單營運:逐期扣款明細、cancel_failed 重試、健康檢查、業務 gauges。

驗收標準
--------
- activate / record_period(成功/失敗)/ mark_failed 落 SubscriptionCharge;
  重放冪等(同 subscription+period+success 不重複)
- ops/retry_cancel_failed:成功停扣 → cancelled;RtnCode!=1 留在
  cancel_failed;例外隔離不中斷批次;dry-run 不打綠界
- ops/check_billing_health:三類異常偵測與 exit code
- /metrics 含業務 gauges;collect 拋錯時 /metrics 仍 200
- /ui/features 顯示訂閱狀態(cancel_failed 警示)與扣款紀錄
"""

from __future__ import annotations

import datetime
import io
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf  # noqa: F401,E402
from saas_mvp.models import feature_change_history as _fch  # noqa: F401,E402
from saas_mvp.models import feature_subscription as _fs  # noqa: F401,E402
from saas_mvp.models import subscription_charge as _sc  # noqa: F401,E402
import saas_mvp.models.line_webhook_event as _lwe  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.feature_subscription import (  # noqa: E402
    SUB_CANCEL_FAILED,
    SUB_CANCELLED,
    SUB_FAILED,
    FeatureSubscription,
)
from saas_mvp.models.subscription_charge import SubscriptionCharge  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.ops.check_billing_health import check_billing_health, main as health_main  # noqa: E402
from saas_mvp.ops.retry_cancel_failed import main as retry_main, retry_cancel_failed  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import subscriptions as subs_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db) -> int:
    t = Tenant(name=f"bo_{uuid.uuid4().hex[:6]}", plan="free")
    db.add(t)
    db.commit()
    return t.id


def _make_sub(db, tid: int, *, status: str | None = None) -> FeatureSubscription:
    sub = subs_svc.create_subscription(
        db, tenant_id=tid, feature="COUPON_SYSTEM", amount_cents=29900
    )
    if status is not None:
        sub.status = status
        db.commit()
        db.refresh(sub)
    return sub


def _charges(db, sub_id: int) -> list[SubscriptionCharge]:
    db.expire_all()
    return list(db.execute(
        select(SubscriptionCharge)
        .where(SubscriptionCharge.subscription_id == sub_id)
        .order_by(SubscriptionCharge.id)
    ).scalars())


class TestChargeLedger:
    def test_activate_records_first_charge(self, db):
        tid = _tenant(db)
        sub = _make_sub(db, tid)
        subs_svc.activate(db, sub, gwsr="G123", auth_code="777777")
        rows = _charges(db, sub.id)
        assert len(rows) == 1
        assert rows[0].period_no == 1 and rows[0].success is True
        assert rows[0].amount_cents == 29900
        assert rows[0].gwsr == "G123"

    def test_period_success_and_failure_recorded(self, db):
        tid = _tenant(db)
        sub = _make_sub(db, tid)
        subs_svc.activate(db, sub)
        subs_svc.record_period(db, sub, success=True)   # 第 2 期
        subs_svc.record_period(db, sub, success=False)  # 第 3 期失敗
        rows = _charges(db, sub.id)
        assert [(r.period_no, r.success) for r in rows] == [
            (1, True), (2, True), (3, False),
        ]

    def test_replay_idempotent(self, db):
        """綠界重送回調:同期成功不重複落列。"""
        tid = _tenant(db)
        sub = _make_sub(db, tid)
        subs_svc.activate(db, sub)
        subs_svc.record_period(db, sub, success=True, total_success_times=2)
        subs_svc.record_period(db, sub, success=True, total_success_times=2)  # 重放
        rows = _charges(db, sub.id)
        assert len(rows) == 2  # 期1 + 期2,無重複

    def test_first_auth_failure_recorded(self, db):
        tid = _tenant(db)
        sub = _make_sub(db, tid)
        subs_svc.mark_failed(db, sub)
        rows = _charges(db, sub.id)
        assert len(rows) == 1
        assert rows[0].period_no == 1 and rows[0].success is False


class _FakeEcpay:
    """可程式化回應/例外的 fake cancel_period client。"""

    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[str] = []

    def cancel_period(self, trade_no: str) -> dict:
        self.calls.append(trade_no)
        resp = self.responses.get(trade_no, {"RtnCode": "1"})
        if isinstance(resp, Exception):
            raise resp
        return resp


class TestRetryCancelFailed:
    def test_success_marks_cancelled(self, db):
        tid = _tenant(db)
        sub = _make_sub(db, tid, status=SUB_CANCEL_FAILED)
        fake = _FakeEcpay({sub.merchant_trade_no: {"RtnCode": "1"}})
        results = retry_cancel_failed(
            session_factory=_Session, ecpay_client=fake, apply=True
        )
        assert [r.status for r in results] == ["cancelled"]
        db.expire_all()
        assert db.get(FeatureSubscription, sub.id).status == SUB_CANCELLED

    def test_rtncode_failure_stays(self, db):
        tid = _tenant(db)
        sub = _make_sub(db, tid, status=SUB_CANCEL_FAILED)
        fake = _FakeEcpay({sub.merchant_trade_no: {"RtnCode": "0", "RtnMsg": "err"}})
        results = retry_cancel_failed(
            session_factory=_Session, ecpay_client=fake, apply=True
        )
        assert [r.status for r in results] == ["still_failed"]
        db.expire_all()
        assert db.get(FeatureSubscription, sub.id).status == SUB_CANCEL_FAILED

    def test_exception_isolated(self, db):
        """一筆網路例外不中斷批次:後面那筆仍成功處理。"""
        tid = _tenant(db)
        bad = _make_sub(db, tid, status=SUB_CANCEL_FAILED)
        good = _make_sub(db, tid, status=SUB_CANCEL_FAILED)
        fake = _FakeEcpay({
            bad.merchant_trade_no: OSError("network down"),
            good.merchant_trade_no: {"RtnCode": "1"},
        })
        results = retry_cancel_failed(
            session_factory=_Session, ecpay_client=fake, apply=True
        )
        by_id = {r.subscription_id: r.status for r in results}
        assert by_id[bad.id] == "error"
        assert by_id[good.id] == "cancelled"

    def test_dry_run_does_not_call_ecpay(self, db):
        tid = _tenant(db)
        _make_sub(db, tid, status=SUB_CANCEL_FAILED)
        fake = _FakeEcpay({})
        out = io.StringIO()
        rc = retry_main(
            [], session_factory=_Session, ecpay_client=fake, stdout=out
        )
        assert rc == 0
        assert fake.calls == []
        assert "would_retry" in out.getvalue()


class TestBillingHealth:
    def test_detects_three_anomalies(self, db):
        tid = _tenant(db)
        _make_sub(db, tid, status=SUB_CANCEL_FAILED)
        stale = _make_sub(db, tid)
        stale.created_at = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(hours=72)
        db.commit()
        # 不一致:failed 但旗標仍開
        bad = _make_sub(db, tid, status=SUB_FAILED)
        features_svc.set_enabled(
            db, tid, bad.feature, True, actor_user_id=None, source="admin"
        )

        report = check_billing_health(session_factory=_Session)
        assert len(report["cancel_failed"]) >= 1
        assert any(r["subscription_id"] == stale.id for r in report["stale_pending"])
        assert any(r["subscription_id"] == bad.id for r in report["inconsistent"])

        out = io.StringIO()
        rc = health_main([], session_factory=_Session, stdout=out)
        assert rc == 1  # 有異常 → exit 1

    def test_clean_returns_zero(self, db):
        tid = _tenant(db)
        sub = _make_sub(db, tid)
        subs_svc.activate(db, sub)
        out = io.StringIO()
        rc = health_main([], session_factory=_Session, stdout=out)
        assert rc == 0
        assert "anomalies=0" in out.getvalue()


# ── /metrics 業務 gauges 與 /ui/features ────────────────────────────────────


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


class TestMetricsGauges:
    def test_metrics_includes_business_gauges(self, client, monkeypatch):
        monkeypatch.setattr(settings, "metrics_enabled", True)
        # 造一筆 cancel_failed
        db = _Session()
        try:
            tid = _tenant(db)
            _make_sub(db, tid, status=SUB_CANCEL_FAILED)
        finally:
            db.close()
        # gauges 從 app 的 SessionLocal 讀（非 override）— monkeypatch 指到測試 engine
        import saas_mvp.db as dbmod

        monkeypatch.setattr(dbmod, "SessionLocal", _Session)
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "saas_subscriptions_cancel_failed 1" in r.text
        assert "saas_webhook_events_stuck_pending" in r.text

    def test_metrics_survives_collect_failure(self, client, monkeypatch):
        monkeypatch.setattr(settings, "metrics_enabled", True)
        import saas_mvp.obs.business as biz

        def boom(_db):
            raise RuntimeError("collector exploded")

        monkeypatch.setattr(biz, "collect_business_gauges", boom)
        r = client.get("/metrics")
        assert r.status_code == 200  # HTTP 指標仍要能刮


class TestFeaturesPage:
    def _login(self, client) -> int:
        email = f"feat_{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={
            "email": email, "password": "Test1234!",
            "tenant_name": f"feat_t_{uuid.uuid4().hex[:8]}",
        })
        assert r.status_code == 201
        token = r.json()["access_token"]
        tid = client.get(
            "/tenants/me", headers={"Authorization": f"Bearer {token}"}
        ).json()["id"]
        client.post("/ui/login", data={"email": email, "password": "Test1234!"},
                    follow_redirects=False)
        return tid
