"""Application settings (env-overridable via SAAS_* variables or .env)."""

from pydantic import field_validator
from pydantic_settings import BaseSettings

_INSECURE_DEFAULT = "change-me-in-production-use-32-chars-min"


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
