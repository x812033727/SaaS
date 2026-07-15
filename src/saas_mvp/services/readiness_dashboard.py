"""將技術 readiness 檢查轉成平台管理員可操作的網頁資料。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from saas_mvp.ops.check_readiness import Check, run_checks


@dataclass(frozen=True)
class ReadinessItem:
    key: str
    label: str
    category: str
    status: str
    detail: str
    guidance: str
    action_url: str | None = None
    action_label: str | None = None


@dataclass(frozen=True)
class ReadinessDashboard:
    items: list[ReadinessItem]
    passes: int
    warns: int
    fails: int
    progress: int

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def launch_safe(self) -> bool:
        return self.fails == 0

    @property
    def commercially_ready(self) -> bool:
        return self.fails == 0 and self.warns == 0


_META = {
    "secret_key": (
        "應用程式安全金鑰", "安全", "核心登入與資料簽章使用的金鑰。", None, None
    ),
    "line_channel_encrypt_key": (
        "敏感資料加密金鑰", "安全", "LINE 與平台憑證會以此金鑰加密保存。", None, None
    ),
    "env": ("正式環境模式", "安全", "正式站必須以正式環境模式運行。", None, None),
    "ui_csrf": ("後台表單防護", "安全", "防止第三方網站冒用登入狀態送出操作。", None, None),
    "payment": (
        "線上金流", "營收", "啟用後才能向顧客實際收款。",
        "/ui/admin/payment-settings", "前往金流設定",
    ),
    "public_base_url": (
        "付款通知網址", "營收", "金流需要安全的公開網址回傳付款結果。",
        "/ui/admin/payment-settings", "查看通知網址",
    ),
    "invoice": (
        "電子發票", "營收", "付款成功後自動開立並支援重試與作廢。",
        "/ui/admin/invoice-settings", "前往發票設定",
    ),
    "smtp": (
        "Email 寄送", "通知", "用於驗證信、重設密碼與重要營運通知。",
        "/ui/admin/email-settings", "前往寄信設定",
    ),
    "ai": (
        "AI 客服", "整合", "啟用後才能提供 AI 回覆與知識庫功能。",
        "/ui/admin/ai-settings", "前往 AI 設定",
    ),
    "gcal_oauth": (
        "Google 登入與日曆", "整合", "提供 Google 登入與預約同步。",
        "/ui/admin/oauth-settings", "前往登入設定",
    ),
    "line_config": (
        "LINE 官方帳號", "整合", "至少需要一個店家完成 LINE Bot 綁定。",
        "/ui/admin/bots", "查看 LINE 綁定",
    ),
    "sentry": (
        "錯誤監控", "維運", "建議正式商用啟用即時錯誤告警。", None, None
    ),
    "sms": (
        "簡訊備援", "通知", "未啟用供應商時維持關閉，避免誤以為已發送簡訊。", None, None
    ),
    "db_migration": (
        "資料庫版本", "維運", "資料庫結構必須與目前程式版本一致。", None, None
    ),
}


def _friendly_detail(check: Check) -> str:
    key, status, raw = check.name, check.status, check.detail
    if key == "payment":
        if "stub" in raw:
            return "目前為模擬付款，不會實際收款。"
        if "stage" in raw or "sandbox" in raw:
            return "目前為測試環境，適合演練但尚未正式收款。"
        return "正式金流已啟用。" if status == "PASS" else "金流設定尚未完成。"
    if key == "invoice":
        if "stub" in raw:
            return "目前為模擬發票，不會開立正式發票。"
        if status == "PASS":
            return "電子發票憑證已完成設定。"
        return "電子發票設定尚未完成。"
    if key == "smtp":
        return "Email 寄送服務已設定。" if status == "PASS" else "尚未設定 Email 寄送服務。"
    if key == "ai":
        return "AI 服務已設定。" if status == "PASS" else "AI 客服尚未設定。"
    if key == "gcal_oauth":
        return "Google 整合已設定。" if status == "PASS" else "Google 整合尚未設定。"
    if key == "line_config":
        return "已有店家完成 LINE 綁定。" if status == "PASS" else "尚無店家完成 LINE 綁定。"
    if key == "sentry":
        return "錯誤監控已啟用。" if status == "PASS" else "尚未啟用即時錯誤告警。"
    if key == "sms":
        return "簡訊備援目前安全地保持關閉。" if status == "PASS" else "簡訊供應商尚未完成設定。"
    if key == "db_migration":
        return "資料庫已是目前版本。" if status == "PASS" else "資料庫版本需要維運處理。"
    if key == "public_base_url":
        return "付款通知網址使用安全連線。" if status == "PASS" else "付款通知網址尚未符合安全要求。"
    if key in {"secret_key", "line_channel_encrypt_key", "env", "ui_csrf"}:
        return "設定正確。" if status == "PASS" else "核心安全設定需要平台維運人員處理。"
    return "檢查通過。" if status == "PASS" else "此項目需要確認。"


def build_dashboard(db: Session) -> ReadinessDashboard:
    bind = db.get_bind()
    factory = sessionmaker(autocommit=False, autoflush=False, bind=bind)
    checks = run_checks(
        session_factory=factory,
        db_engine=bind,
        include_host_checks=False,
    )
    items: list[ReadinessItem] = []
    for check in checks:
        meta = _META.get(check.name)
        if meta is None:
            continue
        label, category, guidance, action_url, action_label = meta
        items.append(ReadinessItem(
            key=check.name,
            label=label,
            category=category,
            status=check.status,
            detail=_friendly_detail(check),
            guidance=guidance,
            action_url=action_url,
            action_label=action_label,
        ))
    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    items.sort(key=lambda item: (order[item.status], item.category, item.label))
    passes = sum(item.status == "PASS" for item in items)
    warns = sum(item.status == "WARN" for item in items)
    fails = sum(item.status == "FAIL" for item in items)
    progress = round(passes * 100 / len(items)) if items else 0
    return ReadinessDashboard(items, passes, warns, fails, progress)
