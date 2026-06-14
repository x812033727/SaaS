"""SQLAlchemy engine / session factory."""

import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from saas_mvp.config import settings

_log = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: yield a DB session then close it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables (idempotent)."""
    # import models so their metadata is registered
    # 順序：被依賴者先——Tenant → User → Note/ApiKey → ApiKeyUsage/ApiUsage → PlanChangeHistory
    from saas_mvp.models import tenant, user, note  # noqa: F401
    from saas_mvp.models import api_key, api_key_usage, usage  # noqa: F401
    from saas_mvp.models import plan_change_history  # noqa: F401
    from saas_mvp.models import line_channel_config  # noqa: F401
    Base.metadata.create_all(bind=engine)

    # 無 Alembic 環境的輕量 schema 演進：補既有 DB 缺少的新欄位。
    # 新環境由 create_all 自動建立，此處 inspect 後即略過。
    _migrate_add_line_bot_user_id()


def _migrate_add_line_bot_user_id() -> None:
    """為既有 line_channel_configs 表補上 line_bot_user_id 欄位（向後相容）。

    - 新環境：create_all 已建好欄位 → inspect 偵測到存在 → 直接略過。
    - 既有 DB（無 Alembic）：欄位不存在 → ALTER TABLE 補欄 + 建 unique index。
    - 拆成 ADD COLUMN 與獨立 CREATE UNIQUE INDEX 兩步，規避 SQLite 老版本
      inline UNIQUE 支援不穩定的問題；新欄位初始全為 NULL，SQLite unique
      index 允許多 NULL，無重複實值疑慮。
    - 整段以 try/except 包住，任何失敗僅記 warning，不阻擋服務啟動。
    """
    table = "line_channel_configs"
    column = "line_bot_user_id"
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return  # 表尚未建立（理論上 create_all 已建），無需遷移
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            return  # 欄位已存在（新環境或先前已遷移），冪等略過

        with engine.begin() as conn:
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR(64)")
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_lcfg_lbuid "
                    f"ON {table} ({column})"
                )
            )
        _log.info("migrated: added %s.%s column + unique index", table, column)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        # 僅記例外類型，不帶 exc_info traceback：避免 DB 連線錯誤的 DSN
        # （含密碼）被寫入日誌（資安審查建議）。
        _log.warning(
            "schema migration for %s.%s skipped due to error: %s",
            table, column, type(exc).__name__,
        )
