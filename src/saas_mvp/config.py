"""Application settings (env-overridable via SAAS_* variables or .env)."""

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

_INSECURE_DEFAULT = "change-me-in-production-use-32-chars-min"
_LINE_KEY_DEV_DEFAULT = "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc="


class Settings(BaseSettings):
    # Database
    database_url: str = "sqlite:///./saas_mvp.db"

    # JWT — SAAS_SECRET_KEY must be set in production; no weak default allowed
    secret_key: str = _INSECURE_DEFAULT
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # Server — 127.0.0.1 by default; override with SAAS_HOST=0.0.0.0 for containers
    host: str = "127.0.0.1"
    port: int = 8000

    # "dev" skips the secret_key guard so tests/local runs still work
    env: str = "dev"

    # LINE channel config encryption key (Fernet, URL-safe base64, decodes to 32 bytes)
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # MUST override in production via SAAS_LINE_CHANNEL_ENCRYPT_KEY env var.
    line_channel_encrypt_key: str = _LINE_KEY_DEV_DEFAULT

    # Rate limiting — set SAAS_RATE_LIMIT_ENABLED=false to bypass (e.g. in tests)
    rate_limit_enabled: bool = True

    # Business endpoint rate limits — format: "{calls}/{window_seconds}"
    # SAAS_KEY_RATE_LIMIT:    per-API-key limit (default 100 calls/60s)
    # SAAS_TENANT_RATE_LIMIT: per-tenant limit  (default 1000 calls/60s)
    key_rate_limit: str = "100/60"
    tenant_rate_limit: str = "1000/60"

    # 速率限制後端（多 worker / 橫向擴展）：
    # SAAS_RATE_LIMIT_BACKEND: "memory"（預設，in-process，單 worker）
    #                          | "redis"（跨 worker / 跨機共享同一份滑動視窗計數）。
    # SAAS_REDIS_URL: redis 連線字串（例 redis://localhost:6379/0）；
    #   backend=redis 但 url 空 / 連不上 / 未裝 redis 套件時，會記 warning 並
    #   fallback 回 memory（誤設定不致讓服務啟動失敗）。
    rate_limit_backend: str = "memory"
    redis_url: str = ""

    # Translation backend
    # SAAS_DEEPL_API_KEY: set to your DeepL auth key to enable real HTTP translation.
    #   If empty (default), get_translator() returns StubTranslator (offline, deterministic).
    # SAAS_DEEPL_API_URL: override the DeepL API endpoint (useful for paid tier or testing).
    deepl_api_key: str = ""
    deepl_api_url: str = "https://api-free.deepl.com/v2/translate"

    # 預約提醒（booking reminders）
    # SAAS_REMINDER_ENABLED:           入列開關（false 時建單不入列提醒）
    # SAAS_REMINDER_DAY_OF_LEAD_MINUTES: 當天提醒提前的分鐘數（預設 180 = 3 小時）
    # SAAS_REMINDER_MAX_PER_RUN:       ops 腳本單次最多派送筆數（防推播暴衝）
    reminder_enabled: bool = True
    reminder_day_of_lead_minutes: int = 180
    reminder_max_per_run: int = 500

    # 預約異動通知（PHASE 2）：店家修改/取消預約時 LINE 推播；
    # SAAS_NOTIFICATION_MAX_PER_RUN: ops 腳本單次最多派送筆數（防推播暴衝）。
    notification_max_per_run: int = 500

    # 會員集點（P3）：每完成一筆預約給的點數（0 = 停用集點）
    points_per_booking: int = 10

    # 商品銷售（P4）
    currency: str = "TWD"
    payment_provider: str = "stub"  # "stub" | "ecpay" | "newebpay"

    # 對外可達的網址（組綠界 ReturnURL / checkout 絕對網址用）；ecpay 模式必填。
    public_base_url: str = ""

    # 綠界 ECPay（金鑰預設為綠界公開測試值，正式環境由 SAAS_ECPAY_* 覆寫）
    ecpay_merchant_id: str = "2000132"
    ecpay_hash_key: str = "5294y06JbISpM5x9"
    ecpay_hash_iv: str = "v77hoKGq4kWxNNIS"
    ecpay_env: str = "stage"  # "stage"（測試）| "prod"（正式）
    # 定期定額執行次數（月扣上限 99≈8 年，等同長期；屆滿自動停需重訂）
    ecpay_period_exec_times: int = 99

    # 藍新金流 NewebPay（MPG 幕前；金鑰預設為空，正式環境由 SAAS_NEWEBPAY_* 覆寫）。
    # MerchantID + HashKey + HashIV 由藍新後台取得；env 決定送往測試/正式付款閘道。
    newebpay_merchant_id: str = ""
    newebpay_hash_key: str = ""
    newebpay_hash_iv: str = ""
    newebpay_env: str = "stage"  # "stage"（測試）| "prod"（正式）

    # LINE 隱私保護模式（PHASE 4-2）：以一次性 token 連結引導顧客在網頁填寫 PII，
    # 不在 LINE 聊天室中直接索取個資。SAAS_PII_TOKEN_TTL_MINUTES 為 token 有效分鐘數。
    pii_token_ttl_minutes: int = 1440

    # 月度推播額度（vibeaico「Additional Push Notification Allowance」）：
    # 每租戶每月可推播則數，跨所有 LINE push 路徑（提醒/異動通知/行銷）共用。
    # SAAS_PUSH_ALLOWANCE_BASE:  基本月度額度（預設 200）。
    # SAAS_PUSH_ALLOWANCE_BOOST: 加購 PUSH_BOOST 旗標時額外增加的額度（預設 +500）。
    push_allowance_base: int = 200
    push_allowance_boost: int = 500

    # 進階功能旗標 + 訂閱（橫向）
    # SAAS_FEATURES_DEFAULT_ENABLED: 無 TenantFeature 列時的預設。
    #   True  = 向後相容（進階功能預設開，不破壞既有/dev 易用）
    #   False = 嚴格 freemium（預設關，需訂閱才開）
    features_default_enabled: bool = True
    feature_monthly_price_cents: int = 20000  # NT$200

    # 多分店（PHASE 1）：每租戶可建的「啟用中」分店數量上限。
    max_locations_per_tenant: int = 5

    # 免費版員工數上限；開通 UNLIMITED_STAFF（輕量版以上）解除。
    free_staff_limit: int = 3

    # 會員等級結帳折扣（百分比）。對標 vibeaico「不同等級不同折扣」。
    # 0 = 該等級不折扣；於 POS 結帳對商品小計套用（在優惠券之前）。
    tier_discount_gold_percent: int = 10
    tier_discount_silver_percent: int = 5
    tier_discount_regular_percent: int = 0

    # OAuth 登入（PHASE 3：LINE Login + Google）。任一 provider 的 client_id/secret
    # 留空時，get_provider() 回傳 StubOAuthProvider（離線、決定性，供測試/dev）。
    line_login_channel_id: str = ""
    line_login_channel_secret: str = ""
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # OAuth callback 絕對網址 base；留空時 fallback 到 public_base_url。
    oauth_redirect_base: str = ""

    # 行銷自動化（PHASE 4-1）
    # SAAS_REACTIVATION_DORMANT_DAYS: 久未回訪判定（last_booked_at 早於 N 天）。
    # SAAS_REACTIVATION_CAP_PER_SHOP: 喚回活動每店單次發送上限（防推播暴衝）。
    # SAAS_MARKETING_MAX_PER_RUN:     行銷活動單次發送上限（通用，broadcast/birthday）。
    reactivation_dormant_days: int = 90
    reactivation_cap_per_shop: int = 50
    marketing_max_per_run: int = 500

    # 可觀測性（observability）
    # SAAS_LOG_FORMAT: "text"（預設，人類可讀）| "json"（log 聚合器）。
    # SAAS_LOG_LEVEL:  root logger 等級（DEBUG/INFO/WARNING/ERROR，預設 INFO）。
    # SAAS_METRICS_ENABLED: 是否啟用 Prometheus /metrics（預設 True）。
    # SAAS_METRICS_TOKEN: 非空時 /metrics 需帶 `Authorization: Bearer <token>`；
    #   留空（預設）代表 /metrics 不設限——僅應在內網 / 受信任網段曝露。
    log_format: str = "text"
    log_level: str = "INFO"
    metrics_enabled: bool = True
    metrics_token: str = ""

    # AI 客服（PHASE 4-1，Anthropic Claude）
    # SAAS_ANTHROPIC_API_KEY: 設定後 get_assistant() 回 AnthropicAssistant；
    #   留空（預設）回 StubAIAssistant（離線、決定性，供測試/dev）。
    # SAAS_AI_MODEL: Claude 模型 ID（預設 Claude Sonnet 4.6）。
    anthropic_api_key: str = ""
    ai_model: str = "claude-sonnet-4-6"

    # 後台「重新佈署」按鈕（平台管理員）：
    # web 容器無法直接執行主機腳本，故按鈕只寫一個觸發檔到此路徑（主機掛載目錄），
    # 主機端 systemd.path（saas-deploy-trigger.path）監看到即跑 /usr/local/bin/saas-deploy.sh。
    # 留空（預設）= 隱藏按鈕並停用觸發（dev/test 不會誤觸）。
    # 正式環境設 SAAS_DEPLOY_TRIGGER_PATH=/var/run/saas-deploy/deploy.request。
    deploy_trigger_path: str = ""

    @model_validator(mode="after")
    def line_key_must_be_changed_in_prod(self) -> "Settings":
        """在非 dev/test 環境（讀 self.env，含 .env 檔）拒絕公開 dev 預設金鑰。

        使用 model_validator 而非 field_validator，確保 self.env 已載入
        （field_validator 只能用 os.getenv，會漏看 .env 檔案中設定的 SAAS_ENV）。
        """
        if (self.line_channel_encrypt_key == _LINE_KEY_DEV_DEFAULT
                and self.env.lower() not in ("dev", "test")):
            raise ValueError(
                "SAAS_LINE_CHANNEL_ENCRYPT_KEY must be set to a unique Fernet key "
                "in non-dev environments. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        return self

    @field_validator("secret_key")
    @classmethod
    def secret_key_must_be_changed(cls, v: str) -> str:
        # env is not yet available at field-level validation time,
        # so we guard only against the exact insecure default string.
        # The application startup check in app.py additionally enforces this
        # at runtime when env != "dev".
        if v == _INSECURE_DEFAULT:
            import os
            if os.getenv("SAAS_ENV", "dev").lower() not in ("dev", "test"):
                raise ValueError(
                    "SAAS_SECRET_KEY must be set to a strong random value "
                    "in non-dev environments. "
                    "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
                )
        return v

    class Config:
        env_prefix = "SAAS_"
        env_file = ".env"
        env_file_encoding = "utf-8"
        # 忽略 .env / 環境中非 SAAS_ 的額外鍵：正式部署的 .env 常含部署層
        # 變數（POSTGRES_USER、WEB_PORT、GUNICORN_WORKERS…，供 docker-compose 用），
        # 這些不該讓 app 啟動時因 extra_forbidden 崩潰。
        extra = "ignore"


settings = Settings()
