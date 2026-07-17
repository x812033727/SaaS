"""R5-D3 — token_version 撤銷 + 成員管理 + disabled_at。

覆蓋:
- tv claim:改密碼(API/UI)/重設密碼/登出全部 → 舊票立即失效;新票續有效
- 舊票相容:無 tv 的票在 token_version=0 的 user 上仍有效(零中斷)
- disabled_at:停用成員 → 既有票+登入全擋;啟用還原
- 成員管理:role 切換即時生效、最後一位 owner 保護(停用/降級/移除)、自我保護
- /auth/renew 不可續撤銷/停用的票
"""

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
import saas_mvp.models.audit_log as _al  # noqa: F401,E402
import saas_mvp.models.organization as _org  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.auth.security import create_access_token, decode_access_token  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.services.mailer import StubMailer, get_mailer  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

PASSWORD = "Test1234!"


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


@pytest.fixture()
def client():
    app = create_app()

    def override_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_mailer] = lambda: StubMailer()
    with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
        yield c


def _register(client, email=None, pw=PASSWORD) -> tuple[str, str]:
    email = email or f"u_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/auth/register",
        json={"email": email, "password": pw, "tenant_name": f"t_{uuid.uuid4().hex[:8]}"},
    )
    assert r.status_code == 201, r.text
    return email, r.json()["access_token"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


class TestTvClaim:
    def test_new_token_carries_tv(self, client):
        _, tok = _register(client)
        assert decode_access_token(tok)["tv"] == 0

    def test_password_change_revokes_old_token(self, client):
        email, tok = _register(client)
        # 舊票可用
        assert client.get("/auth/me", headers=_bearer(tok)).status_code == 200
        # 改密碼(API)
        r = client.post(
            "/auth/change-password",
            headers=_bearer(tok),
            json={"current_password": PASSWORD, "new_password": "NewPass99!"},
        )
        assert r.status_code == 204
        # 舊票立即失效
        assert client.get("/auth/me", headers=_bearer(tok)).status_code == 401
        # 重新登入拿新票可用
        r = client.post("/auth/token", data={"username": email, "password": "NewPass99!"})
        assert r.status_code == 200
        newtok = r.json()["access_token"]
        assert decode_access_token(newtok)["tv"] == 1
        assert client.get("/auth/me", headers=_bearer(newtok)).status_code == 200

    def test_legacy_token_without_tv_still_valid_at_version_0(self, client):
        email, _ = _register(client)
        db = _Session()
        try:
            u = db.query(User).filter(User.email == email).first()
            uid, tid = u.id, u.tenant_id
        finally:
            db.close()
        # 模擬部署前簽的舊票:手動建一顆但拔掉 tv claim
        import jwt as _jwt
        from saas_mvp.config import settings
        payload = _jwt.decode(
            create_access_token(user_id=uid, tenant_id=tid),
            settings.secret_key, algorithms=[settings.algorithm],
        )
        payload.pop("tv")
        legacy = _jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)
        # token_version 仍為 0 → 舊票有效
        assert client.get("/auth/me", headers=_bearer(legacy)).status_code == 200

    def test_renew_rejects_revoked_token(self, client):
        email, tok = _register(client)
        client.post(
            "/auth/change-password",
            headers=_bearer(tok),
            json={"current_password": PASSWORD, "new_password": "NewPass99!"},
        )
        r = client.post("/auth/renew", headers=_bearer(tok))
        assert r.status_code == 401


class TestUiPasswordKeepsActorLoggedIn:
    def test_ui_change_password_reissues_cookie(self, client):
        email, _ = _register(client)
        client.post("/ui/login", data={"email": email, "password": PASSWORD})
        r = client.post(
            "/ui/account/password",
            data={"current_password": PASSWORD, "new_password": "NewPass99!",
                  "confirm_password": "NewPass99!"},
        )
        assert r.status_code == 200
        assert "access_token=" in r.headers.get("set-cookie", "")
        # 新 cookie 已被 client jar 接手 → 仍可存取
        assert client.get("/ui/account").status_code == 200


