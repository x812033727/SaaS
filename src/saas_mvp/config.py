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

    # 會員集點（P3）：每完成一筆預約給的點數（0 = 停用集點）
    points_per_booking: int = 10

    # 商品銷售（P4）
    currency: str = "TWD"
    payment_provider: str = "stub"  # "stub" | "ecpay"

    # 對外可達的網址（組綠界 ReturnURL / checkout 絕對網址用）；ecpay 模式必填。
    public_base_url: str = ""

    # 綠界 ECPay（金鑰預設為綠界公開測試值，正式環境由 SAAS_ECPAY_* 覆寫）
    ecpay_merchant_id: str = "2000132"
    ecpay_hash_key: str = "5294y06JbISpM5x9"
    ecpay_hash_iv: str = "v77hoKGq4kWxNNIS"
    ecpay_env: str = "stage"  # "stage"（測試）| "prod"（正式）
    # 定期定額執行次數（月扣上限 99≈8 年，等同長期；屆滿自動停需重訂）
    ecpay_period_exec_times: int = 99

    # 進階功能旗標 + 訂閱（橫向）
    # SAAS_FEATURES_DEFAULT_ENABLED: 無 TenantFeature 列時的預設。
    #   True  = 向後相容（進階功能預設開，不破壞既有/dev 易用）
    #   False = 嚴格 freemium（預設關，需訂閱才開）
    features_default_enabled: bool = True
    feature_monthly_price_cents: int = 20000  # NT$200

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


settings = Settings()
