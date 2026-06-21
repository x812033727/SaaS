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


def get_session_factory():
    """FastAPI dependency: 回傳可呼叫的 session factory（無引數，回傳 ``Session``）。

    預設回傳 :data:`SessionLocal`（production module-level singleton）。
    測試透過 ``app.dependency_overrides[get_session_factory] = lambda: _Session``
    替換成測試自己的 ``sessionmaker(bind=_engine)``——背景任務內
    ``db = session_factory()`` 即可沿用測試的 ``StaticPool`` 共連 in-memory
    SQLite，看得到所有 ``Base.metadata.create_all`` 建出的表。

    用途：背景任務（line_webhook ``_process_events`` 等）需要「離開 request
    生命週期」自管 session，又不能硬編 ``SessionLocal()``——硬編會綁死
    production engine，測試無法 override。

    與 :func:`get_db` 的差異：``get_db`` 為 request-scoped yield session
    （FastAPI 依賴收尾關閉）；本函式回傳**可重複呼叫**的 factory，適合跨
    await 邊界、需獨立交易的任務。
    """
    return SessionLocal


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
    from saas_mvp.models import line_webhook_event  # noqa: F401
    from saas_mvp.models import line_user_lang  # noqa: F401
    # 預約（booking）相關模型：customer 先於 reservation（FK 依賴）。
    from saas_mvp.models import customer, booking_slot  # noqa: F401
    from saas_mvp.models import reservation, reservation_reminder  # noqa: F401
    Base.metadata.create_all(bind=engine)

    # 無 Alembic 環境的輕量 schema 演進：補既有 DB 缺少的新欄位。
    # 新環境由 create_all 自動建立，此處 inspect 後即略過。
    _migrate_add_line_bot_user_id()
    _migrate_add_line_credential_status_fields()
    _migrate_backfill_char_count()
    _migrate_add_tenant_store_type()
    _migrate_add_line_bot_mode()


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


def _migrate_add_line_credential_status_fields() -> None:
    """為既有 line_channel_configs 表補上 credential 驗證狀態欄位。

    只做 ADD COLUMN，不回填舊資料；讀取/API 邊界會把 NULL 正規化為
    ``unchecked``。任何失敗僅記 warning，不阻擋服務啟動。
    """
    table = "line_channel_configs"
    columns = {
        "credential_status": "VARCHAR(16)",
        "credential_last_error": "VARCHAR(255)",
        "credential_checked_at": "DATETIME",
    }
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return

        existing = {col["name"] for col in inspector.get_columns(table)}
        missing = {
            name: col_type for name, col_type in columns.items() if name not in existing
        }
        if not missing:
            return

        with engine.begin() as conn:
            for name, col_type in missing.items():
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}"))
        _log.info(
            "migrated: added %s columns to %s",
            ", ".join(sorted(missing)),
            table,
        )
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s credential fields skipped due to error: %s",
            table,
            type(exc).__name__,
        )


def _migrate_add_tenant_store_type() -> None:
    """為既有 tenants 表補上 store_type 欄位（向後相容）。

    - 新環境：create_all 已建好欄位 → inspect 偵測到存在 → 直接略過。
    - 既有 DB（無 Alembic）：欄位不存在 → ALTER TABLE 補欄。
    - store_type 為分類標籤，**不建 unique index**；新欄位初始全為 NULL（未分類）。
    - 整段以 try/except 包住，任何失敗僅記 warning，不阻擋服務啟動。
    """
    table = "tenants"
    column = "store_type"
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return  # 表尚未建立（理論上 create_all 已建），無需遷移
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            return  # 欄位已存在（新環境或先前已遷移），冪等略過

        with engine.begin() as conn:
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR(32)")
            )
        _log.info("migrated: added %s.%s column", table, column)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s.%s skipped due to error: %s",
            table, column, type(exc).__name__,
        )


def _migrate_add_line_bot_mode() -> None:
    """為既有 line_channel_configs 表補上 bot_mode 欄位（向後相容）。

    - 新環境：create_all 已建好欄位 → inspect 偵測到存在 → 直接略過。
    - 既有 DB（無 Alembic）：欄位不存在 → ALTER TABLE 補欄，**帶 NOT NULL
      DEFAULT 'translation'**，既有列自動回填 translation（既有翻譯店家零影響）。
    - 整段以 try/except 包住，任何失敗僅記 warning，不阻擋服務啟動。
    """
    table = "line_channel_configs"
    column = "bot_mode"
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return  # 表尚未建立（理論上 create_all 已建），無需遷移
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            return  # 欄位已存在（新環境或先前已遷移），冪等略過

        with engine.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR(16) "
                    "NOT NULL DEFAULT 'translation'"
                )
            )
        _log.info("migrated: added %s.%s column", table, column)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s.%s skipped due to error: %s",
            table, column, type(exc).__name__,
        )


def _migrate_backfill_char_count() -> None:
    """為既有 api_usage 表回填 NULL 的 char_count 為 0（一次性資料修正）。

    背景：
      ApiUsage 在 schema 演進中新增 ``char_count`` 欄位（nullable=False,
      default=0）。SQLAlchemy 的 ``default=0`` 僅對新 INSERT 生效；既有
      列（特別是 ALTER TABLE ADD COLUMN 階段未被回填的）可能仍為 NULL。
      讀取端雖以 ``(row.char_count or 0)`` 兜底，DB 層級仍可能因 NULL
      違反 NOT NULL 約束而對部分操作（例如 PostgreSQL strict 模式下的
      聚合查詢）報錯。本函式以 idempotent UPDATE 把 NULL 統一回填 0。

    設計：
      - 用原生 SQL ``UPDATE api_usage SET char_count = 0 WHERE
        char_count IS NULL``，SQLite/PostgreSQL 通用、零 ORM 開銷。
      - 冪等：第二次起無 NULL 列，UPDATE 影響 0 列 → no-op。
      - 表不存在（理論上不會：create_all 已建）→ 略過不爆。
      - 失敗僅 warning、不阻擋啟動：與 _migrate_add_line_bot_user_id 同形。
    """
    table = "api_usage"
    column = "char_count"
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column not in existing:
            return  # 舊 DB 完全缺 char_count 欄位——不在本 backfill 範圍
        with engine.begin() as conn:
            result = conn.execute(
                text(f"UPDATE {table} SET {column} = 0 WHERE {column} IS NULL")
            )
            if result.rowcount:
                _log.info(
                    "backfilled %s.%s: %d rows set to 0", table, column, result.rowcount
                )
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "backfill for %s.%s skipped due to error: %s",
            table, column, type(exc).__name__,
        )
