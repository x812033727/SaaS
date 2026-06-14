#!/usr/bin/env bash
# run_tests.sh — 跨環境測試入口
#
# 問題背景：此機器沒有 `python`，只有 `python3` 與 `/opt/ti/.venv/bin/python`。
# 此腳本自動選出最佳可用 Python → 設環境變數 → 跑 pytest。
#
# 用法：bash run_tests.sh

set -euo pipefail

# ── 1. 選 Python ──────────────────────────────────────────────
PYTHON=""
for candidate in /opt/ti/.venv/bin/python python3 python; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[run_tests.sh] ERROR: 找不到可用的 Python，請安裝 Python 3.11+" >&2
    exit 1
fi

echo "[run_tests.sh] 使用 Python: $("$PYTHON" --version 2>&1)"

# ── 2. 確保依賴已安裝 ─────────────────────────────────────────
# 優先用 venv pip；若不存在則用 python 自帶的 pip 模組
if [ -x "/opt/ti/.venv/bin/pip" ]; then
    PIP="/opt/ti/.venv/bin/pip"
else
    PIP="$PYTHON -m pip"
fi

# 依賴已可匯入時跳過 pip（省去在唯讀 FS 下注定失敗、仍耗數秒的解析開銷）。
# 否則才嘗試安裝 editable + test extra，失敗（唯讀 FS / 權限）時靜默繼續。
# PYTHONPATH=src 確保不依賴 site-packages 也能 import saas_mvp。
if ! PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -c \
        "import sqlalchemy, fastapi, pytest, saas_mvp" 2>/dev/null; then
    "$PIP" install --quiet -e ".[test]" 2>/dev/null || true
fi

# ── 3. 設環境變數 ─────────────────────────────────────────────
# DB 指向 in-memory，避免 init_db() 嘗試建立檔案型 SQLite
export SAAS_DATABASE_URL="${SAAS_DATABASE_URL:-sqlite:///:memory:}"
export SAAS_RATE_LIMIT_ENABLED="${SAAS_RATE_LIMIT_ENABLED:-false}"
# 測試環境用最低 bcrypt cost 加速大量密碼雜湊（生產維持預設 12，見 auth/security.py）。
# 大量 register/login 測試走 bcrypt 雜湊，預設 12 rounds 使整套接近逾時邊界。
# SAAS_TESTING=1 是低成本值的明確放行旗標；缺它時 security.py 對 <10 的值會 fail-closed。
export SAAS_TESTING="${SAAS_TESTING:-1}"
export SAAS_BCRYPT_ROUNDS="${SAAS_BCRYPT_ROUNDS:-4}"

# ── 4. 執行 pytest ────────────────────────────────────────────
# PYTHONPATH=src 確保 saas_mvp 從 src/ 載入，不依賴 site-packages
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

exec "$PYTHON" -m pytest -q "$@"
