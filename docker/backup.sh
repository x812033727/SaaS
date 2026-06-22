#!/usr/bin/env bash
# 單次資料庫備份 + 保留輪替（在 postgres:16-alpine 容器內跑，pg_dump 版本與 db 服務精準相符）。
#
# 設計：
#  - pg_dump custom format（-Fc）：已內建壓縮、可用 pg_restore --clean --if-exists 選擇性還原。
#  - 先寫 .tmp 再 rename → atomic：避免 pg_dump 中途失敗留下半截檔被誤當成有效備份。
#  - pg_dump 失敗即非零退出，且**不**執行輪替刪除——保住現有最後一份好備份。
#  - 日誌只印目標 db 名，不印 PGPASSWORD / DSN（比照 db.py 既有資安慣例）。
#
# 可手動執行：docker compose run --rm db-backup /usr/local/bin/backup.sh
set -euo pipefail

PGHOST="${PGHOST:-db}"
PGUSER="${PGUSER:-saas}"
PGDATABASE="${PGDATABASE:-saas}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION="${BACKUP_RETENTION_DAYS:-14}"
export PGHOST PGUSER PGDATABASE
export PGPASSWORD="${PGPASSWORD:-}"

log() { echo "[backup] $(date -u +%FT%TZ) $*"; }

mkdir -p "$BACKUP_DIR"

ts="$(date -u +%Y%m%d-%H%M%S)"
target="$BACKUP_DIR/saas-$ts.dump"
tmp="$target.tmp"

log "開始 pg_dump（db=$PGDATABASE host=$PGHOST）→ $target"
if ! pg_dump -Fc -f "$tmp"; then
  log "ERROR: pg_dump 失敗，保留既有備份不輪替"
  rm -f "$tmp"
  exit 1
fi
mv "$tmp" "$target"
size="$(wc -c <"$target" | tr -d ' ')"
log "備份完成：$(basename "$target")（${size} bytes）"

# 保留輪替：刪除 RETENTION 天前的備份（只刪我們自己的命名格式，避免誤刪外來檔）。
if [[ "$RETENTION" -gt 0 ]]; then
  deleted="$(find "$BACKUP_DIR" -maxdepth 1 -name 'saas-*.dump' -mtime "+$RETENTION" -print -delete | wc -l | tr -d ' ')"
  log "輪替：刪除 $deleted 份超過 ${RETENTION} 天的備份"
fi

log "完成"
