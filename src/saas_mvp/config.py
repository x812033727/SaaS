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
