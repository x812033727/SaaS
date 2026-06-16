"""QA Task #1 驗收測試：ApiUsage.char_count 欄位 + 既有列 backfill。

驗收標準（Task #1）
-------------------
1. ApiUsage 具 char_count 欄位，型別 int、nullable=False、default=0。
2. 新 INSERT 走 SQLAlchemy default，char_count 自動 = 0。
3. 既有 migration/建表相容：
   a. 同 schema 既有列若 char_count 為 NULL，讀取端 `(row.char_count or 0)`
      兜底 0，不報錯、不參與算術崩潰。
   b. 一次性 backfill `_migrate_backfill_char_count()` 把 NULL 列回填 0。
4. backfill 冪等：第二次起 rowcount=0，no-op。
5. backfill 容錯：表不存在 / 欄位不存在 / engine 爆 → 僅 warning，不阻擋啟動。
6. init_db() 源碼內已掛載 `_migrate_backfill_char_count()`（production 啟動時
   自動跑）。本測試直接讀 db.py 源檔字串，避開 conftest 把 init_db 改為
   no-op lambda 的影響。

範圍
----
專注 task #1（model 欄位 + 既有列 backfill）。task #2（PLAN_DAILY_CHAR_LIMITS /
has_char_quota / increment_char_usage）、task #3（webhook 兩道閘）、task #4
（/usage、/quota/status 補欄位）、task #5（end-to-end 翻譯計量）不在本檔範圍。
"""

from __future__ import annotations

import datetime
import logging
import pathlib

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

import saas_mvp.db as dbmod
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.usage import ApiUsage

# 把全部 model metadata 載入以避免 SQLAlchemy 跨模組 mapper 初始化失敗
# （Tenant 與 User 之間有 relationship，缺一會在 mapper configure 階段炸）。
from saas_mvp.models import user as _user  # noqa: F401
from saas_mvp.models import note as _note  # noqa: F401
from saas_mvp.models import api_key as _ak  # noqa: F401
from saas_mvp.models import api_key_usage as _aku  # noqa: F401
from saas_mvp.models import plan_change_history as _pch  # noqa: F401
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401
import saas_mvp.models.line_user_lang as _lul  # noqa: F401

TABLE = "api_usage"
COLUMN = "char_count"


# ── 共用 helper ─────────────────────────────────────────────────────────────

def _make_old_db_engine(tmp_path, *, rows=None):
    """建一個『舊 DB』：api_usage 表存在，char_count 欄位存在，舊列含 NULL。

    rows: (id, tenant_id, period, count, char_count) 元組列表。
          若 None → 預設 2 列：1 列 NULL、1 列已是 0。
    """
    if rows is None:
        rows = [
            (1, 100, "2024-01-01", 5, None),
            (2, 200, "2024-01-01", 7, 0),
        ]
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
                f"{COLUMN} INTEGER"  # 故意不加 NOT NULL，模擬舊 schema
                ")"
            )
        )
        for rid, tid, period, count, char_count in rows:
            conn.execute(
                text(
                    f"INSERT INTO {TABLE} (id, tenant_id, period, count, {COLUMN}) "
                    "VALUES (:id, :tid, :p, :c, :ch)"
                ),
                {"id": rid, "tid": tid, "p": period, "c": count, "ch": char_count},
            )
    return eng


@pytest.fixture
def patch_engine(monkeypatch):
    """讓 migration 函式內讀到的 module-level engine 指向測試 engine。"""
    def _patch(eng):
        monkeypatch.setattr(dbmod, "engine", eng)
    return _patch


# ── 1. Model 欄位宣告（int / nullable=False / default=0） ─────────────────

