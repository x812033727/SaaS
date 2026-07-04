"""pytest session-wide conftest — 最早執行，確保測試環境設定正確。

pytest 在 import 任何 test_*.py 之前先 import conftest.py，
因此這裡是設定 env var 的最安全時機。
"""

import os
import pathlib

# 關閉 auth rate limit，避免多個測試累積超過 20 req/60s 的限制。
# 這裡用 [] 而非 setdefault，確保無論環境原本是什麼值都覆蓋。
os.environ["SAAS_RATE_LIMIT_ENABLED"] = "false"

# 關閉 /ui CSRF（double-submit token），既有 UI 測試不必逐一帶 token；
# CSRF 行為本身由 tests/test_ui_csrf.py 以 monkeypatch 動態開啟專測。
os.environ["SAAS_UI_CSRF_ENABLED"] = "false"

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

# 確保所有 relationship 字串引用的 model 都進入 SQLAlchemy class registry；
# 各測試檔才能單獨執行，不靠其他測試先 import 的順序副作用。
from saas_mvp.models import api_key as _ak  # noqa: F401, E402
from saas_mvp.models import api_key_usage as _aku  # noqa: F401, E402
from saas_mvp.models import note as _n  # noqa: F401, E402
from saas_mvp.models import plan_change_history as _pch  # noqa: F401, E402
from saas_mvp.models import tenant as _t  # noqa: F401, E402
from saas_mvp.models import usage as _us  # noqa: F401, E402
from saas_mvp.models import user as _u  # noqa: F401, E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401, E402
import saas_mvp.models.line_user_lang as _lul  # noqa: F401, E402
import saas_mvp.models.line_webhook_event as _lwe  # noqa: F401, E402
import saas_mvp.models.customer as _cust  # noqa: F401, E402
import saas_mvp.models.booking_slot as _bslot  # noqa: F401, E402
import saas_mvp.models.reservation as _resv  # noqa: F401, E402
import saas_mvp.models.reservation_reminder as _rem  # noqa: F401, E402
import saas_mvp.models.booking_waitlist as _wl  # noqa: F401, E402
import saas_mvp.models.coupon as _coupon  # noqa: F401, E402
import saas_mvp.models.coupon_redemption as _credeem  # noqa: F401, E402
import saas_mvp.models.point_transaction as _ptx  # noqa: F401, E402
import saas_mvp.models.product as _prod  # noqa: F401, E402
import saas_mvp.models.order as _order  # noqa: F401, E402
import saas_mvp.models.order_item as _oitem  # noqa: F401, E402
import saas_mvp.models.tenant_feature as _tf  # noqa: F401, E402
import saas_mvp.models.feature_change_history as _fch  # noqa: F401, E402
import saas_mvp.models.feature_subscription as _fsub  # noqa: F401, E402
import saas_mvp.models.subscription_charge as _subchg  # noqa: F401, E402
import saas_mvp.models.location as _loc  # noqa: F401, E402
import saas_mvp.models.staff as _staff  # noqa: F401, E402
import saas_mvp.models.staff_shift as _sshift  # noqa: F401, E402
import saas_mvp.models.staff_leave as _sleave  # noqa: F401, E402
import saas_mvp.models.service_category as _scat  # noqa: F401, E402
import saas_mvp.models.service as _svc  # noqa: F401, E402
import saas_mvp.models.service_staff as _svcstaff  # noqa: F401, E402
import saas_mvp.models.customer_tag as _ctag  # noqa: F401, E402
import saas_mvp.models.customer_tag_link as _ctaglink  # noqa: F401, E402
import saas_mvp.models.booking_notification as _bnotif  # noqa: F401, E402
import saas_mvp.models.business_profile as _bprofile  # noqa: F401, E402
import saas_mvp.models.portfolio_category as _pcat  # noqa: F401, E402
import saas_mvp.models.portfolio_item as _pitem  # noqa: F401, E402
import saas_mvp.models.campaign as _camp  # noqa: F401, E402
import saas_mvp.models.campaign_send as _campsend  # noqa: F401, E402
import saas_mvp.models.faq_entry as _faq  # noqa: F401, E402
import saas_mvp.models.pii_request as _pii  # noqa: F401, E402
import saas_mvp.models.flex_menu as _flexmenu  # noqa: F401, E402
import saas_mvp.models.flex_menu_card as _flexcard  # noqa: F401, E402
import saas_mvp.models.auto_reply_rule as _autorule  # noqa: F401, E402
import saas_mvp.models.push_usage as _pushusage  # noqa: F401, E402

# ── 測試提速：bcrypt 預設 12 rounds，每次 hash/verify 成本高，數百個 register/login
# 測試累積使全套逼近 60s self-test 逾時。測試環境把 rounds 降到合法最低值 4，
# 雜湊行為與格式不變（仍是 bcrypt），僅迭代次數變少 → 全套大幅加速。
# 只在 test session 生效（conftest 只在測試載入），不影響 production 設定。
# 用 .update() 原地修改既有 context，保留任何已持有 _pwd_ctx 引用的程式碼正常運作。
import saas_mvp.auth.security as _security  # noqa: E402

_security._pwd_ctx.update(bcrypt__rounds=4)
