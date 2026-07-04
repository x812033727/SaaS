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


def test_scheduler_writes_heartbeat_continuously():
    """supercronic 版排程器的心跳鏈：啟動先寫一次（start_period 防誤判），
    之後由 crontab 每分鐘 touch——compose healthcheck 判新鮮度 <180s。
    （原 bash while-loop 版每輪 touch 的斷言隨 supercronic 改版更新。）"""
    src = _SCHEDULER.read_text(encoding="utf-8")
    assert "touch /tmp/sched/heartbeat" in src, (
        "scheduler.sh 啟動必須先寫一次心跳檔（避免 start_period 內被判 unhealthy）"
    )
    assert "supercronic" in src, "scheduler.sh 必須以 supercronic 執行 crontab"

    crontab = (_ROOT / "docker" / "crontab").read_text(encoding="utf-8")
    heartbeat_lines = [
        line for line in crontab.splitlines()
        if not line.strip().startswith("#")
        and "touch /tmp/sched/heartbeat" in line
    ]
    assert heartbeat_lines, "crontab 必須有心跳排程項"
    # 心跳必須是每分鐘（healthcheck 判 <180s 新鮮度）
    assert any(l.split()[:5] == ["*", "*", "*", "*", "*"] for l in heartbeat_lines), (
        "心跳排程必須每分鐘執行（compose healthcheck 判 180 秒新鮮度）"
    )
