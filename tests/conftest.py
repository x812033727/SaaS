"""pytest session-wide conftest — 最早執行，確保測試環境設定正確。

pytest 在 import 任何 test_*.py 之前先 import conftest.py，
因此這裡是設定 env var 的最安全時機。
"""

import os
import pathlib

# 關閉 auth rate limit，避免多個測試累積超過 20 req/60s 的限制。
# 這裡用 [] 而非 setdefault，確保無論環境原本是什麼值都覆蓋。
os.environ["SAAS_RATE_LIMIT_ENABLED"] = "false"

# DB URL 設 in-memory：必須在任何 saas_mvp 模組 import 之前設定，
# 因為 db.py 在模組層級就建立 engine（settings.database_url）。
# setdefault：若 CI/環境已有真實 DB URL 則不覆蓋。
os.environ.setdefault("SAAS_DATABASE_URL", "sqlite:///:memory:")

# 確保 subprocess（如 test_task1_structure.py 的 subprocess.run）也能 import saas_mvp。
# pytest 的 pythonpath=["src"] 只修改當前 process 的 sys.path，不傳遞給 subprocess。
# 在此明確把 src/ 的絕對路徑寫入 PYTHONPATH，讓所有子行程繼承正確路徑，
# 不依賴 /tmp/test_clean/ 等可能在驗證環境不存在的暫存目錄。
_src_dir = str(pathlib.Path(__file__).parent.parent / "src")
_existing_pp = os.environ.get("PYTHONPATH", "")
os.environ["PYTHONPATH"] = _src_dir + (":" + _existing_pp if _existing_pp else "")

# ── DB 修正：lifespan 的 init_db() 使用 sqlite:///./saas_mvp.db（檔案型），
# 在 CI / 沙盒環境可能因路徑或權限問題無法開啟。
# 各測試模組的 client fixture 已透過 Base.metadata.create_all(bind=_engine)
# 建立 in-memory 表格，並以 override_get_db 注入同一個 in-memory engine，
# 因此 lifespan 階段的 init_db 可安全替換為 no-op。
import saas_mvp.db as _saas_db  # noqa: E402

_saas_db.init_db = lambda: None

# 確保 LineChannelConfig 進入 SQLAlchemy class registry，
# 使 Tenant.line_channel_config relationship 的字串解析不失敗。
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401, E402
