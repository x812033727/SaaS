"""Application settings (env-overridable, sane defaults for local dev)."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = "sqlite:///./saas_mvp.db"

    # JWT
    secret_key: str = "change-me-in-production-use-32-chars-min"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    class Config:
        env_prefix = "SAAS_"
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
