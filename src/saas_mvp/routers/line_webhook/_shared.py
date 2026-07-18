"""line_webhook 套件共用:logger/路徑常數/router(R7-A 純搬移拆分)。"""
import logging

from fastapi import APIRouter




# logger 名固定為拆分前的模組名:tests 以 caplog.at_level(logger="saas_mvp.routers.
# line_webhook") 斷言,子模組共用此 logger 保持 log record 名不變。
_log = logging.getLogger("saas_mvp.routers.line_webhook")

# ── Webhook 路徑單一真相來源 ──────────────────────────────────────────────────
# router 掛載路徑與「對外公告的 webhook_url」共用同一組常數，避免兩處各自硬碼、
# 路由改名後靜默脫節。tenants router 的自助端點 import webhook_url_for() 組裝回應，
# 並有測試斷言「webhook_url 與 app 實際註冊的 route 一致」作保底。
LINE_WEBHOOK_PREFIX = "/line"
_WEBHOOK_ROUTE = "/webhook/{tenant_id}"
# 完整對外路徑模板，例：/line/webhook/{tenant_id}
LINE_WEBHOOK_PATH_TEMPLATE = LINE_WEBHOOK_PREFIX + _WEBHOOK_ROUTE


def webhook_url_for(tenant_id: int) -> str:
    """組裝租戶專屬 webhook 相對路徑（host 由部署端拼接）。"""
    return LINE_WEBHOOK_PATH_TEMPLATE.format(tenant_id=tenant_id)


router = APIRouter(
    prefix=LINE_WEBHOOK_PREFIX,
    tags=["line-webhook"],
)

_QUOTA_EXCEEDED_MSG = (
    "翻譯配額已超過今日上限，請明日再試或升級方案。"
)

# 統一的「驗章失敗」回應 detail。四條拒絕路徑（無 config / 缺 header / 簽章錯 /
# destination 不符）共用，避免外部藉 detail 區分租戶是否已設定。
_INVALID_SIGNATURE_DETAIL = "Invalid X-Line-Signature"

# 等量時間驗簽用的固定 dummy secret（32 bytes 對應 SHA-256 block size）。
