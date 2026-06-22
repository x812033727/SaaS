#!/usr/bin/env bash
# 從 pg_dump 備份檔還原資料庫（在 postgres:16-alpine 容器內跑）。
#
# 用法：
#   docker compose run --rm -e FORCE=1 db-backup \
#     /usr/local/bin/restore.sh /backups/saas-YYYYmmdd-HHMMSS.dump
#
# 警示：**破壞性**操作——pg_restore --clean --if-exists 會先 DROP 既有物件再重建，
# 等同把目標資料庫覆寫成 dump 當下狀態。為防誤覆寫正式庫：
#   - 有 TTY：互動詢問 yes/no。
#   - 無 TTY（cron / compose run）：必須帶 FORCE=1，否則拒絕執行。
# 日誌只印檔名與目標 db，不印 PGPASSWORD / DSN。
set -euo pipefail

PGHOST="${PGHOST:-db}"
PGUSER="${PGUSER:-saas}"
PGDATABASE="${PGDATABASE:-saas}"
export PGHOST PGUSER PGDATABASE
export PGPASSWORD="${PGPASSWORD:-}"

log() { echo "[restore] $(date -u +%FT%TZ) $*"; }

dump="${1:-}"
if [[ -z "$dump" ]]; then
  echo "用法：restore.sh <dump 檔路徑>（例：/backups/saas-20260622-030000.dump）" >&2
  exit 2
fi
if [[ ! -f "$dump" ]]; then
  echo "[restore] ERROR: 找不到備份檔：$dump" >&2
  exit 2
fi

confirm() {
  if [[ "${FORCE:-}" == "1" ]]; then
    return 0
  fi
  if [[ -t 0 ]]; then
    read -r -p "將以 $(basename "$dump") 覆寫資料庫 '$PGDATABASE'（既有資料會被 DROP）。確定？輸入 yes：" ans
    [[ "$ans" == "yes" ]] && return 0
    log "已取消"
    exit 1
  fi
  echo "[restore] ERROR: 非互動環境需帶 FORCE=1 才會還原（防誤覆寫）" >&2
  exit 1
}

confirm
log "開始還原：$(basename "$dump") → db=$PGDATABASE host=$PGHOST"
# --clean --if-exists：先 DROP 既有物件（不存在不報錯）；--no-owner：忽略 dump 內的擁有者，
# 用當前連線角色重建，避免跨環境角色名不一致而失敗。
pg_restore --clean --if-exists --no-owner -d "$PGDATABASE" "$dump"
log "還原完成"
