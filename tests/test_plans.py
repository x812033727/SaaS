"""方案 bundle（B1 變現翻正）測試。

覆蓋:
- PLAN_BUNDLES 結構(pro ⊇ standard、PUSH_BOOST 不在任何 bundle)
- effective_plan / trial_active(試用中、到期即刻降回、naive datetime、非法值)
- features.is_enabled 三層判定(明確列 > bundle > fallback)
- start_trial(settings 驅動;trial_days=0 停用)
- push_quota 依方案分級額度
- ops/backfill_trial_grandfather(冪等、跳過 pro/試用中)
- UI:註冊自動開試用、/ui/pricing 公開、/ui/plan 需登入
"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf  # noqa: F401,E402
from saas_mvp.models import feature_change_history as _fch  # noqa: F401,E402
from saas_mvp.models import push_usage as _pu  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.ops.backfill_trial_grandfather import backfill_trial  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import plans as plans_svc  # noqa: E402
from saas_mvp.services import push_quota as push_quota_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
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


def _tenant(db, *, plan="free", trial_plan=None, trial_ends_at=None) -> Tenant:
    t = Tenant(
        name=f"pl_{uuid.uuid4().hex[:8]}", plan=plan,
        trial_plan=trial_plan, trial_ends_at=trial_ends_at,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ── bundle 結構 ───────────────────────────────────────────────────────────────

def test_bundles_are_nested_and_push_boost_excluded():
    std = plans_svc.PLAN_BUNDLES[plans_svc.PLAN_STANDARD]
    pro = plans_svc.PLAN_BUNDLES[plans_svc.PLAN_PRO]
    free = plans_svc.PLAN_BUNDLES[plans_svc.PLAN_FREE]
    assert free <= std <= pro  # 上級方案涵蓋下級
    for bundle in plans_svc.PLAN_BUNDLES.values():
        assert features_svc.PUSH_BOOST not in bundle  # 單點加購不入 bundle
    # bundle 內全是合法 feature key
    for bundle in plans_svc.PLAN_BUNDLES.values():
        assert bundle <= features_svc.VALID_FEATURES


def test_plan_pricing_from_settings():
    assert plans_svc.plan_price_cents("free") == 0
    assert plans_svc.plan_price_cents("standard") == settings.plan_standard_price_cents
    assert plans_svc.plan_price_cents("pro") == settings.plan_pro_price_cents


# ── effective_plan / trial ────────────────────────────────────────────────────

def test_effective_plan_no_trial(db):
    t = _tenant(db, plan="standard")
    assert plans_svc.effective_plan(t, now=_NOW) == "standard"
    assert not plans_svc.trial_active(t, now=_NOW)


def test_effective_plan_trial_active_and_expiry(db):
    t = _tenant(
        db, plan="free", trial_plan="pro",
        trial_ends_at=_NOW + datetime.timedelta(days=3),
    )
    assert plans_svc.effective_plan(t, now=_NOW) == "pro"
    assert plans_svc.trial_active(t, now=_NOW)
    # 到期即刻降回（純計算，無 cron）
    after = _NOW + datetime.timedelta(days=3, seconds=1)
    assert plans_svc.effective_plan(t, now=after) == "free"
    assert not plans_svc.trial_active(t, now=after)


def test_effective_plan_naive_datetime_treated_as_utc(db):
    t = _tenant(
        db, plan="free", trial_plan="pro",
        trial_ends_at=datetime.datetime(2030, 6, 18, 9, 0),  # naive（SQLite 常見）
    )
    assert plans_svc.effective_plan(t, now=_NOW) == "pro"


def test_effective_plan_invalid_values_ignored(db):
    t = _tenant(
        db, plan="weird_legacy_value", trial_plan="nonsense",
        trial_ends_at=_NOW + datetime.timedelta(days=3),
    )
    # 非法 trial_plan 忽略試用；非法 plan 正規化為 free
    assert plans_svc.effective_plan(t, now=_NOW) == "free"


def test_start_trial_and_disable(db, monkeypatch):
    t = _tenant(db)
    monkeypatch.setattr(settings, "trial_days", 14)
    monkeypatch.setattr(settings, "trial_plan", "pro")
    plans_svc.start_trial(t, now=_NOW)
    assert t.trial_plan == "pro"
    assert t.trial_ends_at == _NOW + datetime.timedelta(days=14)

    t2 = _tenant(db)
    monkeypatch.setattr(settings, "trial_days", 0)  # 停用
    plans_svc.start_trial(t2, now=_NOW)
    assert t2.trial_plan is None


# ── is_enabled 三層判定 ───────────────────────────────────────────────────────

@pytest.fixture()
def strict_freemium(monkeypatch):
    """模擬正式環境:fallback 關(conftest 為相容既有測試設 true)。"""
    monkeypatch.setattr(settings, "features_default_enabled", False)


def test_is_enabled_layer2_bundle(db, strict_freemium):
    std = _tenant(db, plan="standard")
    assert features_svc.is_enabled(db, std.id, features_svc.AUTO_REMINDER) is True
    assert features_svc.is_enabled(db, std.id, features_svc.COUPON_SYSTEM) is False
    pro = _tenant(db, plan="pro")
    assert features_svc.is_enabled(db, pro.id, features_svc.COUPON_SYSTEM) is True
    free = _tenant(db)
    assert features_svc.is_enabled(db, free.id, features_svc.SERVICE_CATALOG) is True
    assert features_svc.is_enabled(db, free.id, features_svc.AUTO_REMINDER) is False


def test_is_enabled_layer1_explicit_row_overrides_bundle(db, strict_freemium):
    pro = _tenant(db, plan="pro")
    # admin 明確關掉 → 即使 pro bundle 內含也要關（第 1 層優先）
    features_svc.set_enabled(
        db, pro.id, features_svc.COUPON_SYSTEM, False,
        actor_user_id=None, source="admin",
    )
    assert features_svc.is_enabled(db, pro.id, features_svc.COUPON_SYSTEM) is False
    # free 租戶單點訂閱 → bundle 沒有也開
    free = _tenant(db)
    features_svc.set_enabled(
        db, free.id, features_svc.MARKETING_AUTO, True,
        actor_user_id=None, source="subscribe",
    )
    assert features_svc.is_enabled(db, free.id, features_svc.MARKETING_AUTO) is True


def test_is_enabled_trial_grants_bundle_then_expires(db, strict_freemium):
    t = _tenant(
        db, plan="free", trial_plan="pro",
        trial_ends_at=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=7),
    )
    assert features_svc.is_enabled(db, t.id, features_svc.MARKETING_AUTO) is True
    # 試用過期 → 即刻失效
    t.trial_ends_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    db.commit()
    assert features_svc.is_enabled(db, t.id, features_svc.MARKETING_AUTO) is False


def test_is_enabled_layer3_fallback(db, monkeypatch):
    free = _tenant(db)
    monkeypatch.setattr(settings, "features_default_enabled", True)
    assert features_svc.is_enabled(db, free.id, features_svc.MARKETING_AUTO) is True
    monkeypatch.setattr(settings, "features_default_enabled", False)
    assert features_svc.is_enabled(db, free.id, features_svc.MARKETING_AUTO) is False


# ── push 額度依方案分級 ───────────────────────────────────────────────────────

def test_push_allowance_by_plan(db):
    free = _tenant(db)
    std = _tenant(db, plan="standard")
    pro = _tenant(db, plan="pro")
    assert push_quota_svc.allowance(db, free.id) == settings.push_allowance_base
    assert push_quota_svc.allowance(db, std.id) == settings.push_allowance_standard
    assert push_quota_svc.allowance(db, pro.id) == settings.push_allowance_pro


def test_push_allowance_trial_and_boost(db):
    t = _tenant(
        db, plan="free", trial_plan="pro",
        trial_ends_at=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=7),
    )
    assert push_quota_svc.allowance(db, t.id) == settings.push_allowance_pro
    features_svc.set_enabled(
        db, t.id, features_svc.PUSH_BOOST, True,
        actor_user_id=None, source="subscribe",
    )
    assert (
        push_quota_svc.allowance(db, t.id)
        == settings.push_allowance_pro + settings.push_allowance_boost
    )


# ── grandfather backfill ─────────────────────────────────────────────────────

def test_backfill_trial_grandfather(db):
    plain = _tenant(db)                       # 該補
    pro = _tenant(db, plan="pro")             # 跳過（已付費 pro）
    trialing = _tenant(                        # 跳過（試用中）
        db, plan="free", trial_plan="pro",
        trial_ends_at=_NOW + datetime.timedelta(days=5),
    )
    factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

    dry = backfill_trial(session_factory=factory, apply=False, days=30, now=_NOW)
    by_id = {r.tenant_id: r.status for r in dry}
    assert by_id[plain.id] == "would_grant"
    assert by_id[pro.id] == "skipped_pro"
    assert by_id[trialing.id] == "skipped_active_trial"

    applied = backfill_trial(session_factory=factory, apply=True, days=30, now=_NOW)
    assert {r.tenant_id: r.status for r in applied}[plain.id] == "granted"
    db.expire_all()
    db.refresh(plain)
    assert plain.trial_plan == "pro"
    assert plain.trial_ends_at is not None

    # 冪等：重跑不重補、不延長
    again = backfill_trial(session_factory=factory, apply=True, days=30, now=_NOW)
    assert {r.tenant_id: r.status for r in again}[plain.id] == "skipped_active_trial"


# ── UI ───────────────────────────────────────────────────────────────────────

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


def test_register_starts_trial(client):
    name = f"trialshop_{uuid.uuid4().hex[:6]}"
    r = client.post(
        "/ui/register",
        data={"email": f"{name}@x.tw", "password": "longpassword", "tenant_name": name},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = _Session()
    try:
        t = db.query(Tenant).filter(Tenant.name == name).one()
        assert t.trial_plan == plans_svc.normalize_plan(settings.trial_plan)
        assert t.trial_ends_at is not None
    finally:
        db.close()


def test_pricing_page_public(client):
    r = client.get("/ui/pricing")
    assert r.status_code == 200
    assert "標準版" in r.text and "專業版" in r.text


def test_plan_page_requires_login(client):
    r = client.get("/ui/plan", follow_redirects=False)
    assert r.status_code == 303  # 未登入導回 login
