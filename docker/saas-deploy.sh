#!/usr/bin/env bash
# /opt/saas 乾淨自動重佈：git 同步 origin/main → CI 門檻 → rebuild → prune 舊 image。
# 安裝(主機)：install -m 755 /opt/saas/docker/saas-deploy.sh /usr/local/bin/saas-deploy.sh
# 以 flock 串行化(避免並發部署);各次部署都 fetch+reset 到最新,故排隊的最後一次即含全部變更。
# CI 門檻:等 GitHub Actions 對 origin/main SHA 的 check-runs 全綠才部署;
# 紅燈或逾時(15 分鐘)則中止本次部署(舊版本繼續跑)。
# 緊急繞過:SAAS_DEPLOY_SKIP_CI_GATE=1 /usr/local/bin/saas-deploy.sh
set -euo pipefail
exec 9>/var/lock/saas-deploy.lock
flock -w 900 9 || { echo "$(date -u +%FT%TZ) [deploy] 取鎖逾時，略過"; exit 0; }

cd /opt/saas
echo "=== $(date -u +%FT%TZ) [deploy] 開始 ==="
git fetch --prune origin
TARGET=$(git rev-parse origin/main)

# ── CI 門檻 ──────────────────────────────────────────────
if [ "${SAAS_DEPLOY_SKIP_CI_GATE:-0}" != "1" ]; then
  echo "[deploy] 等待 CI 結果 (${TARGET:0:7})"
  DEADLINE=$(( $(date +%s) + 900 ))
  CI_STATE="pending"
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    # 只看 GitHub Actions 的 check-runs;尚無任何 run 視為 pending(workflow 可能還在排隊)
    CI_STATE=$(gh api "repos/x812033727/SaaS/commits/$TARGET/check-runs" \
      --jq '[.check_runs[] | select(.app.slug=="github-actions")]
            | if length==0 then "pending"
              elif any(.status!="completed") then "pending"
              elif all(.conclusion=="success" or .conclusion=="skipped") then "success"
              else "failure" end' 2>/dev/null) || CI_STATE="api_error"
    case "$CI_STATE" in
      success) break ;;
      failure)
        echo "[deploy] CI 紅燈 (${TARGET:0:7})，中止部署"; exit 0 ;;
      *) sleep 20 ;;
    esac
  done
  if [ "$CI_STATE" != "success" ]; then
    echo "[deploy] CI 逾時未轉綠 (最後狀態=$CI_STATE)，中止部署"; exit 0
  fi
  echo "[deploy] CI 綠燈"
fi

BEFORE=$(git rev-parse --short HEAD)
git reset --hard "$TARGET"
AFTER=$(git rev-parse --short HEAD)
echo "[deploy] $BEFORE -> $AFTER"
if [ "$BEFORE" = "$AFTER" ]; then
  echo "[deploy] 無變更，仍重建以確保一致"
fi
docker-compose up -d --build
docker image prune -f >/dev/null 2>&1 || true
echo "=== $(date -u +%FT%TZ) [deploy] 完成 ($AFTER) ==="