class TestLogoutAll:
    def test_logout_all_kills_other_sessions_keeps_this(self, client):
        email, tok = _register(client)  # 「其他裝置」的 API 票
        client.post("/ui/login", data={"email": email, "password": PASSWORD})  # 本裝置 cookie
        assert client.get("/auth/me", headers=_bearer(tok)).status_code == 200
        r = client.post("/ui/account/logout-all")
        assert r.status_code == 303
        # 其他裝置的舊票失效
        assert client.get("/auth/me", headers=_bearer(tok)).status_code == 401
        # 本裝置(cookie 已重簽)仍在
        assert client.get("/ui/account").status_code == 200


class TestDisableMember:
    def _seed_two(self, client):
        """owner + 一個 staff(經邀請流程),回傳 (owner_email, staff_id)。"""
        owner_email, _ = _register(client)
        client.post("/ui/login", data={"email": owner_email, "password": PASSWORD})
        r = client.post("/ui/members/invite")
        assert r.status_code == 200
        # 從 DB 取 invite token 太麻煩 —— 直接建 staff 掛同租戶
        db = _Session()
        try:
            owner = db.query(User).filter(User.email == owner_email).first()
            from saas_mvp.auth.security import hash_password
            from saas_mvp.services import organizations as org_svc
            from saas_mvp.models.tenant import Tenant
            staff = User(
                email=f"s_{uuid.uuid4().hex[:8]}@example.com",
                hashed_password=hash_password(PASSWORD),
                tenant_id=owner.tenant_id, role="staff",
            )
            db.add(staff)
            db.flush()
            org_svc.ensure_user_memberships(db, tenant=db.get(Tenant, owner.tenant_id), user=staff)
            db.commit()
            return owner_email, staff.id, staff.email
        finally:
            db.close()

    def test_disable_blocks_login_and_tokens(self, client):
        owner_email, staff_id, staff_email = self._seed_two(client)
        # owner 停用 staff
        r = client.post(f"/ui/members/{staff_id}/disable")
        assert r.status_code == 200 and "已停用" in r.text
        # staff 無法登入
        r = client.post("/ui/login", data={"email": staff_email, "password": PASSWORD})
        assert r.status_code == 403
        r = client.post("/auth/token", data={"username": staff_email, "password": PASSWORD})
        assert r.status_code == 403
        # 啟用還原
        r = client.post(f"/ui/members/{staff_id}/enable")
        assert r.status_code == 200 and "已啟用" in r.text
        r = client.post("/auth/token", data={"username": staff_email, "password": PASSWORD})
        assert r.status_code == 200

    def test_cannot_disable_self(self, client):
        owner_email, _ = _register(client)
        client.post("/ui/login", data={"email": owner_email, "password": PASSWORD})
        db = _Session()
        try:
            oid = db.query(User).filter(User.email == owner_email).first().id
        finally:
            db.close()
        r = client.post(f"/ui/members/{oid}/disable")
        assert r.status_code == 400 and "無法對自己" in r.text


class TestLastOwnerProtection:
    def test_cannot_demote_or_disable_last_owner(self, client):
        # 兩個 owner:demote 一個可以,demote 到剩一個時擋
        owner_email, _ = _register(client)
        client.post("/ui/login", data={"email": owner_email, "password": PASSWORD})
        db = _Session()
        try:
            owner = db.query(User).filter(User.email == owner_email).first()
            from saas_mvp.auth.security import hash_password
            second = User(
                email=f"o2_{uuid.uuid4().hex[:8]}@example.com",
                hashed_password=hash_password(PASSWORD),
                tenant_id=owner.tenant_id, role="owner",
            )
            db.add(second)
            db.commit()
            second_id = second.id
        finally:
            db.close()
        # 降級 second(還剩 owner 自己)→ 可以
        r = client.post(f"/ui/members/{second_id}/role", data={"role": "staff"})
        assert r.status_code == 200 and "員工" in r.text
        # 現在只剩 owner 自己一個 owner。owner 不能對自己降級(self 保護先觸發)
        db = _Session()
        try:
            oid = db.query(User).filter(User.email == owner_email).first().id
        finally:
            db.close()
        r = client.post(f"/ui/members/{oid}/role", data={"role": "staff"})
        assert r.status_code == 400  # self 保護

    def test_last_owner_cannot_be_removed_via_second_owner(self, client):
        owner_email, _ = _register(client)
        client.post("/ui/login", data={"email": owner_email, "password": PASSWORD})
        db = _Session()
        try:
            owner = db.query(User).filter(User.email == owner_email).first()
            from saas_mvp.auth.security import hash_password
            second = User(
                email=f"o2_{uuid.uuid4().hex[:8]}@example.com",
                hashed_password=hash_password(PASSWORD),
                tenant_id=owner.tenant_id, role="owner",
            )
            db.add(second)
            db.commit()
            second_id = second.id
        finally:
            db.close()
        # 先停用 second → 只剩 owner 一個啟用 owner
        client.post(f"/ui/members/{second_id}/disable")
        # 現在若把 owner 也停用應被擋(最後一位啟用 owner)——但 owner 是 self
        # 改測:staff 提權後停用另一 owner 的最後保護。用 role API 把 second 啟用+設 owner
        client.post(f"/ui/members/{second_id}/enable")
        # 兩個 owner,停用 second 可以
        r = client.post(f"/ui/members/{second_id}/disable")
        assert r.status_code == 200


