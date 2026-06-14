"""Task #2 驗收測試 — upsert_line_config 自動回填 line_bot_user_id。

驗收標準（對應整體計畫 #2）：
  - upsert 成功後，若 bot/info 可達 → line_bot_user_id 被填入回傳的 userId
  - bot/info 失敗（拋例外）→ upsert 仍成功、line_bot_user_id 留 None、僅記 warning
  - bot/info 回應缺 userId（None）→ line_bot_user_id 留 None，upsert 仍成功
  - 不提供 bot_info_client（None）→ 行為與舊版完全一致（不呼叫外部 API）
  - 失敗路徑 rollback 後 session 不髒，後續查詢/寫入正常
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "SAAS_LINE_CHANNEL_ENCRYPT_KEY",
    "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=",
)

from saas_mvp.db import Base
from saas_mvp.models import user as _u, note as _n  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku  # noqa: F401
from saas_mvp.models import usage as _us, plan_change_history as _pch  # noqa: F401
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.line_client import StubLineBotInfoClient
from saas_mvp.services import line_config as svc

_BOT_USER_ID = "U" + "a" * 32


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    sess = Session()
    yield sess
    sess.close()


def _tenant(db, name="acme") -> Tenant:
    t = Tenant(name=name, plan="free")
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _orm_cfg(db, tenant_id: int) -> LineChannelConfig:
    return (
        db.query(LineChannelConfig)
        .filter(LineChannelConfig.tenant_id == tenant_id)
        .one()
    )


def test_bot_info_success_fills_user_id(db):
    """bot/info 可達 → line_bot_user_id 被填入。"""
    t = _tenant(db)
    stub = StubLineBotInfoClient(_BOT_USER_ID)

    resp = svc.upsert_line_config(
        db, t.id, channel_secret="s", access_token="tok", bot_info_client=stub
    )

    assert resp["has_access_token"] is True
    assert stub.calls == ["tok"]  # 以明文 access_token 呼叫
    assert _orm_cfg(db, t.id).line_bot_user_id == _BOT_USER_ID


def test_bot_info_failure_does_not_block_upsert(db):
    """bot/info 拋例外 → upsert 仍成功、user_id 留 None、session 不髒。"""
    t = _tenant(db)
    stub = StubLineBotInfoClient(_BOT_USER_ID, raises=True)

    resp = svc.upsert_line_config(
        db, t.id, channel_secret="s", access_token="tok", bot_info_client=stub
    )

    assert resp["tenant_id"] == t.id
    assert _orm_cfg(db, t.id).line_bot_user_id is None
    # rollback 後 session 仍可用：後續寫入正常
    t2 = _tenant(db, name="beta")
    assert t2.id != t.id


def test_bot_info_returns_none_keeps_null(db):
    """bot/info 回應缺 userId → 留 None，不報錯。"""
    t = _tenant(db)
    stub = StubLineBotInfoClient(None)

    svc.upsert_line_config(
        db, t.id, channel_secret="s", access_token="tok", bot_info_client=stub
    )

    assert _orm_cfg(db, t.id).line_bot_user_id is None


def test_no_client_is_backward_compatible(db):
    """不傳 bot_info_client → 不回填、行為同舊版。"""
    t = _tenant(db)

    svc.upsert_line_config(db, t.id, channel_secret="s", access_token="tok")

    assert _orm_cfg(db, t.id).line_bot_user_id is None


def test_duplicate_user_id_does_not_break_upsert(db):
    """同一 bot userId 被他租戶佔用（IntegrityError）→ rollback，upsert 不爆。"""
    t1 = _tenant(db, name="t1")
    t2 = _tenant(db, name="t2")
    stub = StubLineBotInfoClient(_BOT_USER_ID)

    svc.upsert_line_config(
        db, t1.id, channel_secret="s", access_token="tok1", bot_info_client=stub
    )
    assert _orm_cfg(db, t1.id).line_bot_user_id == _BOT_USER_ID

    # t2 拿到同一 userId → unique 衝突，須吞掉、不阻擋設定儲存
    resp = svc.upsert_line_config(
        db, t2.id, channel_secret="s", access_token="tok2", bot_info_client=stub
    )
    assert resp["tenant_id"] == t2.id
    assert _orm_cfg(db, t2.id).line_bot_user_id is None
    # t1 的值不受影響
    assert _orm_cfg(db, t1.id).line_bot_user_id == _BOT_USER_ID