class TestModelDeclaresCharCount:
    """驗收標準 1：ApiUsage 具 char_count 欄位，預設 0"""

    def test_column_exists_on_model(self):
        col = ApiUsage.__table__.columns.get(COLUMN)
        assert col is not None, "ApiUsage 必須宣告 char_count 欄位"

    def test_column_type_is_integer(self):
        col = ApiUsage.__table__.columns[COLUMN]
        assert col.type.python_type is int, (
            f"char_count 型別應為 int，got {col.type}"
        )

    def test_column_not_nullable(self):
        col = ApiUsage.__table__.columns[COLUMN]
        assert col.nullable is False, "char_count 應為 nullable=False"

    def test_column_has_default(self):
        col = ApiUsage.__table__.columns[COLUMN]
        assert col.default is not None, "char_count 應有 default（給既有 migration 兜底）"
        # SQLAlchemy ColumnDefault.arg 應為 0
        default_arg = getattr(col.default, "arg", None)
        assert default_arg == 0, f"char_count 預設值應為 0，got {default_arg!r}"

    def test_new_row_default_is_zero(self, tmp_path):
        """新 INSERT 走 SQLAlchemy default → char_count = 0（不需 ORM 顯式指定）。"""
        eng = create_engine(
            f"sqlite:///{tmp_path}/fresh.db",
            connect_args={"check_same_thread": False},
        )
        dbmod.Base.metadata.create_all(bind=eng)
        Session = sessionmaker(bind=eng)
        with Session() as s:
            t = Tenant(name="t-default-zero", plan="free")
            s.add(t)
            s.flush()
            row = ApiUsage(
                tenant_id=t.id,
                period=datetime.date(2024, 1, 1),
                count=0,
            )
            s.add(row)
            s.commit()
            assert row.char_count == 0, (
                f"新 INSERT 的 char_count 應自動 = 0（default=0），got {row.char_count}"
            )


# ── 2. 既有 NULL 列讀取端兜底（不報錯、得 0） ─────────────────────────────

class TestNullCharCountReadable:
    """驗收標準 3：『既有 migration/建表相容，舊資料讀取為 0 不報錯』。

    走真實 ApiUsage model + create_all 建表，再 raw SQL 把 char_count 設成 NULL
    模擬「升級前既存的 NULL 列」——SQLAlchemy 載入時走完整 mapper 初始化路徑，
    驗證真實讀取端行為（不是純 SQL 測試）。
    """

    def test_orm_loads_null_as_python_none(self, tmp_path):
        """ORM 載入 NULL char_count 的 row 時，row.char_count 為 None（SQL 層）。

        模擬「升級前既存的 NULL 列」：用舊 schema 建表（無 NOT NULL 約束），
        INSERT 一筆 NULL char_count → ORM mapper 按現有 schema 載入。
        create_all 在已存在表上是 no-op，不會覆蓋舊 schema 結構。
        """
        eng = _make_old_db_engine(
            tmp_path,
            rows=[(1, 100, "2024-01-01", 5, None)],
        )
        # 註冊 metadata（不重建表）
        dbmod.Base.metadata.create_all(bind=eng)
        Session = sessionmaker(bind=eng)
        with Session() as s:
            null_row = s.execute(
                select(ApiUsage).where(ApiUsage.id == 1)
            ).scalar_one()
            # 既有 NULL 列：ORM 屬性值為 None
            assert null_row.char_count is None, "NULL 列被 ORM 載入應為 None"
            # count 欄位正常讀取（證明 ORM 載入沒崩）
            assert null_row.count == 5

    def test_or_zero_fallback_does_not_crash(self, tmp_path):
        """讀取端 (row.char_count or 0) 兜底 0，不報錯、可參與算術。

        與 quota.py / routers/usage.py 的消費端語意一致——直接以 ORM 載入
        NULL 列屬性後套 (or 0) 兜底，驗證這條語意對舊 DB 兼容。
        """
        eng = _make_old_db_engine(
            tmp_path,
            rows=[(1, 100, "2024-01-01", 5, None)],
        )
        dbmod.Base.metadata.create_all(bind=eng)
        Session = sessionmaker(bind=eng)
        with Session() as s:
            null_row = s.execute(
                select(ApiUsage).where(ApiUsage.id == 1)
            ).scalar_one()
            used_chars = null_row.char_count or 0
            assert used_chars == 0
            # 算術不崩（max(0, 1000 - 0) = 1000）
            assert max(0, 1000 - used_chars) == 1000


# ── 3. backfill 主流程：NULL → 0 ──────────────────────────────────────────

class TestBackfillNullToZero:
    def test_null_rows_backfilled_to_zero(self, tmp_path, patch_engine):
        eng = _make_old_db_engine(tmp_path)
        patch_engine(eng)

        # 前置：id=1 為 NULL
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
        """既有的非 NULL 實值（含正數）不應被 backfill 覆寫。"""
        eng = _make_old_db_engine(
            tmp_path,
            rows=[
                (1, 100, "2024-01-01", 5, None),
                (2, 200, "2024-01-01", 7, 42),
            ],
        )
        patch_engine(eng)
        dbmod._migrate_backfill_char_count()

        after = eng.connect().execute(
            text(f"SELECT id, {COLUMN} FROM {TABLE} ORDER BY id")
        ).fetchall()
        assert after == [(1, 0), (2, 42)], "非 NULL 列 42 應保持不動"

    def test_all_null_rows_all_backfilled(self, tmp_path, patch_engine):
        """多列全 NULL 的情境——全部回填，不漏列。"""
        eng = _make_old_db_engine(
            tmp_path,
            rows=[
                (1, 100, "2024-01-01", 1, None),
                (2, 200, "2024-01-01", 2, None),
                (3, 300, "2024-01-01", 3, None),
            ],
        )
        patch_engine(eng)
        dbmod._migrate_backfill_char_count()

        after = eng.connect().execute(
            text(f"SELECT id, {COLUMN} FROM {TABLE} ORDER BY id")
        ).fetchall()
        assert after == [(1, 0), (2, 0), (3, 0)], "所有 NULL 列應全數回填"