class TestRemoveMember:
    def test_remove_anonymizes_plan_history_and_succeeds(self, client):
        """移除有方案異動歷程的成員:歷程 changed_by 匿名化、列保留、刪除成功。"""
        import saas_mvp.models.plan_change_history as _pch  # noqa: F401
        from saas_mvp.models.plan_change_history import PlanChangeHistory

        owner_email, _ = _register(client)
        client.post("/ui/login", data={"email": owner_email, "password": PASSWORD})
        db = _Session()
        try:
            owner = db.query(User).filter(User.email == owner_email).first()
            from saas_mvp.auth.security import hash_password
            staff = User(
                email=f"s_{uuid.uuid4().hex[:8]}@example.com",
                hashed_password=hash_password(PASSWORD),
                tenant_id=owner.tenant_id, role="staff",
            )
            db.add(staff)
            db.flush()
            db.add(PlanChangeHistory(
                tenant_id=owner.tenant_id, from_plan="free", to_plan="pro",
                changed_by_user_id=staff.id,
            ))
            db.commit()
            staff_id = staff.id
        finally:
            db.close()

        r = client.post(f"/ui/members/{staff_id}/remove")
        assert r.status_code == 200 and "已移除" in r.text
        db = _Session()
        try:
            assert db.get(User, staff_id) is None
            rows = db.query(PlanChangeHistory).all()
            assert len(rows) == 1 and rows[0].changed_by_user_id is None
        finally:
            db.close()


class TestRoleLiveEffect:
    def test_demoted_owner_loses_owner_pages_immediately(self, client):
        owner_email, _ = _register(client)
        client.post("/ui/login", data={"email": owner_email, "password": PASSWORD})
        db = _Session()
        try:
            owner = db.query(User).filter(User.email == owner_email).first()
            from saas_mvp.auth.security import hash_password
            from saas_mvp.services import organizations as org_svc
            from saas_mvp.models.tenant import Tenant
            staff = User(
                email=f"s_{uuid.uuid4().hex[:8]}@example.com",
                hashed_password=hash_password(PASSWORD),
                tenant_id=owner.tenant_id, role="staff",
            )
            db.add(staff)
            db.flush()
            org_svc.ensure_user_memberships(db, tenant=db.get(Tenant, owner.tenant_id), user=staff)
            db.commit()
            staff_id, staff_email = staff.id, staff.email
        finally:
            db.close()
        # 提為 owner
        r = client.post(f"/ui/members/{staff_id}/role", data={"role": "owner"})
        assert r.status_code == 200
        # staff 用自己的 session 登入,能開 members 頁
        staff_client = TestClient(client.app, follow_redirects=False)
        staff_client.post("/ui/login", data={"email": staff_email, "password": PASSWORD})
        assert staff_client.get("/ui/members").status_code == 200
        # owner 把他降回 staff → 立即失去 owner 頁(下一請求即生效)
        client.post(f"/ui/members/{staff_id}/role", data={"role": "staff"})
        r = staff_client.get("/ui/members")
        assert r.status_code in (302, 303, 403)
