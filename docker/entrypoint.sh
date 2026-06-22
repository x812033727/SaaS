#!/usr/bin/env bash
# 容器進入點：web（多 worker API）或 scheduler（ops 排程）。
# web 啟動前先等 DB 可連線，再跑一次 init_db（冪等遷移），避免多 worker 同時建表競態。
set -euo pipefail

wait_for_db() {
  echo "[entrypoint] 等待資料庫可連線…"
  python - <<'PY'
import time, sys
from sqlalchemy import create_engine, text
from saas_mvp.config import settings
url = settings.database_url
for i in range(60):
    try:
        e = create_engine(url)
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        print(f"[entrypoint] 資料庫就緒（{url.split('@')[-1]}）")
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        print(f"[entrypoint] DB 尚未就緒（{type(exc).__name__}），第 {i+1} 次重試…")
        time.sleep(2)
print("[entrypoint] 等待資料庫逾時", file=sys.stderr)
sys.exit(1)
PY
}

run_migrations() {
  echo "[entrypoint] 執行 init_db（建表 + 冪等遷移）…"
  python -c "from saas_mvp.db import init_db; init_db()"
}

case "${1:-web}" in
  web)
    wait_for_db
    run_migrations
    WORKERS="${GUNICORN_WORKERS:-4}"
    echo "[entrypoint] 啟動 gunicorn（${WORKERS} workers）…"
    # --forwarded-allow-ips：信任反代送來的 X-Forwarded-For/Proto，讓 uvicorn
    # 還原真實 client IP（否則 request.client.host 永遠是反代 IP，per-IP 限流會
    # 退化成全站共用一桶）。預設 "*" 之所以安全：容器埠只綁 loopback，唯一能連到
    # 它的就是本機 nginx。若改成公網直連，務必把此值設成可信反代 IP。
    exec gunicorn saas_mvp.app:app \
      -k uvicorn.workers.UvicornWorker \
      -w "${WORKERS}" \
      -b 0.0.0.0:8000 \
      --forwarded-allow-ips "${GUNICORN_FORWARDED_ALLOW_IPS:-*}" \
      --access-logfile - \
      --error-logfile - \
      --timeout 60 \
      --graceful-timeout 30
    ;;
  scheduler)
    wait_for_db
    echo "[entrypoint] 啟動 ops 排程器…"
    exec scheduler.sh
    ;;
  seed)
    wait_for_db
    run_migrations
    echo "[entrypoint] 灌入示範資料…"
    exec python -m saas_mvp.ops.seed_demo
    ;;
  *)
    exec "$@"
    ;;
esac
