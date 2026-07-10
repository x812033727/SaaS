"""R2-2 測試 — F1 audit log / F3 webhook 診斷+健康檢查 / F2 impersonation。"""

from __future__ import annotations

import datetime
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.audit_log import AuditLog  # noqa: E402
from saas_mvp.models.line_webhook_event import (  # noqa: E402
    LineWebhookEvent,
    LineWebhookEventStatus,
)
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.obs.errors import safe_traceback  # noqa: E402
from saas_mvp.ops.check_webhook_health import check_webhook_health  # noqa: E402
from saas_mvp.services import audit as audit_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_NOW = datetime.datetime.now(datetime.timezone.utc)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


# ── F1 audit ─────────────────────────────────────────────────────────────────

class TestAuditService:
    def test_record_and_scrub(self, db):
        audit_svc.record(
            db, action="admin.tenant.patch", actor_user_id=1, tenant_id=2,
            target="tenant:2",
            detail={"plan": "pro", "channel_secret": "SHOULD-HIDE",
                    "nested": {"api_key": "HIDE-TOO", "ok": "visible"}},
            ip="1.2.3.4",
        )
        db.commit()
        row = db.execute(select(AuditLog)).scalar_one()
        assert row.action == "admin.tenant.patch"
        detail = json.loads(row.detail_json)
        assert detail["channel_secret"] == "***"
        assert detail["nested"]["api_key"] == "***"
        assert detail["nested"]["ok"] == "visible"
        assert "SHOULD-HIDE" not in row.detail_json

    def test_record_never_raises(self):
        class Boom:
            def add(self, *_a, **_k):
                raise RuntimeError("db down")

        audit_svc.record(Boom(), action="x")  # 不拋

    def test_rollback_discards_audit(self, db):
        audit_svc.record(db, action="billing.plan.subscribe", tenant_id=1)
        db.rollback()
        db.commit()
        assert db.execute(select(AuditLog)).scalar_one_or_none() is None


# ── F3 safe_traceback + webhook health ───────────────────────────────────────

class TestWebhookDiagnostics:
    def test_safe_traceback_masks_sensitive(self):
        try:
            secret = "Bearer abc123token"  # noqa: F841
            raise ValueError("Authorization failed for Bearer abc123token")
        except ValueError as exc:
            tb = safe_traceback(exc)
        assert "abc123token" not in tb
        assert "[redacted line]" in tb or "ValueError" in tb
        assert len(tb) <= 4000

    def test_health_detects_stale_and_ratio(self, db):
        t = Tenant(name=f"wh_{uuid.uuid4().hex[:6]}", plan="free")
        db.add(t)
        db.flush()
        old = _NOW - datetime.timedelta(minutes=30)
        db.add(LineWebhookEvent(
            tenant_id=t.id, webhook_event_id="stale-1",
            status=LineWebhookEventStatus.PENDING.value,
            last_stage="claimed", attempt_count=1, updated_at=old, created_at=old,
        ))
        for i in range(6):
            db.add(LineWebhookEvent(
                tenant_id=t.id, webhook_event_id=f"f-{i}",
                status=LineWebhookEventStatus.FAILED.value,
                last_stage="claimed", attempt_count=1,
                created_at=_NOW, updated_at=_NOW,
            ))
        for i in range(6):
            db.add(LineWebhookEvent(
                tenant_id=t.id, webhook_event_id=f"p-{i}",
                status=LineWebhookEventStatus.PROCESSED.value,
                last_stage="reply_sent", attempt_count=1,
                created_at=_NOW, updated_at=_NOW,
            ))
        db.commit()
        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        report = check_webhook_health(session_factory=factory, now=_NOW)
        assert len(report["stale_pending"]) == 1
        assert report["stale_pending"][0]["webhook_event_id"] == "stale-1"
        assert report["failed_24h"] == 6
        assert report["ratio_exceeded"] is True  # 6/12 = 0.5 > 0.1

    def test_health_clean(self, db):
        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        report = check_webhook_health(session_factory=factory, now=_NOW)
        assert report["stale_pending"] == [] and not report["ratio_exceeded"]


