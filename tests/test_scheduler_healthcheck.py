"""守衛排程容器的 healthcheck：必須用迴圈心跳，而非 image 預設的 HTTP /healthz。

排程容器（docker/scheduler.sh）沒有 HTTP server，若沿用 image 的 HTTP /healthz
HEALTHCHECK 會被永久誤判 unhealthy（並可能觸發無謂 restart / 監控誤報）。
正確作法：scheduler.sh 每輪寫心跳檔，compose 覆寫 healthcheck 檢查其新鮮度。
"""

from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_COMPOSE = _ROOT / "docker-compose.yml"
_SCHEDULER = _ROOT / "docker" / "scheduler.sh"


def _scheduler_compose_block() -> str:
    text = _COMPOSE.read_text(encoding="utf-8")
    start = text.index("\n  scheduler:")
    # 到下一個頂層 key（volumes: / networks:）為止
    end = len(text)
    for marker in ("\nvolumes:", "\nnetworks:"):
        idx = text.find(marker, start)
        if idx != -1:
            end = min(end, idx)
    return text[start:end]


def test_scheduler_service_overrides_healthcheck_with_heartbeat():
    block = _scheduler_compose_block()
    assert "healthcheck:" in block, "scheduler 服務必須覆寫 healthcheck"
    assert "heartbeat" in block, "scheduler healthcheck 必須檢查心跳檔，而非 HTTP /healthz"


def test_scheduler_script_writes_heartbeat_in_loop():
    src = _SCHEDULER.read_text(encoding="utf-8")
    assert 'HEARTBEAT=' in src, "scheduler.sh 必須定義 HEARTBEAT 路徑"
    # 迴圈內須更新心跳（while 之後至少一次 touch "$HEARTBEAT"）
    after_loop = src[src.index("while true"):]
    assert 'touch "$HEARTBEAT"' in after_loop, "排程迴圈每輪必須更新心跳檔"
