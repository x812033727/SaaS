"""R2-4 測試 — D1 意圖擴充 / D2 tool loop / D3 歷史注入。"""

from __future__ import annotations

import datetime
import os
import types
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.ai.agent import (  # noqa: E402
    AnthropicAgent,
    StubAgent,
    ToolBelt,
)
from saas_mvp.db import Base  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import ai_conversation as conv_svc  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_SLOT_START = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _seed(db) -> dict:
    t = Tenant(name=f"aiu_{uuid.uuid4().hex[:8]}", plan="pro")
    db.add(t)
    db.flush()
    slot = BookingSlot(
        tenant_id=t.id, slot_start=_SLOT_START, max_capacity=4,
    )
    db.add(slot)
    db.flush()
    out = {"tenant_id": t.id, "slot_id": slot.id}
    db.commit()
    return out


def _book(db, tid, slot_id, user) -> int:
    return booking_svc.book_slot(
        db, tenant_id=tid, slot_id=slot_id, party_size=1, line_user_id=user
    ).id


# ── D1 意圖(StubAgent 規則 + 驅動分流)────────────────────────────────────────

class TestIntentStub:
    def test_stub_detects_intents(self):
        agent = StubAgent()
        assert agent.converse("我要取消 #7", {}, "").intent == "cancel"
        assert agent.converse("取消 7 號", {}, "").reservation_id == 7
        assert agent.converse("想改期 #3", {}, "").intent == "reschedule"
        assert agent.converse("查詢我的預約", {}, "").intent == "query"
        assert agent.converse("我要剪髮", {}, "・id=1 剪髮").intent == "book"


class TestManageIntents:
    def test_cancel_with_valid_id_offers_confirm_button(self, db):
        s = _seed(db)
        rid = _book(db, s["tenant_id"], s["slot_id"], "Uai1")
        reply, buttons = conv_svc.handle_free_text(
            db, s["tenant_id"], "Uai1", f"我要取消 #{rid}"
        )
        assert f"#{rid}" in reply
        assert buttons == [(f"確認取消 #{rid}", f"action=cancel&reservation_id={rid}")]

    def test_cancel_others_reservation_not_confirmed(self, db):
        """幻覺/他人編號:不出該編號確認按鈕,改列自己的。"""
        s = _seed(db)
        other_rid = _book(db, s["tenant_id"], s["slot_id"], "Uother")
        my_rid = _book(db, s["tenant_id"], s["slot_id"], "Umine")
        reply, buttons = conv_svc.handle_free_text(
            db, s["tenant_id"], "Umine", f"取消 #{other_rid}"
        )
        datas = [d for _l, d in (buttons or [])]
        assert all(f"reservation_id={other_rid}" not in d for d in datas)
        assert any(f"reservation_id={my_rid}" in d for d in datas)

    def test_cancel_single_reservation_auto_target(self, db):
        s = _seed(db)
        rid = _book(db, s["tenant_id"], s["slot_id"], "Usolo")
        reply, buttons = conv_svc.handle_free_text(
            db, s["tenant_id"], "Usolo", "我想取消預約"
        )
        assert f"#{rid}" in reply
        assert buttons[0][1] == f"action=cancel&reservation_id={rid}"

    def test_reschedule_intent_buttons(self, db):
        s = _seed(db)
        rid = _book(db, s["tenant_id"], s["slot_id"], "Ures")
        reply, buttons = conv_svc.handle_free_text(
            db, s["tenant_id"], "Ures", f"想改期 #{rid}"
        )
        assert buttons[0][1] == f"action=reschedule&reservation_id={rid}"

    def test_query_lists_with_manage_buttons(self, db):
        s = _seed(db)
        rid = _book(db, s["tenant_id"], s["slot_id"], "Uq")
        reply, buttons = conv_svc.handle_free_text(
            db, s["tenant_id"], "Uq", "查詢我的預約"
        )
        assert f"#{rid}" in reply
        datas = [d for _l, d in buttons]
        assert f"action=reschedule&reservation_id={rid}" in datas
        assert f"action=cancel&reservation_id={rid}" in datas

    def test_cancel_no_reservations_message(self, db):
        s = _seed(db)
        reply, buttons = conv_svc.handle_free_text(
            db, s["tenant_id"], "Unobody", "我要取消預約"
        )
        assert "沒有可以取消" in reply


# ── D2 tool loop(fake anthropic client)──────────────────────────────────────

def _block(type_, **kw):
    return types.SimpleNamespace(type=type_, **kw)


