"""B4 營運基礎測試 — 平台總覽 / Sentry 告警降級 / 備份腳本存在性。"""

from __future__ import annotations

import datetime
import os
import pathlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import feature_subscription as _fs  # noqa: F401,E402
from saas_mvp.models import subscription_charge as _sc  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.services import admin as admin_svc  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import subscriptions as subs_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


class TestPlatformOverview:
    def test_counts_plans_trials_and_mrr(self, db):
        db.add(Tenant(name="free_shop", plan="free"))
        db.add(Tenant(name="pro_shop", plan="pro"))
        trial = Tenant(
            name="trial_shop", plan="free", trial_plan="pro",
            trial_ends_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=5),
        )
        db.add(trial)
        db.flush()
        # active bundle 訂閱 → MRR
        sub = subs_svc.create_subscription(
            db, tenant_id=trial.id,
            feature=features_svc.BUNDLE_PRO, amount_cents=89900,
        )
        subs_svc.activate(db, sub)

        o = admin_svc.platform_overview(db)
        assert o["tenant_total"] == 3
        assert o["trials_active"] == 1
        assert o["plan_distribution"]["pro"] == 2  # 付費 pro + 試用中 pro
        assert o["plan_distribution"]["free"] == 1
        assert o["mrr_cents"] == 89900
        assert o["month_charges_cents"] == 89900  # activate 記了第 1 期成功

    def test_empty_platform(self, db):
        o = admin_svc.platform_overview(db)
        assert o["tenant_total"] == 0 and o["mrr_cents"] == 0


class TestAlerts:
    def test_capture_alert_never_raises_when_disabled(self):
        from saas_mvp.obs.alerts import capture_alert

        capture_alert("test alert — sentry disabled")  # 不設 DSN:只記 log,不拋

    def test_init_sentry_noop_without_dsn(self, monkeypatch):
        from saas_mvp.config import settings
        from saas_mvp.obs import alerts

        monkeypatch.setattr(settings, "sentry_dsn", "")
        alerts.init_sentry()  # no-op,不拋


class TestAdminOverviewUI:
    @pytest.fixture()
    def client(self):
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

    def _admin_login(self, client) -> None:
        email = f"adm_{uuid.uuid4().hex[:6]}@x.tw"
        client.post("/auth/register", json={
            "email": email, "password": "Test1234!",
            "tenant_name": f"admshop_{uuid.uuid4().hex[:6]}",
        })
        s = _Session()
        try:
            u = s.query(User).filter(User.email == email).one()
            u.is_admin = True
            s.commit()
        finally:
            s.close()
        client.post("/ui/login", data={"email": email, "password": "Test1234!"})

    def test_overview_page_renders_for_admin(self, client):
        self._admin_login(client)
        r = client.get("/ui/admin")
        assert r.status_code == 200
        assert "平台總覽" in r.text and "MRR" in r.text

    def test_overview_forbidden_for_non_admin(self, client):
        email = f"usr_{uuid.uuid4().hex[:6]}@x.tw"
        client.post("/auth/register", json={
            "email": email, "password": "Test1234!",
            "tenant_name": f"usrshop_{uuid.uuid4().hex[:6]}",
        })
        client.post("/ui/login", data={"email": email, "password": "Test1234!"})
        r = client.get("/ui/admin", follow_redirects=False)
        assert r.status_code in (303, 403)  # 非 admin 導走/拒絕


def test_offsite_backup_script_valid_bash():
    script = _REPO / "docker" / "offsite-backup.sh"
    assert script.exists()
    import subprocess
    import sys as _sys

    r = subprocess.run(["bash", "-n", str(script)], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()
