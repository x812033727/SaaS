#!/usr/bin/env bash
# 異地備份（B4）：把每日 pg_dump（/opt/saas/backups，db-backup 服務產出、保留 14 天）
# rclone 到雲端 remote。主機 cron 執行（非容器）。
#
# 安裝（主機，root）：
#   install -m 755 /opt/saas/docker/offsite-backup.sh /usr/local/bin/saas-offsite-backup.sh
#   crontab -l | { cat; echo "30 5 * * * /usr/local/bin/saas-offsite-backup.sh >> /var/log/saas-offsite-backup.log 2>&1"; } | crontab -
#
# 需求：rclone 已設定 remote（預設 pcloud:，可用 SAAS_BACKUP_REMOTE 覆寫）。
# 驗證：每週手動 `rclone check /opt/saas/backups <remote>` + 至少演練一次 restore
#（見 docs/BACKUP.md）。
set -euo pipefail

REMOTE="${SAAS_BACKUP_REMOTE:-pcloud:saas-backups}"
SRC="/opt/saas/backups"

if ! command -v rclone >/dev/null; then
  echo "$(date -u +%FT%TZ) [offsite] rclone 未安裝，略過" >&2
  exit 1
fi
if [ ! -d "$SRC" ]; then
  echo "$(date -u +%FT%TZ) [offsite] 備份目錄不存在：$SRC" >&2
  exit 1
fi

echo "=== $(date -u +%FT%TZ) [offsite] $SRC -> $REMOTE ==="
# copy（不刪遠端舊檔）：遠端自然保留比本地 14 天更長的歷史。
rclone copy "$SRC" "$REMOTE" --max-age 72h --transfers 2 --checkers 4
echo "=== $(date -u +%FT%TZ) [offsite] 完成 ==="