class _FakeClient:
    """按序回應:tool_use(list_services) → propose_action。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

        class _Messages:
            def __init__(_s, outer):
                _s.outer = outer

            def create(_s, **kw):
                _s.outer.calls.append(kw)
                return _s.outer.script.pop(0)

        self.messages = _Messages(self)


class TestToolLoop:
    def _agent(self, script, monkeypatch):
        from saas_mvp.config import settings

        monkeypatch.setattr(settings, "minimax_api_key", "test-key")
        return AnthropicAgent(client_factory=lambda: _FakeClient(script) if isinstance(script, list) else script)

    def test_query_tool_then_propose(self, monkeypatch):
        fake = _FakeClient([
            types.SimpleNamespace(content=[
                _block("tool_use", id="t1", name="list_services", input={}),
            ]),
            types.SimpleNamespace(content=[
                _block("tool_use", id="t2", name="propose_action",
                       input={"reply": "好的", "intent": "book", "service_id": 3}),
            ]),
        ])
        from saas_mvp.config import settings

        monkeypatch.setattr(settings, "minimax_api_key", "test-key")
        agent = AnthropicAgent(client_factory=lambda: fake)
        belt = ToolBelt(list_services=lambda: "id=3 剪髮")
        turn = agent.converse("有什麼服務", {}, "ctx", tools=belt)
        assert turn.intent == "book" and turn.service_id == 3
        assert len(fake.calls) == 2
        # 第二輪 messages 應含 tool_result
        second = fake.calls[1]["messages"]
        assert any(
            isinstance(m.get("content"), list)
            and isinstance(m["content"][0], dict)
            and m["content"][0].get("type") == "tool_result"
            for m in second
        )

    def test_final_round_forced_propose(self, monkeypatch):
        """連續查詢 3 輪:末輪 tool_choice 強制 propose_action。"""
        q = types.SimpleNamespace(content=[
            _block("tool_use", id="t", name="available_dates", input={}),
        ])
        final = types.SimpleNamespace(content=[
            _block("tool_use", id="tf", name="propose_action",
                   input={"reply": "ok", "intent": "other"}),
        ])
        fake = _FakeClient([q, q, final])
        from saas_mvp.config import settings

        monkeypatch.setattr(settings, "minimax_api_key", "test-key")
        agent = AnthropicAgent(client_factory=lambda: fake)
        turn = agent.converse("hmm", {}, "ctx", tools=ToolBelt(
            available_dates=lambda: "2030-06-01"
        ))
        assert turn.intent == "other"
        assert len(fake.calls) == 3
        assert fake.calls[2]["tool_choice"] == {"type": "tool", "name": "propose_action"}

    def test_history_merged_and_alternating(self, monkeypatch):
        final = types.SimpleNamespace(content=[
            _block("tool_use", id="tf", name="propose_action",
                   input={"reply": "ok", "intent": "other"}),
        ])
        fake = _FakeClient([final])
        from saas_mvp.config import settings

        monkeypatch.setattr(settings, "minimax_api_key", "test-key")
        agent = AnthropicAgent(client_factory=lambda: fake)
        history = [
            ("user", "你好"),
            ("user", "想約時間"),      # 連續 user → 合併
            ("assistant", "好的!"),
        ]
        agent.converse("明天有空嗎", {}, "ctx", history=history)
        msgs = fake.calls[0]["messages"]
        roles = [m["role"] for m in msgs]
        # 交錯合法(無連續同角色)
        assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))
        assert "你好" in msgs[0]["content"] and "想約時間" in msgs[0]["content"]
        assert "明天有空嗎" in msgs[-1]["content"]


def test_agent_sdk_runner_receives_only_bound_read_tools(monkeypatch):
    from saas_mvp.config import settings

    monkeypatch.setattr(settings, "minimax_api_key", "test-key")
    captured = {}

    def runner(**kwargs):
        captured.update(kwargs)
        return {
            "reply": "可以預約",
            "intent": "book",
            "service_id": 3,
            "date": None,
            "party_size": None,
            "reservation_id": None,
        }

    agent = AnthropicAgent(runner=runner)
    turn = agent.converse(
        "我要剪髮", {}, "ctx", tools=ToolBelt(list_services=lambda: "id=3 剪髮")
    )
    assert turn.intent == "book" and turn.service_id == 3
    assert set(captured["tool_dispatch"]) == {"list_services"}
    assert captured["tool_dispatch"]["list_services"]({}) == "id=3 剪髮"
    assert captured["output_schema"]["additionalProperties"] is False
