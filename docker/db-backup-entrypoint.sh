#!/usr/bin/env bash
# 每日資料庫備份排程器（單一實例，勿多副本）——比照 docker/scheduler.sh 的極簡迴圈：
#  - 每分鐘輪詢；當 UTC 時間 == BACKUP_TIME（HHMM，預設 0300）且當日尚未跑過 → 呼叫 backup.sh。
#  - 以 /tmp marker 確保每日只備份一次（即使該分鐘內迴圈多跑）。
#  - 維持 heartbeat 檔供容器 healthcheck 判斷迴圈是否還活著（postgres image 無 HTTP 探針）。
# 正式環境若需更精準排程，可改用 cron / supercronic 取代本迴圈。
set -uo pipefail

BACKUP_TIME="${BACKUP_TIME:-0300}"
MARKER_DIR="${BACKUP_MARKER_DIR:-/tmp/dbbackup}"
mkdir -p "$MARKER_DIR"
HEARTBEAT="$MARKER_DIR/heartbeat"

log() { echo "[db-backup] $(date -u +%FT%TZ) $*"; }

ran_today() {  # 0 if today's backup already done
  [[ -f "$MARKER_DIR/done-$(date -u +%F)" ]]
}
mark_today() { touch "$MARKER_DIR/done-$(date -u +%F)"; }

log "啟動（每分鐘輪詢；UTC ${BACKUP_TIME} 觸發每日備份，保留 ${BACKUP_RETENTION_DAYS:-14} 天）"
touch "$HEARTBEAT"  # 啟動即先寫一次，避免 start_period 內被判 unhealthy
while true; do
  if [[ "$(date -u +%H%M)" == "$BACKUP_TIME" ]] && ! ran_today; then
    if /usr/local/bin/backup.sh; then
      mark_today
    else
      log "WARN: 本次備份失敗（不標記，下一輪會重試）"
    fi
  fi
  touch "$HEARTBEAT"  # 一輪完成 → 更新心跳
  sleep 60
done