# ── 4. backfill 冪等 ──────────────────────────────────────────────────────

class TestBackfillIdempotent:
    def test_second_call_is_noop(self, tmp_path, patch_engine):
        eng = _make_old_db_engine(tmp_path)
        patch_engine(eng)

        dbmod._migrate_backfill_char_count()  # 首次：1 列 NULL → 0
        dbmod._migrate_backfill_char_count()  # 第二次：no-op
        dbmod._migrate_backfill_char_count()  # 第三次：no-op

        after = eng.connect().execute(
            text(f"SELECT {COLUMN} FROM {TABLE} ORDER BY id")
        ).fetchall()
        assert all(v == 0 for v, in after), "冪等後仍應全為 0"

    def test_empty_table_is_noop(self, tmp_path, patch_engine):
        """有表但沒資料 → backfill 不該拋例外。"""
        url = f"sqlite:///{tmp_path}/empty.db"
        eng = create_engine(url, connect_args={"check_same_thread": False})
        with eng.begin() as conn:
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


# ── 5. backfill 容錯：表不存在 / 欄位不存在 / engine 爆 ─────────────────

class TestBackfillFailureSwallowed:
    def test_no_error_when_table_absent(self, tmp_path, patch_engine):
        """DB 完全無 api_usage 表（建表前先跑 backfill）→ 不該拋例外。"""
        url = f"sqlite:///{tmp_path}/empty.db"
        eng = create_engine(url, connect_args={"check_same_thread": False})
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))
        patch_engine(eng)
        # 不該拋例外
        dbmod._migrate_backfill_char_count()
        assert TABLE not in inspect(eng).get_table_names()

    def test_no_error_when_column_absent(self, tmp_path, patch_engine):
        """更舊的 schema 完全沒 char_count 欄位 → 不該拋例外（不補欄位）。"""
        url = f"sqlite:///{tmp_path}/legacy.db"
        eng = create_engine(url, connect_args={"check_same_thread": False})
        with eng.begin() as conn:
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
        assert COLUMN not in cols, "本 backfill 不負責補欄位（那是 migration 範圍）"

    def test_engine_failure_is_swallowed_and_warns(self, monkeypatch, caplog):
        """engine 操作拋例外 → backfill 必須吞掉、僅記 warning（不得阻擋啟動）。"""

        class BoomEngine:
            def __getattr__(self, name):
                raise RuntimeError("simulated DB failure")

        monkeypatch.setattr(dbmod, "engine", BoomEngine())

        with caplog.at_level(logging.WARNING, logger="saas_mvp.db"):
            # 關鍵：不得拋例外
            dbmod._migrate_backfill_char_count()

        assert any(
            rec.levelno >= logging.WARNING for rec in caplog.records
        ), "失敗時應記錄 warning"


# ── 6. init_db() 源碼內已掛載 backfill ──────────────────────────────────

class TestInitDbSourceMountsBackfill:
    """驗收標準 3 副條：『既有 migration/建表相容』——production 啟動時
    backfill 必須被自動觸發。直讀 db.py 源檔字串驗證掛載（避開 conftest
    把 init_db 改為 lambda:None 的影響）。"""

    def test_db_py_source_calls_backfill(self):
        src_path = pathlib.Path(dbmod.__file__)
        source = src_path.read_text(encoding="utf-8")
        assert "_migrate_backfill_char_count" in source, (
            "db.py 必須定義 _migrate_backfill_char_count() 函式"
        )
        # 進一步驗證 init_db() 內有呼叫它
        # 用 str.split 抓 init_db 區段（簡化版；不依賴 import 時的 module attr）
        marker = "def init_db"
        idx = source.find(marker)
        assert idx != -1, "db.py 必須有 init_db 函式"
        # 抓到下一個 top-level def / if __name__ 為止
        end = source.find("\ndef ", idx + len(marker))
        if end == -1:
            end = source.find("\nif __name__", idx)
        if end == -1:
            end = len(source)
        init_db_body = source[idx:end]
        assert "_migrate_backfill_char_count()" in init_db_body, (
            "init_db() 內必須呼叫 _migrate_backfill_char_count()，"
            "確保 production 啟動時 backfill 被自動執行"
        )


