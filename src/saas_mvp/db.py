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


def import_all_models() -> None:
    """Import every ORM model so SQLAlchemy's class registry is fully populated.

    Standalone entrypoints（如 ops/ 腳本）在 app 未 import 全部 router 的情況下，
    若只 import 部分 model，relationship 字串引用（例：Tenant→Note）會解析失敗。
    呼叫此函式即可保證註冊表完整。不建表、無副作用，可重複呼叫。
    """
    # 順序：被依賴者先——Tenant → User → Note/ApiKey → ApiKeyUsage/ApiUsage → PlanChangeHistory
    from saas_mvp.models import organization, tenant, user, note  # noqa: F401
    from saas_mvp.models import platform_oauth_config  # noqa: F401
    from saas_mvp.models import platform_email_config  # noqa: F401
    from saas_mvp.models import platform_ai_config  # noqa: F401
    from saas_mvp.models import platform_payment_config  # noqa: F401
    from saas_mvp.models import email_delivery  # noqa: F401
    from saas_mvp.models import api_key, api_key_usage, usage  # noqa: F401
    from saas_mvp.models import plan_change_history  # noqa: F401
    from saas_mvp.models import line_channel_config  # noqa: F401
    from saas_mvp.models import line_webhook_event  # noqa: F401
    from saas_mvp.models import line_user_lang  # noqa: F401
    # 預約（booking）相關模型：customer 先於 reservation（FK 依賴）。
    from saas_mvp.models import customer, booking_slot  # noqa: F401
    from saas_mvp.models import reservation, reservation_reminder  # noqa: F401
    from saas_mvp.models import gcal_sync_job  # noqa: F401
    # 額滿候補（依賴 booking_slot FK）。
    from saas_mvp.models import booking_waitlist  # noqa: F401
    # P3 優惠券/會員
    from saas_mvp.models import coupon, coupon_redemption, point_transaction  # noqa: F401
    # P4 商品銷售
    from saas_mvp.models import product, order, order_item  # noqa: F401
    # 橫向：進階功能旗標 + 訂閱
    from saas_mvp.models import tenant_feature, feature_change_history  # noqa: F401
    # 進階功能訂閱月費（綠界信用卡定期定額 recurring）+ 逐期扣款明細。
    from saas_mvp.models import feature_subscription  # noqa: F401
    from saas_mvp.models import subscription_charge  # noqa: F401
    # PHASE 1：多分店 / 員工排班 / 服務目錄（location 先於 staff，staff 先於 service_staff）。
    from saas_mvp.models import location  # noqa: F401
    from saas_mvp.models import staff, staff_shift, staff_leave  # noqa: F401
    from saas_mvp.models import service_category, service, service_staff  # noqa: F401
    # PHASE 2：顧客標籤/分眾、行事曆 ICS、預約異動通知。
    from saas_mvp.models import customer_tag, customer_tag_link  # noqa: F401
    from saas_mvp.models import booking_notification  # noqa: F401
    # PHASE 3：公開店家頁、作品集、OAuth 登入。
    from saas_mvp.models import business_profile  # noqa: F401
    from saas_mvp.models import portfolio_category, portfolio_item  # noqa: F401
    # PHASE 4-1：行銷自動化（活動 + 發送紀錄）+ AI 客服 FAQ。
    from saas_mvp.models import campaign, campaign_send  # noqa: F401
    from saas_mvp.models import faq_entry  # noqa: F401
    # PHASE 4-2：隱私保護模式（tokenized PII 表單請求）。
    from saas_mvp.models import pii_request  # noqa: F401
    # A1.1：網頁預約表單 token（tokenized 深連結）。
    from saas_mvp.models import booking_form_token  # noqa: F401
    # B3：Email 用途 token（驗證/重設密碼/邀請）。
    from saas_mvp.models import email_token  # noqa: F401
    # A3.3：預約後滿意度調查。
    from saas_mvp.models import reservation_feedback  # noqa: F401
    # A2：AI 對話狀態 + 月度計量。
    from saas_mvp.models import line_conversation, ai_usage  # noqa: F401
    # F1：統一稽核日誌。
    from saas_mvp.models import audit_log  # noqa: F401
    # C2：電子發票。
    from saas_mvp.models import invoice  # noqa: F401
    # E1：Google Calendar 授權憑證。
    from saas_mvp.models import tenant_gcal_credential  # noqa: F401
    # D4：AI 答不好的問題（FAQ 自學）。
    from saas_mvp.models import ai_unanswered_question  # noqa: F401
    # PHASE 5：Flex 圖文選單卡片（menu 先於 card，FK 依賴）。
    from saas_mvp.models import flex_menu, flex_menu_card  # noqa: F401
    # LINE 自動回覆規則（依賴 flex_menu FK）。
    from saas_mvp.models import auto_reply_rule  # noqa: F401
    # 月度推播額度計量（跨提醒/異動通知/行銷 push 路徑共用）。
    from saas_mvp.models import push_usage  # noqa: F401
    # 後台 LINE 客服對話紀錄（收/發）。
    from saas_mvp.models import line_message  # noqa: F401


