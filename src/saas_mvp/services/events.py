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
import json
import logging
from collections import defaultdict

_log = logging.getLogger(__name__)

# 跨 worker 廣播用的 redis pub/sub 頻道。
REDIS_CHANNEL = "saas:events"


class EventBroker:
    def __init__(self) -> None:
        self._subs: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None
        # 設定後，publish 改走 redis pub/sub（跨 worker），由各 worker 的
        # listener 收回再 deliver_local；None 時維持行內單 worker 廣播。
        self._redis = None

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

    def set_redis(self, client) -> None:
        """啟用 redis pub/sub 廣播（多 worker）；傳 None 還原為行內廣播。"""
        self._redis = client

    def redis_enabled(self) -> bool:
        """目前是否以 redis pub/sub 廣播（健康探針回報用）。"""
        return self._redis is not None

    def publish(self, tenant_id: int, event: dict) -> None:
        """投遞事件給該租戶所有訂閱者（best-effort，不拋例外）。

        可由任意執行緒呼叫（mutating 流程多在 threadpool）。

        啟用 redis 時：PUBLISH 到共享頻道，由各 worker 的 listener 收回後
        deliver_local（含本 worker，故不在此重複本地投遞，避免雙送）;publish
        失敗則退回本地投遞，至少同 worker 仍即時。
        """
        if self._redis is not None:
            try:
                self._redis.publish(
                    REDIS_CHANNEL,
                    json.dumps({"t": tenant_id, "e": event}, ensure_ascii=False),
                )
                return
            except Exception as exc:  # noqa: BLE001 — redis 故障退回本地，不拋
                _log.warning("event redis publish failed (%s); local-only",
                             type(exc).__name__)
        self.deliver_local(tenant_id, event)

    def deliver_local(self, tenant_id: int, event: dict) -> None:
        """投遞到「本 worker」的 SSE 訂閱佇列（redis listener 收到訊息時呼叫）。"""
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


async def _redis_listener(target: EventBroker, async_client) -> None:
    """訂閱共享頻道，把跨 worker 事件投遞到本 worker 的 SSE 佇列。

    在 app 事件迴圈內以背景 task 執行;cancel 時優雅退出。
    """
    pubsub = async_client.pubsub()
    await pubsub.subscribe(REDIS_CHANNEL)
    try:
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            try:
                raw = msg["data"]
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8")
                payload = json.loads(raw)
                target.deliver_local(payload["t"], payload["e"])
            except Exception as exc:  # noqa: BLE001 — 單筆壞訊息不得中斷 listener
                _log.warning("event listener bad message (%s)", type(exc).__name__)
    except asyncio.CancelledError:
        raise
    finally:
        try:
            await pubsub.unsubscribe(REDIS_CHANNEL)
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass


async def start_redis_fanout(target: EventBroker | None = None):
    """啟用 redis pub/sub 事件廣播並回傳 listener task；不啟用/失敗回 None。

    於 app lifespan 啟動時呼叫。沿用 rate-limit 的「誤設定不崩啟動」哲學：
    events_backend != redis、url 空、未裝 redis、連線失敗 → 記 warning 回 None
    （維持行內單 worker 廣播）。
    """
    from saas_mvp.config import settings

    target = target or broker
    if settings.events_backend != "redis":
        return None
    try:
        if not settings.redis_url:
            raise ValueError("SAAS_REDIS_URL is empty")
        import redis  # noqa: F401
        import redis.asyncio as aioredis

        sync_client = redis.from_url(settings.redis_url)
        sync_client.ping()  # 主動驗證；連不上即 fallback
        async_client = aioredis.from_url(settings.redis_url)
        target.set_redis(sync_client)
        task = asyncio.create_task(_redis_listener(target, async_client))
        _log.info("event backend: redis pub/sub (%s)", settings.redis_url)
        return task
    except Exception as exc:  # noqa: BLE001 — 任何故障都 fallback，不崩啟動
        _log.warning(
            "events_backend=redis requested but unavailable (%s); "
            "falling back to in-process broadcast (NOT shared across workers)",
            type(exc).__name__,
        )
        target.set_redis(None)
        return None
