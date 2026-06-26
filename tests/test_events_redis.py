"""SSE 事件廣播器 — redis pub/sub 跨 worker 後端測試（對標 vibeaico 多 worker）。

不開實際 SSE 串流（避免 TestClient 卡死）；以 fake redis 驗證
publish→redis / listener→deliver_local 的接線與 fallback。
"""

from __future__ import annotations

import asyncio
import json
import os

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.services import events as ev  # noqa: E402
from saas_mvp.services.events import EventBroker, REDIS_CHANNEL  # noqa: E402


def test_publish_uses_redis_and_skips_local_double_send():
    """啟用 redis 時：publish 只 PUBLISH 到頻道，不在本地重複投遞（避免雙送）。"""
    calls = []

    class FakeRedis:
        def publish(self, ch, data):
            calls.append((ch, data))

    b = EventBroker()
    b.set_redis(FakeRedis())

    async def scenario():
        q = await b.subscribe(1)
        b.publish(1, {"type": "x"})
        # 本地佇列不應直接收到（事件改由 listener 收 redis 後 deliver_local）
        try:
            await asyncio.wait_for(q.get(), 0.1)
            got_local = True
        except asyncio.TimeoutError:
            got_local = False
        return got_local

    assert asyncio.run(scenario()) is False
    assert len(calls) == 1
    ch, data = calls[0]
    assert ch == REDIS_CHANNEL
    payload = json.loads(data)
    assert payload["t"] == 1 and payload["e"]["type"] == "x"


def test_redis_roundtrip_delivers_to_local_subscriber():
    """publish → (模擬 listener) deliver_local → 本 worker SSE 佇列收到。"""
    b = EventBroker()

    async def scenario():
        q = await b.subscribe(7)

        class LoopbackRedis:
            # 模擬「另一/本 worker 的 listener 收到頻道訊息後 deliver_local」
            def publish(self, ch, data):
                payload = json.loads(data)
                b.deliver_local(payload["t"], payload["e"])

        b.set_redis(LoopbackRedis())
        b.publish(7, {"type": "booking_new", "reservation_id": 5})
        return await asyncio.wait_for(q.get(), 1)

    ev_out = asyncio.run(scenario())
    assert ev_out["type"] == "booking_new" and ev_out["reservation_id"] == 5


def test_publish_redis_failure_falls_back_to_local():
    """redis publish 失敗 → 退回本地投遞（至少同 worker 仍即時）。"""
    b = EventBroker()

    class BoomRedis:
        def publish(self, *a):
            raise RuntimeError("redis down")

    async def scenario():
        q = await b.subscribe(3)
        b.set_redis(BoomRedis())
        b.publish(3, {"type": "y"})
        return await asyncio.wait_for(q.get(), 1)

    assert asyncio.run(scenario())["type"] == "y"


def test_start_fanout_memory_returns_none():
    """events_backend 預設 memory → 不啟動 listener，回 None（行為不變）。"""

    async def scenario():
        return await ev.start_redis_fanout(EventBroker())

    assert asyncio.run(scenario()) is None


def test_memory_mode_still_local_broadcast():
    """未設 redis（預設）→ publish 直接本地投遞（回歸保護）。"""
    b = EventBroker()

    async def scenario():
        q = await b.subscribe(9)
        b.publish(9, {"type": "line_message"})
        return await asyncio.wait_for(q.get(), 1)

    assert asyncio.run(scenario())["type"] == "line_message"


def test_redis_listener_decodes_message_and_delivers():
    """_redis_listener：收到頻道 message → 解碼 → deliver_local 到本 worker 佇列。"""
    b = EventBroker()

    class FakePubSub:
        def __init__(self):
            self._msgs = [
                {"type": "subscribe"},  # 訂閱確認，應略過
                {"type": "message",
                 "data": json.dumps({"t": 4, "e": {"type": "booking_cancel"}}).encode()},
            ]

        async def subscribe(self, ch):
            pass

        async def listen(self):
            for m in self._msgs:
                yield m
            raise asyncio.CancelledError  # 模擬 task 被取消結束

        async def unsubscribe(self, ch):
            pass

        async def aclose(self):
            pass

    class FakeAsyncClient:
        def pubsub(self):
            return FakePubSub()

    async def scenario():
        q = await b.subscribe(4)
        task = asyncio.create_task(ev._redis_listener(b, FakeAsyncClient()))
        out = await asyncio.wait_for(q.get(), 1)
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001
            pass
        return out

    assert asyncio.run(scenario())["type"] == "booking_cancel"
