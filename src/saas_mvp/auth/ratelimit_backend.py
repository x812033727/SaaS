"""可插拔速率限制後端：in-memory（預設）+ Redis（跨 worker 共享）。

設計動機（多 worker / 橫向擴展）
--------------------------------
``SlidingWindowRateLimiter`` 原本以 in-process ``OrderedDict`` + ``threading.Lock``
實作滑動視窗，只在「單一 worker process」內有效；跑 ``uvicorn --workers N`` 或
gunicorn 多 worker 時，每個 process 各有一份計數，限流形同被放大 N 倍。

本模組把「是否放行」的判定抽成 :class:`RateLimitBackend`，提供兩個實作：

* :class:`MemoryBackend` —— 包住既有的有界時間戳日誌 + 鎖邏輯，行為與舊版完全一致，
  作為 dev/test/單 worker 的預設。
* :class:`RedisBackend` —— 以 Redis sorted set + Lua script（EVAL）做**原子**滑動視窗，
  跨 process / 跨機共享同一份計數，消除 multi-worker TOCTOU。

注意：本模組可在「未安裝 ``redis`` 套件」時正常 import——``import redis`` 只在
:class:`RedisBackend` 建構式內（lazy）發生。
"""

import secrets
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Callable, List


# ── 有界時間戳日誌（純 stdlib，無外部依賴）──────────────────────────────────
# NOTE: 此類別同時 re-export 給 ratelimit.py（保留既有公開名稱與測試相容）。

class _BoundedTimestampLog:
    """以 OrderedDict 實作的有界 key → timestamps 映射。

    超出 maxsize 時 FIFO evict 最舊 key，防止 DoS 導致無限記憶體增長。
    不使用 defaultdict，改提供 get/set 明確介面。
    """

    def __init__(self, maxsize: int = 10_000) -> None:
        self._maxsize = maxsize
        self._data: "OrderedDict[str, List[float]]" = OrderedDict()

    def get(self, key: str) -> "List[float]":
        """回傳 key 對應的時間戳列表；key 不存在回空列表。"""
        return self._data.get(key, [])

    def set(self, key: str, value: "List[float]") -> None:
        """設定 key 的值；若 key 已存在則移至末尾（LRU 更新）；
        若 key 不存在且容量已滿，則 FIFO evict 最舊 key。"""
        if key in self._data:
            self._data.move_to_end(key)
        elif len(self._data) >= self._maxsize:
            self._data.popitem(last=False)  # 刪最舊
        self._data[key] = value

    def clear(self) -> None:
        """清除所有 entry（測試用，相容 defaultdict.clear()）。"""
        self._data.clear()


# ── 後端抽象 ──────────────────────────────────────────────────────────────────

class RateLimitBackend(ABC):
    """滑動視窗限流後端介面。

    ``allow`` 回傳布林（不拋例外）：限流器自己負責把 ``False`` 轉成 429。
    這讓後端可被獨立替換 / 測試，且 in-memory 與 Redis 走完全相同的判定語意。

    ``name`` 為**實際生效**的後端識別（"memory" / "redis"），供 /healthz 回報，
    讓運維能分辨「設定 redis 但降級為 memory」與「真的在跑 redis」。
    """

    name: str = "unknown"

    @abstractmethod
    def allow(self, key: str, max_calls: int, window_seconds: int) -> bool:
        """嘗試記錄一次對 ``key`` 的呼叫。

        若在 ``window_seconds`` 視窗內的呼叫數 < ``max_calls``，則**原子地**
        記錄本次呼叫並回傳 ``True``（放行）；否則不記錄並回傳 ``False``（超限）。
        """
        raise NotImplementedError


class MemoryBackend(RateLimitBackend):
    """In-process 滑動視窗（既有行為，預設後端）。

    包住既有的 ``_BoundedTimestampLog`` + ``threading.Lock`` 邏輯，以 ``key``
    索引。``clock`` 可注入（預設 ``time.monotonic``），與舊版逐字一致。

    NOTE: 此後端**不跨 process 共享**；多 worker 部署需改用 :class:`RedisBackend`。
    """

    name = "memory"

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        maxsize: int = 10_000,
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._log = _BoundedTimestampLog(maxsize=maxsize)

    def allow(self, key: str, max_calls: int, window_seconds: int) -> bool:
        now = self._clock()
        cutoff = now - window_seconds
        with self._lock:
            calls = [t for t in self._log.get(key) if t > cutoff]
            if len(calls) >= max_calls:
                return False
            calls.append(now)
            self._log.set(key, calls)
            return True


# ── Redis 滑動視窗（原子 Lua script）─────────────────────────────────────────

# KEYS[1] = sorted-set key
# ARGV[1] = now（毫秒整數，wall-clock）
# ARGV[2] = window_ms
# ARGV[3] = max_calls
# ARGV[4] = unique member（避免同毫秒多請求互相覆蓋）
#
# 步驟（全程原子，無跨指令 TOCTOU）：
#   1. ZREMRANGEBYSCORE 清掉視窗外（score <= now-window）的舊紀錄
#   2. ZCARD 取得視窗內現有計數
#   3. 若 count < max → ZADD(now, member) + PEXPIRE(window+1s) 回 1（放行）
#                  否 → 回 0（超限，不寫入）
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max_calls = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count < max_calls then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, window + 1000)
    return 1
end
return 0
"""


class RedisBackend(RateLimitBackend):
    """跨 worker / 跨機共享的滑動視窗，以 Redis sorted set + Lua 原子化。

    時間基準採 **wall-clock**（``time.time()``，毫秒）——monotonic 不可跨 process
    比較，必須用各 worker 一致的牆鐘；視窗較大、毫秒精度足夠，微小時鐘漂移無礙。

    ``import redis`` 在建構式內（lazy），未安裝 ``redis`` 套件時本模組仍可 import；
    只有實際建立 ``RedisBackend`` 才需要該套件。
    """

    name = "redis"

    def __init__(self, redis_client) -> None:
        # lazy import：未裝 redis 時 app 仍可正常 import / 啟動（fallback 到 memory）。
        import redis  # noqa: F401  (確保套件存在；client 由呼叫端傳入)

        self._redis = redis_client
        # register_script 回傳可呼叫的 Script，內部走 EVALSHA + 必要時 fallback EVAL。
        self._script = self._redis.register_script(_SLIDING_WINDOW_LUA)

    def allow(self, key: str, max_calls: int, window_seconds: int) -> bool:
        now_ms = int(time.time() * 1000)
        window_ms = int(window_seconds * 1000)
        member = f"{now_ms}:{secrets.token_hex(8)}"
        result = self._script(
            keys=[key],
            args=[now_ms, window_ms, max_calls, member],
        )
        return bool(result)
