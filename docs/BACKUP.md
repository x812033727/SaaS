# 備份與還原

## 每日備份（已運行）
- compose `db-backup` 服務每日 `pg_dump`（custom format）到 `/opt/saas/backups/`，保留 14 天。
- 心跳 healthcheck 監控備份新鮮度。

## 異地備份（B4，主機 cron）
本地備份與資料庫同機 — 磁碟壞掉一起沒。用 rclone 送雲端：

```bash
# 一次性安裝（主機已設定 rclone remote，預設 pcloud:saas-backups）
install -m 755 /opt/saas/docker/offsite-backup.sh /usr/local/bin/saas-offsite-backup.sh
( crontab -l 2>/dev/null; echo "30 5 * * * /usr/local/bin/saas-offsite-backup.sh >> /var/log/saas-offsite-backup.log 2>&1" ) | crontab -
```

- 換 remote：cron 行前加 `SAAS_BACKUP_REMOTE=<remote>:<path>`。
- 每週驗證：`rclone check /opt/saas/backups pcloud:saas-backups --max-age 72h`

## 還原演練（上線前必做一次）
```bash
# 1. 取最新備份檔名
ls -t /opt/saas/backups/*.dump | head -1

# 2. 還原到臨時資料庫驗證（不動正式庫）
cd /opt/saas
docker-compose exec -T db createdb -U saas restore_test
docker-compose exec -T db pg_restore -U saas -d restore_test /backups/<檔名>
docker-compose exec -T db psql -U saas -d restore_test -c "select count(*) from tenants;"
docker-compose exec -T db dropdb -U saas restore_test

# 3. 真實災難還原（覆蓋正式庫 — 僅災難時）
#    參照 docker/restore.sh（容器內建 restore 腳本）
```

## 災難情境速查
| 情境 | 動作 |
|------|------|
| 誤刪資料 | 從 /opt/saas/backups 最近的 dump 還原（可先還原到 restore_test 撈單表） |
| 磁碟損毀 | 新機裝 docker + rclone → `rclone copy pcloud:saas-backups ./backups` → restore |
| 整機重建 | git clone repo → 還原 .env（**.env 不在備份內,另存密碼管理器**）→ compose up → restore |

⚠️ `.env`（金鑰/密碼）不隨 git 也不隨 DB 備份 — 請另存密碼管理器。