def init_db() -> None:
    """初始化/升級 schema（冪等）——delegate 到 Alembic 遷移三分支。

    保留此函式名：app.py lifespan 與 tests/conftest.py 的 no-op 替換都
    以 `init_db` 為錨點。實際邏輯見 ops/migrate.run_migrations()：
    全新 DB → upgrade head；legacy DB → legacy_init_db() 收斂 + stamp；
    已納管 → upgrade head。容器部署由 entrypoint 先跑
    `python -m saas_mvp.ops.migrate`，lifespan 再跑一次也只是冪等 no-op。
    """
    from saas_mvp.ops.migrate import run_migrations  # 延遲 import 防循環

    run_migrations()


def legacy_init_db() -> None:
    """（過渡保留）Alembic 導入前的建表 + 手寫遷移。

    僅供 ops/migrate 對「未納管的 legacy DB」做一次性收斂到 baseline
    等價 schema；新的 schema 變更一律寫 Alembic revision，
    **不要再新增 _migrate_* 函式**。待所有部署皆 stamp 後可整段刪除。
    """
    # import models so their metadata is registered
    import_all_models()
    Base.metadata.create_all(bind=engine)

    # 無 Alembic 環境的輕量 schema 演進：補既有 DB 缺少的新欄位。
    # 新環境由 create_all 自動建立，此處 inspect 後即略過。
    _migrate_add_line_bot_user_id()
    _migrate_add_line_credential_status_fields()
    _migrate_backfill_char_count()
    _migrate_add_tenant_store_type()
    _migrate_add_line_bot_mode()
    _migrate_add_rich_menu_fields()
    _migrate_add_customer_membership()
    _migrate_add_reservation_attended()
    _migrate_add_order_merchant_trade_no()
    _migrate_add_location_id()
    _migrate_add_reservation_staff_id()
    _migrate_add_reservation_service_id()
    _migrate_add_tenant_ics_token()
    _migrate_add_customer_ics_token()
    _migrate_add_user_oauth()
    _migrate_add_customer_birthday()
    _migrate_add_customer_blacklist()
    _migrate_add_coupon_order_fields()
    _migrate_add_tenant_reminder_hours()
    _migrate_add_profile_announcement()
    _migrate_add_reservation_customer_confirmed()


def _add_column_if_missing(table: str, column: str, ddl: str) -> None:
    """冪等補欄：表存在且欄位缺少時 ALTER TABLE ADD COLUMN。失敗僅記 warning。

    ``ddl`` 為欄位型別與預設片段，例如 ``"INTEGER NOT NULL DEFAULT 0"``。
    """
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        _log.info("migrated: added %s.%s column", table, column)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s.%s skipped due to error: %s",
            table, column, type(exc).__name__,
        )


def _migrate_add_reservation_customer_confirmed() -> None:
    """為既有 booking_reservations 補 customer_confirmed_at 欄
    （提醒訊息「確認出席」互動；NULL=未確認，向後相容）。"""
    _add_column_if_missing(
        "booking_reservations", "customer_confirmed_at", "TIMESTAMP"
    )


def _migrate_add_customer_blacklist() -> None:
    """為既有 booking_customers 表補上黑名單欄位（blacklisted + blacklist_reason）。

    blacklisted 帶 NOT NULL DEFAULT FALSE，既有顧客自動回填 false（零影響）；
    blacklist_reason 為 nullable 備註。只做 ADD COLUMN，失敗僅記 warning，不阻擋啟動。
    （upstream 合併註記：Alembic 納管後的對應 revision 為 0004——legacy DB 走
    本函式收斂，fresh/managed DB 走 revision;兩者冪等互不衝突。）
    """
    _add_column_if_missing(
        "booking_customers", "blacklisted", "BOOLEAN NOT NULL DEFAULT FALSE"
    )
    _add_column_if_missing("booking_customers", "blacklist_reason", "VARCHAR(255)")


