"""簡易 TTL 快取(R6-C1)— in-process、per-worker、執行緒安全。

用途:短 TTL 的**平台級唯讀聚合**(admin 總覽全租戶掃描)。故意不做跨 worker
共享(比照 ratelimit 的 MemoryBackend 哲學):admin 儀表板低併發、TTL 短,
per-worker 各自快取的過期上限就是 TTL,足夠且無序列化風險。

正確性:只快取「無 per-request 參數的平台級聚合」(key=函式名);**不**用於
含 tenant_id 的租戶資料(避免跨租戶洩漏)。TTL=0 視為停用(一律即時計算)。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, TypeVar

T = TypeVar("T")


class TTLCache:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._store: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def get_or_compute(
        self, key: str, ttl_seconds: float, compute: Callable[[], T]
    ) -> T:
        """回快取值;過期/未命中/ttl<=0 則呼叫 compute 並(ttl>0 時)存入。

        compute 在鎖外執行(不在持鎖時打 DB);低併發下偶發重複計算可接受,
        不做 thundering-herd 防護以保持簡單。
        """
        if ttl_seconds <= 0:
            return compute()
        now = self._clock()
        with self._lock:
            hit = self._store.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]  # type: ignore[return-value]
        value = compute()
        with self._lock:
            self._store[key] = (self._clock() + ttl_seconds, value)
        return value

    def invalidate(self, key: str | None = None) -> None:
        """清單一 key(或全部,key=None)。供測試/主動失效用。"""
        with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)


# 平台級 admin 儀表板聚合共用快取(全 worker 各一份)。
admin_dashboard_cache = TTLCache()