# ── 7. 新環境 create_all → backfill 為 noop ─────────────────────────────

class TestNewEnvBackfillIsNoop:
    """新環境：Base.metadata.create_all 已含 char_count（model default=0）→
    新 INSERT 自動 = 0，backfill 找不到 NULL 列 → no-op，不改既有資料。"""

    def test_create_all_then_backfill_noop(self, tmp_path, patch_engine):
        eng = create_engine(
            f"sqlite:///{tmp_path}/new.db",
            connect_args={"check_same_thread": False},
        )
        dbmod.Base.metadata.create_all(bind=eng)
        patch_engine(eng)

        # 欄位已含
        cols = {c["name"] for c in inspect(eng).get_columns(TABLE)}
        assert COLUMN in cols

        Session = sessionmaker(bind=eng)
        with Session() as s:
            t = Tenant(name="t-newenv", plan="free")
            s.add(t)
            s.flush()
            s.add(ApiUsage(
                tenant_id=t.id,
                period=datetime.date(2024, 1, 1),
                count=0,
            ))
            s.commit()
            tid = t.id

        # backfill 應 no-op
        dbmod._migrate_backfill_char_count()

        val = eng.connect().execute(
            text(f"SELECT {COLUMN} FROM {TABLE} WHERE tenant_id = :t"),
            {"t": tid},
        ).scalar()
        assert val == 0


# ── 8. 反向對照組：未受影響的列與其他表不應被波及 ──────────────────────

class TestBackfillBlastRadius:
    """反向對照：backfill 只動 api_usage.char_count，不應影響其他欄位或表。"""

    def test_other_columns_unchanged(self, tmp_path, patch_engine):
        eng = _make_old_db_engine(tmp_path)
        patch_engine(eng)

        # 記下 count 與 tenant_id 等其他欄位的值
        before = eng.connect().execute(
            text(f"SELECT id, tenant_id, period, count FROM {TABLE} ORDER BY id")
        ).fetchall()

        dbmod._migrate_backfill_char_count()

        after = eng.connect().execute(
            text(f"SELECT id, tenant_id, period, count FROM {TABLE} ORDER BY id")
        ).fetchall()
        assert before == after, "backfill 不應動到 count / tenant_id / period"

    def test_other_tables_unchanged(self, tmp_path, patch_engine):
        """另一張表（含同名 char_count 欄位的可能性）不應被波及。"""
        url = f"sqlite:///{tmp_path}/multi.db"
        eng = create_engine(url, connect_args={"check_same_thread": False})
        with eng.begin() as conn:
            conn.execute(
                text(
                    f"CREATE TABLE {TABLE} ("
                    "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, "
                    "period VARCHAR(10) NOT NULL, count INTEGER NOT NULL DEFAULT 0, "
                    f"{COLUMN} INTEGER"
                    ")"
                )
            )
            conn.execute(
                text(
                    f"INSERT INTO {TABLE} (tenant_id, period, count, {COLUMN}) "
                    "VALUES (1, '2024-01-01', 1, NULL)"
                )
            )
            # 另一張表，故意也叫 char_count 欄位——不該被 backfill UPDATE 到
            conn.execute(
                text(
                    "CREATE TABLE api_key_usage ("
                    "id INTEGER PRIMARY KEY, api_key_id INTEGER, "
                    "tenant_id INTEGER, period VARCHAR(10), count INTEGER, "
                    f"{COLUMN} INTEGER"
                    ")"
                )
            )
            conn.execute(
                text(
                    f"INSERT INTO api_key_usage (api_key_id, tenant_id, period, count, {COLUMN}) "
                    "VALUES (1, 1, '2024-01-01', 1, NULL)"
                )
            )
        patch_engine(eng)

        dbmod._migrate_backfill_char_count()

        # api_usage.char_count NULL → 0
        v = eng.connect().execute(
            text(f"SELECT {COLUMN} FROM {TABLE} WHERE id = 1")
        ).scalar()
        assert v == 0

        # api_key_usage.char_count 應仍是 NULL（不該被波及）
        v2 = eng.connect().execute(
            text(f"SELECT {COLUMN} FROM api_key_usage WHERE id = 1")
        ).scalar()
        assert v2 is None, "backfill 應只動 api_usage 表，不該 UPDATE 其他表"
