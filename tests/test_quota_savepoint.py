"""併發插入衝突時,額度計量器不得回捲呼叫端外層交易(savepoint 隔離)。

背景(重掃 workflow 確認的 live pre-existing 缺陷):
  ai_quota._get_or_create_locked / push_quota._get_or_create_push_usage_locked
  在月度計量列不存在時 INSERT count=0;若併發下另一寫入者已插入同 (tenant,period),
  flush 觸發 IntegrityError。**原本**的 except 分支呼叫 db.rollback() —— 那會把
  呼叫端未提交的外層交易一起回捲:
    * ai_quota:consume_ai_in_txn 與對話狀態同交易 → 對話狀態被清掉
    * push_quota:consume_push_in_txn 與「標 sent」同交易 → 重複推播 + 重扣額度
  修法:改用 db.begin_nested() SAVEPOINT,只回捲失敗的插入,外層交易完好。

此路徑(跨連線併發衝突)在單連線 in-memory SQLite 無法自然觸發,故以注入式
white-box 測試:強制 flush 拋 IntegrityError,驗證 (a) 不呼叫外層 db.rollback、
(b) 改讀既有列回傳、(c) 外層未提交工作存活。另加正常路徑功能測試。
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.db import Base  # noqa: E402
from saas_mvp.models.ai_usage import AiUsage  # noqa: E402
from saas_mvp.models.push_usage import PushUsage  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.services import ai_quota, push_quota  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
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


@pytest.fixture()
def tenant_id(db) -> int:
    t = Tenant(name=f"q_{uuid.uuid4().hex[:8]}", plan="pro")
    db.add(t)
    db.commit()
    return t.id


def _calls_attr(fn, obj_name: str, method: str) -> bool:
    """AST 判斷 fn 內是否有 `<obj_name>.<method>(...)` 呼叫(不受註解/字串影響)。"""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == method
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == obj_name
        ):
            return True
    return False


class _FakeResult:
    def __init__(self, val):
        self._val = val

    def scalar_one_or_none(self):
        return self._val

    def scalar_one(self):
        return self._val


def _force_conflict(db, monkeypatch, winner):
    """讓下一次 _get_or_create 進入插入分支、flush 拋 IntegrityError、再讀到 winner。

    回傳一個 dict,called['outer_rollback'] 記錄外層 db.rollback 是否被呼叫。
    """
    spy = {"outer_rollback": False}
    real_rollback = db.rollback
    monkeypatch.setattr(
        db, "rollback",
        lambda: (spy.__setitem__("outer_rollback", True), real_rollback())[1],
    )
    calls = {"n": 0}

    def fake_execute(stmt, *a, **k):
        calls["n"] += 1
        # 第 1 次 FOR UPDATE select → None(進插入分支);之後 → winner(衝突後重讀)
        return _FakeResult(None if calls["n"] == 1 else winner)

    def fake_flush(*a, **k):
        raise IntegrityError("dup", None, Exception("unique(tenant,period)"))

    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(db, "flush", fake_flush)
    return spy


# ── AI 計量 ───────────────────────────────────────────────────────────────────

class TestAiQuotaSavepoint:
    def test_normal_path_preserves_outer_and_increments(self, db, tenant_id):
        # 呼叫端未提交的外層工作
        marker = User(email="ai-outer@x.tw", hashed_password="x",
                      tenant_id=tenant_id, role="staff")
        db.add(marker)
        db.flush()
        ai_quota.consume_ai_in_txn(db, tenant_id)
        ai_quota.consume_ai_in_txn(db, tenant_id)  # 冪等遞增
        assert ai_quota.get_usage(db, tenant_id) == 2
        assert db.get(User, marker.id) is not None  # 外層工作仍在(未被回捲)
        db.commit()
        assert ai_quota.get_usage(db, tenant_id) == 2

    def test_conflict_branch_does_not_rollback_outer_txn(self, db, tenant_id, monkeypatch):
        period = ai_quota._period_now()
        winner = AiUsage(tenant_id=tenant_id, period=period, count=9)
        marker = User(email="ai-race@x.tw", hashed_password="x",
                      tenant_id=tenant_id, role="staff")
        db.add(marker)
        db.flush()  # 真實 flush,開啟外層交易;marker 未提交

        spy = _force_conflict(db, monkeypatch, winner)
        row = ai_quota._get_or_create_locked(db, tenant_id, period)

        assert row is winner  # 衝突後改讀既有列
        assert spy["outer_rollback"] is False  # 關鍵:未回捲外層交易
        monkeypatch.undo()
        assert db.get(User, marker.id) is not None  # 外層未提交工作存活

    def test_helper_uses_savepoint_not_outer_rollback(self):
        assert not _calls_attr(ai_quota._get_or_create_locked, "db", "rollback"), \
            "conflict 分支不得呼叫外層 db.rollback()"
        assert _calls_attr(ai_quota._get_or_create_locked, "db", "begin_nested"), \
            "須以 SAVEPOINT 隔離插入衝突"


# ── 推播計量 ──────────────────────────────────────────────────────────────────

class TestPushQuotaSavepoint:
    def test_normal_path_preserves_outer_and_increments(self, db, tenant_id):
        marker = User(email="push-outer@x.tw", hashed_password="x",
                      tenant_id=tenant_id, role="staff")
        db.add(marker)
        db.flush()
        push_quota.consume_push_in_txn(db, tenant_id, n=1)
        push_quota.consume_push_in_txn(db, tenant_id, n=2)
        assert push_quota.get_usage(db, tenant_id) == 3
        assert db.get(User, marker.id) is not None
        db.commit()
        assert push_quota.get_usage(db, tenant_id) == 3

    def test_conflict_branch_does_not_rollback_outer_txn(self, db, tenant_id, monkeypatch):
        period = push_quota._period_now()
        winner = PushUsage(tenant_id=tenant_id, period=period, count=42)
        marker = User(email="push-race@x.tw", hashed_password="x",
                      tenant_id=tenant_id, role="staff")
        db.add(marker)
        db.flush()

        spy = _force_conflict(db, monkeypatch, winner)
        row = push_quota._get_or_create_push_usage_locked(db, tenant_id, period)

        assert row is winner
        assert spy["outer_rollback"] is False
        monkeypatch.undo()
        assert db.get(User, marker.id) is not None

    def test_helper_uses_savepoint_not_outer_rollback(self):
        fn = push_quota._get_or_create_push_usage_locked
        assert not _calls_attr(fn, "db", "rollback")
        assert _calls_attr(fn, "db", "begin_nested")