def _migrate_add_coupon_order_fields() -> None:
    """票券四類型 + 訂單套券（對標 vibeaico）所需的既有 DB 補欄。"""
    _add_column_if_missing("coupons", "min_spend_cents", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("orders", "discount_cents", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("orders", "coupon_code", "VARCHAR(64)")
    _add_column_if_missing("coupon_redemptions", "order_id", "INTEGER")


def _migrate_add_tenant_reminder_hours() -> None:
    """自訂提醒時間（小時）：為既有 tenants 表補上 reminder_hours_before 欄位。"""
    _add_column_if_missing("tenants", "reminder_hours_before", "INTEGER")


def _migrate_add_profile_announcement() -> None:
    """公開頁公告：為既有 business_profiles 表補上 announcement 欄位。"""
    _add_column_if_missing("business_profiles", "announcement", "TEXT")


def _migrate_add_customer_birthday() -> None:
    """為既有 booking_customers 表補上 nullable birthday 欄位（PHASE 4-1，向後相容）。

    只做 ADD COLUMN，不回填；未填生日的顧客為 NULL。失敗僅記 warning，不阻擋啟動。
    """
    table = "booking_customers"
    column = "birthday"
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} DATE"))
        _log.info("migrated: added %s.%s column", table, column)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s.%s skipped due to error: %s",
            table, column, type(exc).__name__,
        )


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


def _migrate_add_rich_menu_fields() -> None:
    """為既有 line_channel_configs 表補上 Rich Menu 欄位（皆 nullable，向後相容）。

    只做 ADD COLUMN，不回填；未套用 rich menu 的列為 NULL。
    任何失敗僅記 warning，不阻擋服務啟動。
    """
    table = "line_channel_configs"
    columns = {
        "rich_menu_id": "VARCHAR(64)",
        "rich_menu_template": "VARCHAR(32)",
        "rich_menu_theme": "VARCHAR(32)",
    }
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        missing = {n: t for n, t in columns.items() if n not in existing}
        if not missing:
            return
        with engine.begin() as conn:
            for name, col_type in missing.items():
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}"))
        _log.info("migrated: added %s columns to %s", ", ".join(sorted(missing)), table)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s rich menu fields skipped due to error: %s",
            table, type(exc).__name__,
        )


def _migrate_add_customer_membership() -> None:
    """為既有 booking_customers 表補上會員集點欄位（向後相容）。

    points_balance 帶 NOT NULL DEFAULT 0、tier 帶 NOT NULL DEFAULT 'regular'，
    既有列自動回填。失敗僅記 warning，不阻擋啟動。
    """
    table = "booking_customers"
    columns = {
        "points_balance": "INTEGER NOT NULL DEFAULT 0",
        "tier": "VARCHAR(16) NOT NULL DEFAULT 'regular'",
    }
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        missing = {n: t for n, t in columns.items() if n not in existing}
        if not missing:
            return
        with engine.begin() as conn:
            for name, col_type in missing.items():
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}"))
        _log.info("migrated: added %s columns to %s", ", ".join(sorted(missing)), table)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s membership fields skipped due to error: %s",
            table, type(exc).__name__,
        )


def _migrate_add_reservation_attended() -> None:
    """為既有 booking_reservations 表補上 attended 欄位（nullable，向後相容）。

    NULL = 未標記到場；只 ADD COLUMN、不回填。失敗僅記 warning。
    """
    table = "booking_reservations"
    column = "attended"
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} BOOLEAN"))
        _log.info("migrated: added %s.%s column", table, column)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s.%s skipped due to error: %s",
            table, column, type(exc).__name__,
        )


def _migrate_add_order_merchant_trade_no() -> None:
    """為既有 orders 表補上 merchant_trade_no 欄位 + unique index（向後相容）。

    拆 ADD COLUMN 與 CREATE UNIQUE INDEX 兩步（SQLite 老版 inline UNIQUE 不穩）；
    新欄初始全 NULL，SQLite unique index 允許多 NULL。失敗僅 warning。
    """
    table = "orders"
    column = "merchant_trade_no"
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR(20)"))
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_orders_merchant_trade_no "
                    f"ON {table} ({column})"
                )
            )
        _log.info("migrated: added %s.%s column + unique index", table, column)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s.%s skipped due to error: %s",
            table, column, type(exc).__name__,
        )


