#!/usr/bin/env bash
# 極簡 ops 排程器（單一實例，勿多副本）：
#  - 每 60s：派送到期的提醒 + 預約異動通知。
#  - 每小時:00：跑排程/限時行銷活動。
#  - 每日 09:00：生日活動；每日 14:00：沉睡客戶喚回。
# 以 /tmp marker 確保每日任務一天只觸發一次（即使該分鐘內迴圈多跑）。
# 正式環境若需更精準排程，可改用 cron / supercronic 取代本迴圈。
set -uo pipefail

MARKER_DIR="${SCHED_MARKER_DIR:-/tmp/sched}"
mkdir -p "$MARKER_DIR"

# 心跳檔：每輪迴圈結束更新一次；容器 healthcheck 以其新鮮度判斷排程迴圈是否還活著
# （image 的預設 HTTP /healthz 探針只適用 web 容器，本排程容器沒有 HTTP server，
# 會被誤判為 unhealthy）。
HEARTBEAT="$MARKER_DIR/heartbeat"

run() { echo "[scheduler] $(date -u +%FT%TZ) run: $*"; "$@" || echo "[scheduler] WARN: $* 失敗（已隔離，繼續）"; }

ran_today() {  # $1=job name → 0 if already ran today
  local f="$MARKER_DIR/$1-$(date -u +%F)"
  [[ -f "$f" ]]
}
mark_today() { touch "$MARKER_DIR/$1-$(date -u +%F)"; }

echo "[scheduler] 啟動（每分鐘輪詢；UTC 時間判斷每日任務）"
touch "$HEARTBEAT"  # 啟動即先寫一次，避免 start_period 內被判 unhealthy
while true; do
  HHMM="$(date -u +%H%M)"
  MM="$(date -u +%M)"

  # 每分鐘：到期提醒 + 異動通知（--apply 真正派送；受推播月配額閘門保護）
  run python -m saas_mvp.ops.send_due_reminders --apply
  run python -m saas_mvp.ops.send_due_notifications --apply

  # 每小時整點：排程/限時活動
  if [[ "$MM" == "00" ]] && ! ran_today "scheduled-$HHMM"; then
    run python -m saas_mvp.ops.run_scheduled_campaigns --apply
    mark_today "scheduled-$HHMM"
  fi

  # 每日 09:00 UTC：生日活動
  if [[ "$HHMM" == "0900" ]] && ! ran_today birthday; then
    run python -m saas_mvp.ops.run_birthday_campaigns --apply
    mark_today birthday
  fi

  # 每日 14:00 UTC：沉睡客戶喚回
  if [[ "$HHMM" == "1400" ]] && ! ran_today reactivation; then
    run python -m saas_mvp.ops.run_reactivation --apply
    mark_today reactivation
  fi

  touch "$HEARTBEAT"  # 一輪完成 → 更新心跳
  sleep 60
done
