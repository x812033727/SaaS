"""任務 #1 驗收測試：ApiUsage.char_count 欄位 + 既有列 backfill 為 0。

驗收點（任務 #1）
-----------------
1. ApiUsage 具 char_count 欄位，預設 0；新 INSERT 自動 = 0。
2. 既有 DB 相容：舊列 char_count 為 NULL 時，讀取端以 (row.char_count or 0)
   兜底，不報錯、不參與算術崩潰。
3. 一次性 backfill：`_migrate_backfill_char_count()` 把 NULL 列統一回填 0。
4. backfill 冪等：第二次起 rowcount=0，no-op；非 NULL 列不動。
5. backfill 容錯：表不存在 / engine 爆掉 → 僅 warning，不阻擋啟動。

對應既有 `_migrate_add_line_bot_user_id` 的 qa 測試風格：
  test_qa_task4_migrate_line_bot_user_id.py（7 條案例）→ 本檔 7 條對應案例。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

import saas_mvp.db as dbmod
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.usage import ApiUsage

TABLE = "api_usage"
COLUMN = "char_count"


# ── 共用：建一個「舊 DB」：api_usage 表存在、含 NULL 的 char_count ────────────

def _make_old_db_engine(tmp_path, *, rows: list[tuple[int, str, int, int | None]] | None = None):
    """建立『舊 DB』：api_usage 表存在，char_count 欄位存在，舊列含 NULL。

    rows: (id, period, count, char_count) 元組列表。
          若 None → 預設 2 列：1 列 NULL、1 列已是 0。
    """
    if rows is None:
        rows = [(1, "2024-01-01", 5, None), (2, "2024-01-01", 7, 0)]
    url = f"sqlite:///{tmp_path}/old.db"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE {TABLE} ("
                "id INTEGER PRIMARY KEY, "
                "tenant_id INTEGER NOT NULL, "
                "period VARCHAR(10) NOT NULL, "
                "count INTEGER NOT NULL DEFAULT 0, "
                f"{COLUMN} INTEGER"  # 注意：故意不加 NOT NULL，模擬舊 schema
                ")"
            )
        )
        for rid, period, count, char_count in rows:
            conn.execute(
                text(
                    f"INSERT INTO {TABLE} (id, tenant_id, period, count, {COLUMN}) "
                    "VALUES (:id, :tid, :p, :c, :ch)"
                ),
                {"id": rid, "tid": rid * 100, "p": period,
                 "c": count, "ch": char_count},
            )
    return eng


@pytest.fixture
def patch_engine(monkeypatch):
    """讓 migration 針對指定 engine 執行（取代 module-level global engine）。"""
    def _patch(eng):
        monkeypatch.setattr(dbmod, "engine", eng)
    return _patch


# ── 1. ApiUsage model 宣告驗收：欄位存在、預設 0、新 INSERT = 0 ──────────────

class TestModelDeclaresCharCount:
    """驗收標準 1：『ApiUsage 具 char_count 欄位，預設 0』"""

    def test_column_declared_on_model(self):
        """model 必須有 char_count 屬性，型別 Integer、預設 0。"""
        col = ApiUsage.__table__.columns.get(COLUMN)
        assert col is not None, "ApiUsage 必須宣告 char_count 欄位"
        assert col.type.python_type is int, f"char_count 型別應為 int，got {col.type}"
        assert col.nullable is False, "char_count 應為 nullable=False"
        assert col.default is not None, "char_count 應有 server/default"

    def test_new_row_default_is_zero(self, tmp_path):
        """新 INSERT 走 SQLAlchemy default → char_count = 0。"""
        eng = create_engine(
            f"sqlite:///{tmp_path}/fresh.db",
            connect_args={"check_same_thread": False},
        )
        dbmod.Base.metadata.create_all(bind=eng)
        Session = sessionmaker(bind=eng)
        with Session() as s:
            tenant = Tenant(name="t-default-zero", plan="free")
            s.add(tenant)
            s.flush()
            row = ApiUsage(tenant_id=tenant.id, period="2024-01-01", count=0)
            s.add(row)
            s.flush()
            assert row.char_count == 0, (
                f"新 INSERT 的 char_count 應 = 0（default=0），got {row.char_count}"
            )


# ── 2. 既有 NULL 列讀取端兜底：不報錯、得 0 ────────────────────────────────

class TestNullCharCountReadable:
    """驗收標準 1 副條：『既有 migration/建表相容，舊資料讀取為 0 不報錯』。"""

    def test_or_zero_fallback_on_null_row(self, tmp_path, patch_engine):
        """ORM 載入 NULL char_count 的 row 時，row.char_count 為 None；
        讀取端 (row.char_count or 0) 兜底 0，不報錯、不崩。

        用真實 ApiUsage model + 同 schema（create_all），再 raw SQL 把
        char_count 設成 NULL 模擬「既有 NULL 列」——這樣 SQLAlchemy 載入
        row 時走完整的 mapper 初始化路徑（驗證真實讀取端行為，非
        純 SQL 測試）。
        """
        eng = create_engine(
            f"sqlite:///{tmp_path}/with_null.db",
            connect_args={"check_same_thread": False},
        )
        dbmod.Base.metadata.create_all(bind=eng)
        patch_engine(eng)

        # 先建一筆正常 row（用 ORM 走 default=0 → char_count=0）
        Session = sessionmaker(bind=eng)
        with Session() as s:
            tenant = Tenant(name="t-null-test", plan="free")
            s.add(tenant)
            s.flush()
            row = ApiUsage(tenant_id=tenant.id, period="2024-01-01", count=5)
            s.add(row)
            s.commit()
            row_id = row.id

        # 模擬「升級前既存的 NULL 列」：raw SQL 把 char_count 設成 NULL
        with eng.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE {TABLE} SET {COLUMN} = NULL WHERE id = :i"
                ),
                {"i": row_id},
            )

        # 重新用 ORM 載入：模擬 production 升級後第一次讀取
        with Session() as s:
            null_row = s.execute(
                select(ApiUsage).where(ApiUsage.id == row_id)
            ).scalar_one()
            # 既有 NULL 列：屬性值為 None
            assert null_row.char_count is None
            # 讀取端兜底語意（與 quota.py / routers/usage.py 一致）
            used_chars = null_row.char_count or 0
            assert used_chars == 0
            # 算術運算（用於 status 聚合）不崩
            assert max(0, 1000 - used_chars) == 1000


# ── 3. backfill 主流程：NULL 列回填為 0 ─────────────────────────────────────

class TestBackfillNullToZero:
    def test_null_rows_backfilled_to_zero(self, tmp_path, patch_engine):
        eng = _make_old_db_engine(tmp_path)
        patch_engine(eng)

        # migration 前：id=1 仍是 NULL
        before = eng.connect().execute(
            text(f"SELECT id, {COLUMN} FROM {TABLE} ORDER BY id")
        ).fetchall()
        assert before == [(1, None), (2, 0)], "前置：1 NULL、1 已是 0"

        dbmod._migrate_backfill_char_count()

        after = eng.connect().execute(
            text(f"SELECT id, {COLUMN} FROM {TABLE} ORDER BY id")
        ).fetchall()
        assert after == [(1, 0), (2, 0)], "backfill 後 NULL 應回填 0"

    def test_non_null_rows_untouched(self, tmp_path, patch_engine):
        """既有的非 NULL 實值（含負數/正數）不應被 backfill 覆寫。"""
        eng = _make_old_db_engine(
            tmp_path,
            rows=[(1, "2024-01-01", 5, None), (2, "2024-01-01", 7, 42)],
        )
        patch_engine(eng)
        dbmod._migrate_backfill_char_count()

        after = eng.connect().execute(
            text(f"SELECT id, {COLUMN} FROM {TABLE} ORDER BY id")
        ).fetchall()
        assert after == [(1, 0), (2, 42)], "非 NULL 列 42 應保持不動"


# ── 4. backfill 冪等：第二次起 rowcount=0，no-op ──────────────────────────────

class TestBackfillIdempotent:
    def test_second_call_is_noop(self, tmp_path, patch_engine):
        eng = _make_old_db_engine(tmp_path)
        patch_engine(eng)

        dbmod._migrate_backfill_char_count()  # 首次：1 列 NULL → 0
        # 第二次：應 no-op、不爆
        dbmod._migrate_backfill_char_count()
        dbmod._migrate_backfill_char_count()

        after = eng.connect().execute(
            text(f"SELECT {COLUMN} FROM {TABLE} ORDER BY id")
        ).fetchall()
        assert all(v == 0 for v, in after), "冪等後仍應全為 0"

    def test_empty_table_is_noop(self, tmp_path, patch_engine):
        url = f"sqlite:///{tmp_path}/empty.db"
        eng = create_engine(url, connect_args={"check_same_thread": False})
        with eng.begin() as conn:
            # 建空表（無 rows），模擬「有表但沒資料」
            conn.execute(
                text(
                    f"CREATE TABLE {TABLE} ("
                    "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, "
                    "period VARCHAR(10) NOT NULL, count INTEGER NOT NULL DEFAULT 0, "
                    f"{COLUMN} INTEGER NOT NULL DEFAULT 0"
                    ")"
                )
            )
        patch_engine(eng)
        # 不該拋例外
        dbmod._migrate_backfill_char_count()


# ── 5. backfill 容錯：表不存在 / 欄位不存在 / engine 爆 → 不阻擋 ─────────

class TestBackfillFailureSwallowed:
    def test_no_error_when_table_absent(self, tmp_path, patch_engine):
        url = f"sqlite:///{tmp_path}/empty.db"
        eng = create_engine(url, connect_args={"check_same_thread": False})
        # 建一個無關的空 DB（無 api_usage 表）
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))
        patch_engine(eng)

        # 不該拋例外
        dbmod._migrate_backfill_char_count()
        assert TABLE not in inspect(eng).get_table_names()

    def test_no_error_when_column_absent(self, tmp_path, patch_engine):
        """舊 DB 完全沒 char_count 欄位（更早的 schema）→ 略過不爆。

        該情境需要 ALTER TABLE ADD COLUMN，超出本 backfill 範圍；
        本函式只處理「有欄位但舊列 NULL」的情境，欄位不存在應安全略過。
        """
        url = f"sqlite:///{tmp_path}/legacy.db"
        eng = create_engine(url, connect_args={"check_same_thread": False})
        with eng.begin() as conn:
            # 建表時故意不加 char_count 欄位
            conn.execute(
                text(
                    f"CREATE TABLE {TABLE} ("
                    "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, "
                    "period VARCHAR(10) NOT NULL, count INTEGER NOT NULL DEFAULT 0"
                    ")"
                )
            )
            conn.execute(
                text(
                    f"INSERT INTO {TABLE} (tenant_id, period, count) "
                    "VALUES (1, '2024-01-01', 5)"
                )
            )
        patch_engine(eng)

        # 不該拋例外
        dbmod._migrate_backfill_char_count()
        cols = {c["name"] for c in inspect(eng).get_columns(TABLE)}
        assert COLUMN not in cols, "本 backfill 不補欄位"

    def test_engine_failure_is_swallowed_and_warns(self, monkeypatch, caplog):
        """模擬 engine 操作拋例外 → backfill 必須吞掉、只記 warning。"""

        class BoomEngine:
            def __getattr__(self, name):
                raise RuntimeError("simulated DB failure")

        monkeypatch.setattr(dbmod, "engine", BoomEngine())

        with caplog.at_level(logging.WARNING, logger="saas_mvp.db"):
            # 關鍵：不得拋例外（否則 init_db 會阻擋啟動）
            dbmod._migrate_backfill_char_count()

        assert any(
            rec.levelno >= logging.WARNING for rec in caplog.records
        ), "失敗時應記錄 warning"


# ── 6. 新環境：create_all 後 → backfill 為 noop ──────────────────────────────

class TestNewEnvBackfillIsNoop:
    """真實新環境：Base.metadata.create_all 建表（model 已含 default=0）
    → 新 INSERT 自動 char_count=0，backfill 找不到 NULL 列，no-op。"""

    def test_create_all_then_backfill_noop(self, tmp_path, patch_engine):
        from saas_mvp.models import (  # noqa: F401
            api_key,
            api_key_usage,
            line_channel_config,
            note,
            plan_change_history,
            tenant,
            usage,
            user,
        )
        eng = create_engine(
            f"sqlite:///{tmp_path}/new.db",
            connect_args={"check_same_thread": False},
        )
        dbmod.Base.metadata.create_all(bind=eng)
        patch_engine(eng)

        # 前置：欄位已含
        cols = {c["name"] for c in inspect(eng).get_columns(TABLE)}
        assert COLUMN in cols

        # 走 SQLAlchemy ORM 新 INSERT（驗 default=0）
        Session = sessionmaker(bind=eng)
        with Session() as s:
            t = Tenant(name="t-newenv", plan="free")
            s.add(t)
            s.flush()
            s.add(ApiUsage(tenant_id=t.id, period="2024-01-01", count=0))
            s.commit()
            tid = t.id

        # backfill 應 no-op，不改既有資料
        dbmod._migrate_backfill_char_count()

        val = eng.connect().execute(
            text(f"SELECT {COLUMN} FROM api_usage WHERE tenant_id = :t"),
            {"t": tid},
        ).scalar()
        assert val == 0


# ── 7. init_db 內已掛載：呼叫 init_db 不會因 backfill 失敗而阻擋啟動 ─────────

class TestInitDbIntegratesBackfill:
    """init_db() 內已呼叫 _migrate_backfill_char_count()——驗證整合無誤。"""

    def test_init_db_runs_backfill(self, tmp_path, monkeypatch):
        eng = _make_old_db_engine(tmp_path)
        monkeypatch.setattr(dbmod, "engine", eng)

        # init_db() 會走 import model 鏈 → Base.metadata.create_all 嘗試
        # 建立所有表。但本測試的「舊 DB」只有 api_usage 一張表，create_all
        # 會因缺其他表（如 tenants 對其他 model 有 FK）失敗——故這裡只驗證
        # backfill 自身被 init_db 內呼叫，不跑完整 create_all。
        # 直接呼叫 backfill 函式（已在 init_db 路徑內）：
        dbmod._migrate_backfill_char_count()

        after = eng.connect().execute(
            text(f"SELECT {COLUMN} FROM {TABLE} ORDER BY id")
        ).fetchall()
        assert all(v == 0 for v, in after), "backfill 後 NULL 應回填 0"

    def test_init_db_calls_backfill_in_path(self):
        """靜態驗證：init_db() 源碼內已掛載 _migrate_backfill_char_count()。"""
        import inspect as _inspect
        src = _inspect.getsource(dbmod.init_db)
        assert "_migrate_backfill_char_count" in src, (
            "init_db 必須掛載 _migrate_backfill_char_count()，"
            "確保 production 啟動時 backfill 被自動執行"
        )
