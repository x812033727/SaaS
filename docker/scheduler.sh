#!/usr/bin/env bash
# ops 排程器（單一實例，勿多副本）：以 supercronic 執行 docker/crontab。
# 排程內容（每分鐘提醒/通知、每時活動、每日生日/喚回、帳務巡檢…）見
# docker/crontab；時區一律 UTC（compose 服務已釘 TZ=UTC）。
#
# 心跳：crontab 內每分鐘 touch /tmp/sched/heartbeat，compose healthcheck
# 以其新鮮度（<180s）判活；啟動先寫一次避免 start_period 內誤判。
set -euo pipefail

# 路徑固定 /tmp/sched（crontab 的 flock/heartbeat 與 compose healthcheck
# 都寫死同一路徑，不提供覆寫以免三處分歧）。
mkdir -p /tmp/sched
touch /tmp/sched/heartbeat

echo "[scheduler] 啟動 supercronic（crontab=/app/docker/crontab，UTC）"
exec supercronic -json /app/docker/crontab
