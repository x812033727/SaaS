"""QA — 任務 #4：無 Alembic 場景的舊 DB 相容 try-ALTER 補欄位驗證。

覆蓋：
  ① 舊 DB（line_channel_configs 表缺 line_bot_user_id 欄）→ migration 補上欄位 + unique index
  ② 既有資料（NULL）migration 後仍可讀寫、unique index 允許多 NULL
  ③ 冪等：欄位已存在 → 直接略過、不報錯、可重複呼叫
  ④ 表不存在 → 略過、不爆
  ⑤ 失敗（如壞掉的 engine）→ 僅 warning，不拋例外、不阻擋啟動
  ⑥ unique index 確實生效：重複實值 → IntegrityError
"""

import logging

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

import saas_mvp.db as dbmod

TABLE = "line_channel_configs"
COLUMN = "line_bot_user_id"


def _make_old_db_engine(tmp_path):
    """建立『舊 DB』：line_channel_configs 表存在但無 line_bot_user_id 欄位。"""
    url = f"sqlite:///{tmp_path}/old.db"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE {TABLE} ("
                "id INTEGER PRIMARY KEY, "
                "tenant_id INTEGER NOT NULL UNIQUE, "
                "channel_secret_enc BLOB NOT NULL, "
                "access_token_enc BLOB NOT NULL, "
                "default_target_lang VARCHAR(16) NOT NULL DEFAULT 'zh-TW'"
                ")"
            )
        )
        # 既有兩筆資料（模擬升級前已有租戶）
        conn.execute(
            text(
                f"INSERT INTO {TABLE} "
                "(id, tenant_id, channel_secret_enc, access_token_enc) "
                "VALUES (1, 100, x'00', x'01'), (2, 200, x'02', x'03')"
            )
        )
    return eng


@pytest.fixture
def patch_engine(monkeypatch):
    """讓 migration 針對指定 engine 執行（取代 module-level global engine）。"""
    def _patch(eng):
        monkeypatch.setattr(dbmod, "engine", eng)
    return _patch


# ── ① 舊 DB → 補欄位 ──────────────────────────────────────────────────────────

def test_migrate_adds_column_to_old_db(tmp_path, patch_engine):
    eng = _make_old_db_engine(tmp_path)
    patch_engine(eng)

    # migration 前：欄位不存在
    cols_before = {c["name"] for c in inspect(eng).get_columns(TABLE)}
    assert COLUMN not in cols_before, "前置條件：舊 DB 不該有此欄位"

    dbmod._migrate_add_line_bot_user_id()

    # migration 後：欄位存在
    cols_after = {c["name"] for c in inspect(eng).get_columns(TABLE)}
    assert COLUMN in cols_after, "migration 應補上 line_bot_user_id 欄位"

    # unique index 存在
    idx_names = {ix["name"] for ix in inspect(eng).get_indexes(TABLE)}
    assert "ix_lcfg_lbuid" in idx_names, "應建立 unique index ix_lcfg_lbuid"
    target_idx = next(
        ix for ix in inspect(eng).get_indexes(TABLE) if ix["name"] == "ix_lcfg_lbuid"
    )
    assert target_idx["unique"]  # SQLAlchemy 回 1/True 皆視為 unique
    assert target_idx["column_names"] == [COLUMN]


# ── ② 既有 NULL 資料仍可讀寫，多 NULL 允許 ────────────────────────────────────

def test_existing_rows_readable_and_writable_after_migration(tmp_path, patch_engine):
    eng = _make_old_db_engine(tmp_path)
    patch_engine(eng)
    dbmod._migrate_add_line_bot_user_id()

    with eng.connect() as conn:
        rows = conn.execute(
            text(f"SELECT id, {COLUMN} FROM {TABLE} ORDER BY id")
        ).fetchall()
    # 既有兩筆資料新欄位皆為 NULL（多 NULL 不違反 unique index）
    assert rows == [(1, None), (2, None)]

    # 可寫入實值
    with eng.begin() as conn:
        conn.execute(
            text(f"UPDATE {TABLE} SET {COLUMN} = :u WHERE id = 1"),
            {"u": "U" + "a" * 32},
        )
    with eng.connect() as conn:
        val = conn.execute(
            text(f"SELECT {COLUMN} FROM {TABLE} WHERE id = 1")
        ).scalar()
    assert val == "U" + "a" * 32


