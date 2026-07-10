"""B3 onboarding 測試 — email 驗證/忘記密碼/checklist/試用到期通知。"""

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
from saas_mvp.models import email_token as _et  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.email_token import EmailToken  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.ops.send_trial_notices import send_trial_notices  # noqa: E402
from saas_mvp.services import account_email as ae_svc  # noqa: E402
from saas_mvp.services import onboarding as onboarding_svc  # noqa: E402
from saas_mvp.services.mailer import StubMailer, get_mailer  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_NOW = datetime.datetime(2030, 6, 15, 9, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def stub_mailer():
    return StubMailer()


@pytest.fixture()
def client(stub_mailer):
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
    app.dependency_overrides[get_mailer] = lambda: stub_mailer
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _extract_token(body: str, path: str) -> str:
    """從信件內文抓 token（連結最後一段）。"""
    for line in body.splitlines():
        if path in line:
            return line.strip().rsplit("/", 1)[-1]
    raise AssertionError(f"no {path} link in mail body:\n{body}")


def _ui_register(client, *, email=None, name=None) -> tuple[str, str]:
    email = email or f"ob_{uuid.uuid4().hex[:8]}@x.tw"
    name = name or f"obshop_{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/ui/register",
        data={"email": email, "password": "longpassword", "tenant_name": name},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return email, name


# ── email 驗證 ───────────────────────────────────────────────────────────────

class TestEmailVerification:
    def test_register_sends_verification_and_verify_flow(self, client, stub_mailer):
        email, _ = _ui_register(client)
        assert len(stub_mailer.sent) == 1
        assert stub_mailer.sent[0].to == email
        token = _extract_token(stub_mailer.sent[0].body, "/ui/verify-email/")

        r = client.get(f"/ui/verify-email/{token}", follow_redirects=False)
        assert r.status_code == 303
        db = _Session()
        try:
            u = db.query(User).filter(User.email == email).one()
            assert u.email_verified_at is not None
        finally:
            db.close()

        # token 一次性：重放 → 400
        r = client.get(f"/ui/verify-email/{token}")
        assert r.status_code == 400

    def test_invalid_token_400(self, client):
        assert client.get("/ui/verify-email/garbage").status_code == 400

    def test_resend_verification(self, client, stub_mailer):
        _ui_register(client)  # 已登入（register 設 cookie）
        r = client.post("/ui/resend-verification", follow_redirects=False)
        assert r.status_code == 303
        assert len(stub_mailer.sent) == 2  # 註冊 1 + 重寄 1

    def test_dashboard_shows_unverified_banner(self, client):
        _ui_register(client)
        r = client.get("/ui/")
        assert "尚未驗證 Email" in r.text


# ── 忘記密碼 ─────────────────────────────────────────────────────────────────

class TestPasswordReset:
    def test_full_reset_flow(self, client, stub_mailer):
        email, _ = _ui_register(client)
        stub_mailer.sent.clear()
        r = client.post("/ui/forgot-password", data={"email": email})
        assert r.status_code == 200 and "已寄出" in r.text
        token = _extract_token(stub_mailer.sent[0].body, "/ui/reset-password/")

        r = client.post(
            f"/ui/reset-password/{token}", data={"password": "newpassword9"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        # 新密碼可登入
        r = client.post(
            "/ui/login", data={"email": email, "password": "newpassword9"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        # token 一次性
        r = client.post(
            f"/ui/reset-password/{token}", data={"password": "anotherpass1"}
        )
        assert r.status_code == 400

    def test_unknown_email_same_response(self, client, stub_mailer):
        """防帳號列舉：查無 email 回一樣的成功頁、不寄信。"""
        r = client.post("/ui/forgot-password", data={"email": "ghost@x.tw"})
        assert r.status_code == 200 and "已寄出" in r.text
        assert not stub_mailer.sent

    def test_short_password_rejected(self, client, stub_mailer):
        email, _ = _ui_register(client)
        stub_mailer.sent.clear()
        client.post("/ui/forgot-password", data={"email": email})
        token = _extract_token(stub_mailer.sent[0].body, "/ui/reset-password/")
        r = client.post(f"/ui/reset-password/{token}", data={"password": "short"})
        assert r.status_code == 400
        # 失敗不消耗 token
        r = client.post(
            f"/ui/reset-password/{token}", data={"password": "longenough1"},
            follow_redirects=False,
        )
        assert r.status_code == 303


# ── token 服務層 ─────────────────────────────────────────────────────────────

class TestTokenHygiene:
    def test_db_stores_hash_not_plaintext(self, client, stub_mailer):
        _ui_register(client)
        token = _extract_token(stub_mailer.sent[0].body, "/ui/verify-email/")
        db = _Session()
        try:
            rows = db.execute(select(EmailToken)).scalars().all()
            assert rows and all(r.token_hash != token for r in rows)
        finally:
            db.close()

    def test_expired_token_rejected(self, client, stub_mailer):
        _ui_register(client)
        token = _extract_token(stub_mailer.sent[0].body, "/ui/verify-email/")
        db = _Session()
        try:
            row = db.execute(select(EmailToken)).scalars().first()
            row.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
            db.commit()
            with pytest.raises(ae_svc.TokenInvalid):
                ae_svc.verify_email(db, token)
        finally:
            db.close()


# ── onboarding checklist ─────────────────────────────────────────────────────

class TestOnboardingChecklist:
    def test_fresh_tenant_progress(self, client):
        _ui_register(client)
        db = _Session()
        try:
            t = db.query(Tenant).order_by(Tenant.id.desc()).first()
            u = db.query(User).filter(User.tenant_id == t.id).one()
            items = {i["key"]: i["done"] for i in onboarding_svc.checklist(db, tenant=t, user=u)}
            assert items["verify_email"] is False
            assert items["line_config"] is False
            assert items["services"] is False
            assert items["slots"] is False
            assert items["plan"] is True  # 註冊自動開試用
        finally:
            db.close()

    def test_dashboard_renders_checklist(self, client):
        _ui_register(client)
        r = client.get("/ui/")
        assert "開店設定進度" in r.text and "綁定 LINE 官方帳號" in r.text


# ── 試用到期通知 ─────────────────────────────────────────────────────────────

class TestTrialNotices:
    def _tenant_with_trial(self, days_left: int) -> int:
        db = _Session()
        try:
            t = Tenant(
                name=f"tn_{uuid.uuid4().hex[:8]}", plan="free",
                trial_plan="pro",
                trial_ends_at=_NOW + datetime.timedelta(days=days_left),
            )
            db.add(t)
            db.flush()
            db.add(User(
                email=f"{t.name}@x.tw", hashed_password="x", tenant_id=t.id
            ))
            db.commit()
            return t.id
        finally:
            db.close()

    def test_notices_at_milestones_only(self, client, stub_mailer):
        hit7 = self._tenant_with_trial(7)
        self._tenant_with_trial(5)   # 非里程碑,不通知
        hit0 = self._tenant_with_trial(0)
        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

        results = send_trial_notices(
            session_factory=factory, mailer=stub_mailer, apply=True, now=_NOW
        )
        by_tenant = {r.tenant_id: r for r in results}
        assert by_tenant[hit7].status == "sent" and by_tenant[hit7].days_left == 7
        assert by_tenant[hit0].status == "sent" and by_tenant[hit0].days_left == 0
        assert len(results) == 2  # days_left=5 不出現
        assert any("已到期" in m.subject for m in stub_mailer.sent)
        assert any("還剩 7 天" in m.subject for m in stub_mailer.sent)

    def test_dry_run_sends_nothing(self, client, stub_mailer):
        self._tenant_with_trial(1)
        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        results = send_trial_notices(
            session_factory=factory, mailer=stub_mailer, apply=False, now=_NOW
        )
        assert results and results[-1].status == "would_send"
        assert not stub_mailer.sent
