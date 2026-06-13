"""pytest session-wide conftest — 最早執行，確保測試環境設定正確。

pytest 在 import 任何 test_*.py 之前先 import conftest.py，
因此這裡是設定 env var 的最安全時機。
"""

import os
import pathlib

# 關閉 auth rate limit，避免多個測試累積超過 20 req/60s 的限制。
# 這裡用 [] 而非 setdefault，確保無論環境原本是什麼值都覆蓋。
os.environ["SAAS_RATE_LIMIT_ENABLED"] = "false"

# 確保 subprocess（如 test_task1_structure.py 的 subprocess.run）也能 import saas_mvp。
# pytest 的 pythonpath=["src"] 只修改當前 process 的 sys.path，不傳遞給 subprocess。
# 在此明確把 src/ 的絕對路徑寫入 PYTHONPATH，讓所有子行程繼承正確路徑，
# 不依賴 /tmp/test_clean/ 等可能在驗證環境不存在的暫存目錄。
_src_dir = str(pathlib.Path(__file__).parent.parent / "src")
_existing_pp = os.environ.get("PYTHONPATH", "")
os.environ["PYTHONPATH"] = _src_dir + (":" + _existing_pp if _existing_pp else "")