# ── ③ 冪等：欄位已存在 → 略過、可重複呼叫 ─────────────────────────────────────

def test_idempotent_when_column_exists(tmp_path, patch_engine):
    eng = _make_old_db_engine(tmp_path)
    patch_engine(eng)

    # 連續呼叫三次都不該拋例外
    dbmod._migrate_add_line_bot_user_id()
    dbmod._migrate_add_line_bot_user_id()
    dbmod._migrate_add_line_bot_user_id()

    cols = {c["name"] for c in inspect(eng).get_columns(TABLE)}
    assert COLUMN in cols
    # 欄位不應被重複加（仍是單一欄位）
    count = sum(1 for c in inspect(eng).get_columns(TABLE) if c["name"] == COLUMN)
    assert count == 1


# ── ④ 表不存在 → 略過不爆 ─────────────────────────────────────────────────────

def test_no_error_when_table_absent(tmp_path, patch_engine):
    url = f"sqlite:///{tmp_path}/empty.db"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    # 建一個無關的空 DB（無 line_channel_configs 表）
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))
    patch_engine(eng)

    # 不該拋例外
    dbmod._migrate_add_line_bot_user_id()

    assert TABLE not in inspect(eng).get_table_names()


# ── ⑤ 失敗 → 僅 warning，不拋例外（無 Alembic 場景不爆）─────────────────────────

def test_migration_failure_is_swallowed_and_warns(monkeypatch, caplog):
    """模擬 inspect/engine 操作拋例外 → migration 必須吞掉、只記 warning。"""
    class BoomEngine:
        def __getattr__(self, name):
            raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(dbmod, "engine", BoomEngine())

    with caplog.at_level(logging.WARNING, logger="saas_mvp.db"):
        # 關鍵：不得拋例外（否則 init_db 會阻擋啟動）
        dbmod._migrate_add_line_bot_user_id()

    assert any(
        rec.levelno >= logging.WARNING for rec in caplog.records
    ), "失敗時應記錄 warning"


# ── ⑥ unique index 真的生效：重複實值被擋 ────────────────────────────────────

def test_unique_index_enforced(tmp_path, patch_engine):
    eng = _make_old_db_engine(tmp_path)
    patch_engine(eng)
    dbmod._migrate_add_line_bot_user_id()

    dup = "U" + "b" * 32
    with eng.begin() as conn:
        conn.execute(
            text(f"UPDATE {TABLE} SET {COLUMN} = :u WHERE id = 1"), {"u": dup}
        )
    # 第二筆設成相同實值 → 違反 unique index
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(
                text(f"UPDATE {TABLE} SET {COLUMN} = :u WHERE id = 2"), {"u": dup}
            )


# ── ⑦ 新環境：create_all 已含欄位 → migration 偵測到存在即略過（冪等、不重複 ALTER）──

def test_new_env_create_all_then_migrate_is_noop(tmp_path, patch_engine):
    """真實新環境：用 Base.metadata.create_all 建表（model 已宣告 line_bot_user_id）
    → create_all 自動含欄位 → migration 偵測到存在即略過、不重複 ALTER。

    以真實 create_all（而非手動 CREATE TABLE）驗證，確保「新環境 create_all
    自動包含」的驗收前提與生產行為一致。
    """
    # 確保完整 model metadata 已註冊（line_channel_configs 對 tenants 有 FK，
    # 故須一併匯入被依賴的 model，create_all 才能建出含 FK 的表）。
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

    url = f"sqlite:///{tmp_path}/new.db"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    dbmod.Base.metadata.create_all(bind=eng)
    patch_engine(eng)

    # 前置：create_all 已自動建立欄位（驗證 model 宣告生效）
    cols_before = {c["name"] for c in inspect(eng).get_columns(TABLE)}
    assert COLUMN in cols_before, "model 宣告後 create_all 應自動含 line_bot_user_id"

    # 不該拋例外、不該重建欄位（偵測存在即略過）
    dbmod._migrate_add_line_bot_user_id()
    cols = [c["name"] for c in inspect(eng).get_columns(TABLE)]
    assert cols.count(COLUMN) == 1
