"""行內 (in-process) SSE 事件廣播器 — 後台即時通知。

對標 vibeaico「SSE 即時通知：新預約 / 取消 / 新訊息即時推送到後台」。

設計：每租戶一組訂閱 ``asyncio.Queue``。SSE 端點（async）``subscribe`` 取得 queue；
mutating 流程（多在 threadpool 的 sync 函式內）以 ``publish`` 投遞事件——透過
``loop.call_soon_threadsafe`` 跨執行緒安全地放入各 queue。

限制：行內廣播僅及於「同一個 worker 行程」。多 worker 部署若要跨行程廣播，
需改以 Redis pub/sub 為後端（saas-redis 已具備），此處先以單行程為準（測試／
單 worker 即時生效）。publish 一律 best-effort，絕不向呼叫端拋例外。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict


class EventBroker:
    def __init__(self) -> None:
        self._subs: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None

    async def subscribe(self, tenant_id: int) -> asyncio.Queue:
        """SSE 端點呼叫：記錄事件迴圈並登記一個 queue。"""
        self._loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs[tenant_id].add(q)
        return q

    def unsubscribe(self, tenant_id: int, q: asyncio.Queue) -> None:
        subs = self._subs.get(tenant_id)
        if subs is not None:
            subs.discard(q)
            if not subs:
                self._subs.pop(tenant_id, None)

    def subscriber_count(self, tenant_id: int) -> int:
        return len(self._subs.get(tenant_id, ()))

    def publish(self, tenant_id: int, event: dict) -> None:
        """投遞事件給該租戶所有訂閱者（best-effort，不拋例外）。

        可由任意執行緒呼叫（mutating 流程多在 threadpool）。
        """
        subs = list(self._subs.get(tenant_id, ()))
        if not subs:
            return
        loop = self._loop
        for q in subs:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._safe_put, q, event)
            else:
                self._safe_put(q, event)

    @staticmethod
    def _safe_put(q: asyncio.Queue, event: dict) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # 慢消費者：丟棄最舊一筆後重試，避免無限堆積。
            try:
                q.get_nowait()
                q.put_nowait(event)
            except Exception:  # noqa: BLE001
                pass


# 模組級單例（與 push_quota / features 同風格）。
broker = EventBroker()


def publish_event(tenant_id: int, event_type: str, **data) -> None:
    """便捷包裝：組裝 {type, ...data} 並廣播。"""
    payload = {"type": event_type}
    payload.update(data)
    broker.publish(tenant_id, payload)