# ── F2 impersonation ─────────────────────────────────────────────────────────

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
    with TestClient(app) as c:
        yield c


def _register(client, name=None) -> tuple[str, int]:
    email = f"imp_{uuid.uuid4().hex[:8]}@x.tw"
    name = name or f"impshop_{uuid.uuid4().hex[:8]}"
    client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": name,
    })
    db = _Session()
    try:
        u = db.query(User).filter(User.email == email).one()
        return email, u.tenant_id
    finally:
        db.close()


def _make_admin(email) -> None:
    db = _Session()
    try:
        u = db.query(User).filter(User.email == email).one()
        u.is_admin = True
        db.commit()
    finally:
        db.close()


class TestImpersonation:
    def test_full_flow(self, client):
        owner_email, shop_tid = _register(client)
        admin_email, _ = _register(client)
        _make_admin(admin_email)
        client.post("/ui/login", data={"email": admin_email, "password": "Test1234!"})

        # 進入代管
        r = client.post(f"/ui/admin/tenants/{shop_tid}/impersonate", follow_redirects=False)
        assert r.status_code == 303

        # banner 顯示、身分是 owner、進不了 admin 頁
        page = client.get("/ui/")
        assert "代管中" in page.text and owner_email in page.text
        assert client.get("/ui/admin", follow_redirects=False).status_code in (303, 403)

        # 代管期間操作記入 admin 身分(掛點自動帶 impersonator)
        client.post("/ui/plan/standard/subscribe", follow_redirects=False)
        db = _Session()
        try:
            row = db.execute(
                select(AuditLog).where(AuditLog.action == "billing.plan.subscribe")
            ).scalar_one()
            assert row.impersonator_user_id is not None
            start = db.execute(
                select(AuditLog).where(AuditLog.action == "impersonation.start")
            ).scalar_one()
            assert start.tenant_id == shop_tid
        finally:
            db.close()

        # 結束代管 → 回 admin 身分
        r = client.post("/ui/impersonation/stop", follow_redirects=False)
        assert r.status_code == 303
        assert client.get("/ui/admin").status_code == 200
        db = _Session()
        try:
            assert db.execute(
                select(AuditLog).where(AuditLog.action == "impersonation.stop")
            ).scalar_one() is not None
        finally:
            db.close()

    def test_cannot_impersonate_admin(self, client):
        target_email, target_tid = _register(client)
        _make_admin(target_email)  # 目標也是 admin
        admin_email, _ = _register(client)
        _make_admin(admin_email)
        client.post("/ui/login", data={"email": admin_email, "password": "Test1234!"})
        r = client.post(f"/ui/admin/tenants/{target_tid}/impersonate")
        assert r.status_code == 403

    def test_no_chained_impersonation(self, client):
        _, shop_tid = _register(client)
        _, shop2_tid = _register(client)
        admin_email, _ = _register(client)
        _make_admin(admin_email)
        client.post("/ui/login", data={"email": admin_email, "password": "Test1234!"})
        client.post(f"/ui/admin/tenants/{shop_tid}/impersonate", follow_redirects=False)
        # 代管中再代管 → 已非 admin 身分,require_ui_admin 擋(403)
        r = client.post(f"/ui/admin/tenants/{shop2_tid}/impersonate", follow_redirects=False)
        assert r.status_code in (303, 403)
        assert client.get("/ui/admin", follow_redirects=False).status_code in (303, 403)

    def test_imp_token_dies_when_admin_demoted(self, client):
        _, shop_tid = _register(client)
        admin_email, _ = _register(client)
        _make_admin(admin_email)
        client.post("/ui/login", data={"email": admin_email, "password": "Test1234!"})
        client.post(f"/ui/admin/tenants/{shop_tid}/impersonate", follow_redirects=False)
        # admin 被降權 → 代管票整張失效(fail-closed)
        db = _Session()
        try:
            u = db.query(User).filter(User.email == admin_email).one()
            u.is_admin = False
            db.commit()
        finally:
            db.close()
        r = client.get("/ui/", follow_redirects=False)
        assert r.status_code == 303  # 導回 login(票無效)
