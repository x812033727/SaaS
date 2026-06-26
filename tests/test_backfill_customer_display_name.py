"""Offline tests for saas_mvp.ops.backfill_customer_display_name."""

from __future__ import annotations

import os
from io import StringIO

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "SAAS_LINE_CHANNEL_ENCRYPT_KEY",
    "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=",
)

from saas_mvp.db import Base
from saas_mvp.line_client import LineUserProfile
from saas_mvp.models.customer import Customer
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.tenant import Tenant
from saas_mvp.ops.backfill_customer_display_name import (
    backfill_customer_display_names,
    main,
)

UID_A = "U" + "a" * 32
UID_B = "U" + "b" * 32


class FakeProfileClient:
    def __init__(self, responses):
        self.responses = responses  # {user_id: LineUserProfile | None | Exception}
        self.calls: list[tuple[str, str]] = []

    def get_profile(self, user_id, *, access_token):
        self.calls.append((user_id, access_token))
        resp = self.responses[user_id]
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture()
def db_session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield Session
    engine.dispose()


def _seed(Session, *, with_config=True, customers):
    """customers: list of (line_user_id, display_name). 回傳 tenant_id。"""
    with Session() as db:
        t = Tenant(name="shop", plan="free")
        db.add(t)
        db.flush()
        if with_config:
            cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
            cfg.channel_secret = "secret"
            cfg.access_token = "tok-for-tenant"
            db.add(cfg)
        for uid, name in customers:
            db.add(Customer(tenant_id=t.id, line_user_id=uid, display_name=name))
        db.commit()
        return t.id


def _name(Session, tenant_id, uid):
    with Session() as db:
        return db.execute(
            select(Customer.display_name).where(
                Customer.tenant_id == tenant_id, Customer.line_user_id == uid
            )
        ).scalar_one()


def test_dry_run_does_not_commit(db_session_factory):
    tid = _seed(db_session_factory, customers=[(UID_A, None)])
    client = FakeProfileClient({UID_A: LineUserProfile(UID_A, "王小明")})
    results = backfill_customer_display_names(
        session_factory=db_session_factory,
        profile_client=client,
        apply=False,
        sleep_seconds=0,
    )
    assert [r.status for r in results] == ["updated"]
    assert results[0].reason == "dry_run"
    assert client.calls == [(UID_A, "tok-for-tenant")]
    # 未 commit：DB 仍為 None
    assert _name(db_session_factory, tid, UID_A) is None


def test_apply_writes_display_name(db_session_factory):
    tid = _seed(db_session_factory, customers=[(UID_A, None)])
    client = FakeProfileClient({UID_A: LineUserProfile(UID_A, "王小明")})
    results = backfill_customer_display_names(
        session_factory=db_session_factory,
        profile_client=client,
        apply=True,
        sleep_seconds=0,
    )
    assert results[0].status == "updated" and results[0].reason == "applied"
    assert _name(db_session_factory, tid, UID_A) == "王小明"


def test_already_named_is_skipped(db_session_factory):
    _seed(db_session_factory, customers=[(UID_A, "已有名字")])
    client = FakeProfileClient({})  # 不應被呼叫
    results = backfill_customer_display_names(
        session_factory=db_session_factory,
        profile_client=client,
        apply=True,
        sleep_seconds=0,
    )
    assert results == []  # 候選查詢就排除了已命名者
    assert client.calls == []


def test_profile_error_is_non_fatal(db_session_factory):
    tid = _seed(db_session_factory, customers=[(UID_A, None), (UID_B, None)])
    client = FakeProfileClient({
        UID_A: RuntimeError("404 not friend"),
        UID_B: LineUserProfile(UID_B, "阿美"),
    })
    results = backfill_customer_display_names(
        session_factory=db_session_factory,
        profile_client=client,
        apply=True,
        sleep_seconds=0,
    )
    by_uid = {r.customer_id: r for r in results}
    statuses = sorted(r.status for r in results)
    assert statuses == ["failed", "updated"]
    # 失敗者留 None、成功者寫入
    assert _name(db_session_factory, tid, UID_A) is None
    assert _name(db_session_factory, tid, UID_B) == "阿美"
    assert len(by_uid) == 2


def test_missing_display_name_skipped(db_session_factory):
    tid = _seed(db_session_factory, customers=[(UID_A, None)])
    client = FakeProfileClient({UID_A: LineUserProfile(UID_A, None)})
    results = backfill_customer_display_names(
        session_factory=db_session_factory,
        profile_client=client,
        apply=True,
        sleep_seconds=0,
    )
    assert results[0].status == "skipped" and results[0].reason == "display_name_missing"
    assert _name(db_session_factory, tid, UID_A) is None


def test_no_line_config_skipped(db_session_factory):
    _seed(db_session_factory, with_config=False, customers=[(UID_A, None)])
    client = FakeProfileClient({})
    results = backfill_customer_display_names(
        session_factory=db_session_factory,
        profile_client=client,
        apply=True,
        sleep_seconds=0,
    )
    assert results[0].status == "skipped" and results[0].reason == "no_line_config"
    assert client.calls == []


def test_main_writes_report(db_session_factory):
    _seed(db_session_factory, customers=[(UID_A, None)])
    client = FakeProfileClient({UID_A: LineUserProfile(UID_A, "王小明")})
    out = StringIO()
    rc = main(
        ["--apply", "--sleep-ms", "0"],
        session_factory=db_session_factory,
        profile_client=client,
        stdout=out,
    )
    text = out.getvalue()
    assert rc == 0
    assert "mode=apply" in text
    assert "updated=1" in text
