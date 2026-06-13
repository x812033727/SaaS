"""pytest session-wide conftest — 最早執行，確保測試環境設定正確。

pytest 在 import 任何 test_*.py 之前先 import conftest.py，
因此這裡是設定 env var 的最安全時機。
"""

import os

# 關閉 auth rate limit，避免多個測試累積超過 20 req/60s 的限制。
# 這裡用 [] 而非 setdefault，確保無論環境原本是什麼值都覆蓋。
os.environ["SAAS_RATE_LIMIT_ENABLED"] = "false"