def _migrate_add_location_id() -> None:
    """為既有預約相關表補上 nullable location_id 欄位（多分店，向後相容）。

    對 booking_slots / booking_reservations / booking_customers / orders 各加
    一個 nullable INTEGER location_id（NULL = 未指定分店 / 任意，既有列零影響）。
    每張表獨立 inspect-guard；任何失敗僅 warning，不阻擋啟動。
    """
    column = "location_id"
    for table in (
        "booking_slots",
        "booking_reservations",
        "booking_customers",
        "orders",
    ):
        try:
            inspector = inspect(engine)
            if table not in inspector.get_table_names():
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            if column in existing:
                continue
            with engine.begin() as conn:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER")
                )
            _log.info("migrated: added %s.%s column", table, column)
        except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
            _log.warning(
                "schema migration for %s.%s skipped due to error: %s",
                table, column, type(exc).__name__,
            )


def _migrate_add_reservation_staff_id() -> None:
    """為既有 booking_reservations 表補上 nullable staff_id 欄位 + 非 unique index。

    拆 ADD COLUMN 與 CREATE INDEX 兩步；新欄初始全 NULL。失敗僅 warning。
    """
    table = "booking_reservations"
    column = "staff_id"
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_reservation_staff_id "
                    f"ON {table} ({column})"
                )
            )
        _log.info("migrated: added %s.%s column + index", table, column)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s.%s skipped due to error: %s",
            table, column, type(exc).__name__,
        )


def _migrate_add_reservation_service_id() -> None:
    """為既有 booking_reservations 表補上 nullable service_id 欄位 + 非 unique index。

    拆 ADD COLUMN 與 CREATE INDEX 兩步；新欄初始全 NULL。失敗僅 warning。
    """
    table = "booking_reservations"
    column = "service_id"
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_reservation_service_id "
                    f"ON {table} ({column})"
                )
            )
        _log.info("migrated: added %s.%s column + index", table, column)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s.%s skipped due to error: %s",
            table, column, type(exc).__name__,
        )


def _migrate_add_tenant_ics_token() -> None:
    """為既有 tenants 表補上 nullable ics_token 欄位 + unique index（行事曆 feed）。

    拆 ADD COLUMN 與 CREATE UNIQUE INDEX 兩步（SQLite 老版 inline UNIQUE 不穩）；
    新欄初始全 NULL，SQLite unique index 允許多 NULL。失敗僅 warning。
    """
    table = "tenants"
    column = "ics_token"
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR(64)"))
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_tenants_ics_token "
                    f"ON {table} ({column})"
                )
            )
        _log.info("migrated: added %s.%s column + unique index", table, column)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s.%s skipped due to error: %s",
            table, column, type(exc).__name__,
        )


def _migrate_add_customer_ics_token() -> None:
    """為既有 booking_customers 表補上 nullable ics_token 欄位 + unique index。

    拆 ADD COLUMN 與 CREATE UNIQUE INDEX 兩步；新欄初始全 NULL。失敗僅 warning。
    """
    table = "booking_customers"
    column = "ics_token"
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR(64)"))
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_booking_customers_ics_token "
                    f"ON {table} ({column})"
                )
            )
        _log.info("migrated: added %s.%s column + unique index", table, column)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s.%s skipped due to error: %s",
            table, column, type(exc).__name__,
        )


def _migrate_add_user_oauth() -> None:
    """為既有 users 表補上 OAuth 身分欄位（皆 nullable，向後相容）。

    只做 ADD COLUMN，不回填；密碼註冊用戶為 NULL。失敗僅記 warning，不阻擋啟動。
    """
    table = "users"
    columns = {
        "oauth_provider": "VARCHAR(16)",
        "oauth_subject": "VARCHAR(128)",
    }
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        missing = {n: t for n, t in columns.items() if n not in existing}
        if not missing:
            return
        with engine.begin() as conn:
            for name, col_type in missing.items():
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}"))
        _log.info("migrated: added %s columns to %s", ", ".join(sorted(missing)), table)
    except Exception as exc:  # noqa: BLE001 — 遷移失敗不得阻擋啟動
        _log.warning(
            "schema migration for %s oauth fields skipped due to error: %s",
            table, type(exc).__name__,
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
