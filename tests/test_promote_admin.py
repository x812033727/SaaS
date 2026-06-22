"""ops/promote_admin：建立 / 提權 / 取消平台管理員。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.auth.security import verify_password
from saas_mvp.db import Base, import_all_models
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.ops.promote_admin import PromoteError, main, promote_admin


@pytest.fixture
def factory():
    """共享單一 in-memory sqlite（StaticPool）→ 跨 session 同一份 DB。"""
    import_all_models()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_user(factory, email="owner@shop.tw", is_admin=False):
    s = factory()
    try:
        t = Tenant(name=f"t-{email}", plan="free")
        s.add(t)
        s.flush()
        u = User(email=email, hashed_password="x", tenant_id=t.id, is_admin=is_admin)
        s.add(u)
        s.commit()
        return u.id
    finally:
        s.close()


def test_promote_existing_user(factory):
    _seed_user(factory, "owner@shop.tw")
    s = factory()
    action, user = promote_admin(s, email="owner@shop.tw")
    s.close()
    assert action == "promoted"
    assert user.is_admin is True


def test_promote_is_idempotent(factory):
    _seed_user(factory, "owner@shop.tw")
    promote_admin(factory(), email="owner@shop.tw")
    action, user = promote_admin(factory(), email="owner@shop.tw")
    assert action == "already-admin"
    assert user.is_admin is True


def test_email_is_normalised(factory):
    _seed_user(factory, "owner@shop.tw")
    action, user = promote_admin(factory(), email="  OWNER@Shop.TW ")
    assert action == "promoted"


def test_demote(factory):
    _seed_user(factory, "owner@shop.tw", is_admin=True)
    action, user = promote_admin(factory(), email="owner@shop.tw", demote=True)
    assert action == "demoted"
    assert user.is_admin is False


def test_create_new_admin(factory):
    action, user = promote_admin(
        factory(), email="admin@you.tw", create=True, password="S3cret-pw",
    )
    assert action == "created"
    assert user.is_admin is True
    assert user.tenant_id
    # 密碼以 bcrypt 雜湊儲存，可驗證
    check = factory()
    stored = check.query(User).filter(User.email == "admin@you.tw").first()
    check.close()
    assert verify_password("S3cret-pw", stored.hashed_password)


def test_create_requires_password(factory):
    with pytest.raises(PromoteError):
        promote_admin(factory(), email="a@b.tw", create=True)


def test_create_rejects_existing_email(factory):
    _seed_user(factory, "owner@shop.tw")
    with pytest.raises(PromoteError):
        promote_admin(factory(), email="owner@shop.tw", create=True, password="x12345")


def test_promote_missing_user_errors(factory):
    with pytest.raises(PromoteError):
        promote_admin(factory(), email="ghost@nowhere.tw")


def test_create_and_demote_mutually_exclusive(factory):
    with pytest.raises(PromoteError):
        promote_admin(factory(), email="a@b.tw", create=True, password="x12345", demote=True)


# ── CLI main() ───────────────────────────────────────────────────────────────

def test_main_promotes_and_returns_zero(factory, capsys):
    _seed_user(factory, "owner@shop.tw")
    rc = main(["--email", "owner@shop.tw"], session_factory=factory)
    assert rc == 0
    assert "已提權為管理員" in capsys.readouterr().out


def test_main_create(factory, capsys):
    rc = main(
        ["--email", "admin@you.tw", "--password", "S3cret-pw", "--create"],
        session_factory=factory,
    )
    assert rc == 0
    assert "已建立管理員帳號" in capsys.readouterr().out


def test_main_returns_nonzero_on_error(factory, capsys):
    rc = main(["--email", "ghost@nowhere.tw"], session_factory=factory)
    assert rc == 2
