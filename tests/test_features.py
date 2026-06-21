"""進階功能旗標 service 測試（DB 直連）。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401
from saas_mvp.models import tenant_feature as _tf  # noqa: F401
from saas_mvp.models import feature_change_history as _fch  # noqa: F401

from saas_mvp.db import Base
from saas_mvp.models.feature_change_history import FeatureChangeHistory
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import features as f

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


def _tenant(db, name="f") -> int:
    t = Tenant(name=name, plan="free")
    db.add(t)
    db.commit()
    return t.id


def test_default_enabled_when_no_row(db):
    tid = _tenant(db)
    # SAAS_FEATURES_DEFAULT_ENABLED 預設 True → 無列時開通
    assert f.is_enabled(db, tid, f.COUPON_SYSTEM) is True


def test_strict_mode_default_disabled(db, monkeypatch):
    """SAAS_FEATURES_DEFAULT_ENABLED=False（嚴格 freemium）→ 無列時關閉。"""
    tid = _tenant(db)
    monkeypatch.setattr(f.settings, "features_default_enabled", False)
    assert f.is_enabled(db, tid, f.COUPON_SYSTEM) is False
    # 明確開通仍生效
    f.set_enabled(db, tid, f.COUPON_SYSTEM, True, actor_user_id=None, source="subscribe")
    assert f.is_enabled(db, tid, f.COUPON_SYSTEM) is True


def test_set_enabled_persists_and_writes_history(db):
    tid = _tenant(db)
    f.set_enabled(db, tid, f.COUPON_SYSTEM, False, actor_user_id=None, source="admin")
    assert f.is_enabled(db, tid, f.COUPON_SYSTEM) is False
    # 再開回
    f.set_enabled(db, tid, f.COUPON_SYSTEM, True, actor_user_id=None, source="subscribe")
    assert f.is_enabled(db, tid, f.COUPON_SYSTEM) is True
    # 稽核兩筆
    hist = db.execute(
        select(FeatureChangeHistory).where(FeatureChangeHistory.tenant_id == tid)
    ).scalars().all()
    assert len(hist) == 2
    assert {h.source for h in hist} == {"admin", "subscribe"}


def test_list_for_tenant(db):
    tid = _tenant(db)
    f.set_enabled(db, tid, f.PRODUCT_SALES, False, actor_user_id=None, source="admin")
    rows = {r["key"]: r for r in f.list_for_tenant(db, tid)}
    assert set(rows) == f.VALID_FEATURES
    assert rows[f.PRODUCT_SALES]["enabled"] is False
    assert rows[f.AUTO_REMINDER]["enabled"] is True  # 預設
    assert rows[f.COUPON_SYSTEM]["monthly_price_cents"] == 20000


def test_cross_tenant_isolation(db):
    a = _tenant(db, "a")
    b = _tenant(db, "b")
    f.set_enabled(db, a, f.COUPON_SYSTEM, False, actor_user_id=None, source="admin")
    assert f.is_enabled(db, a, f.COUPON_SYSTEM) is False
    assert f.is_enabled(db, b, f.COUPON_SYSTEM) is True  # b 不受影響


def test_unknown_feature_raises(db):
    tid = _tenant(db)
    with pytest.raises(f.UnknownFeatureError):
        f.set_enabled(db, tid, "NOPE", True, actor_user_id=None, source="admin")
    with pytest.raises(f.UnknownFeatureError):
        f.validate_feature("NOPE")
