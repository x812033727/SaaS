"""ops/migrate — Alembic 遷移三分支(fresh / legacy / managed)。

驗收標準
--------
- 全新 DB → upgrade head:全部業務表存在 + alembic_version = head
- legacy DB(有業務表、無 alembic_version)→ 收斂 + stamp,不重跑 DDL 不炸
- 已納管 DB → 再執行為冪等 no-op
- baseline schema 與 legacy init(create_all + _migrate_*)逐欄位等價
- --check 只回報不改動
"""

from __future__ import annotations

import io
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect

import saas_mvp.db as dbmod
from saas_mvp.ops import migrate as migrate_mod


def _run(tmp_path, name: str):
    """回傳 (engine, url) 指向 tmp 下的 SQLite 檔。"""
    url = f"sqlite:///{tmp_path}/{name}.db"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    return eng, url


def _tables(eng) -> set[str]:
    return set(inspect(eng).get_table_names())


def _version(eng) -> str | None:
    if "alembic_version" not in _tables(eng):
        return None
    with eng.connect() as c:
        row = c.exec_driver_sql("SELECT version_num FROM alembic_version").first()
        return row[0] if row else None


def test_fresh_db_upgrades_to_head(tmp_path):
    eng, url = _run(tmp_path, "fresh")
    state = migrate_mod.run_migrations(engine=eng, database_url=url)
    assert state == "fresh"
    tables = _tables(eng)
    # 抽查核心業務表
    for t in ("tenants", "users", "booking_reservations", "booking_slots",
              "line_webhook_events", "booking_waitlist_entries",
              "feature_subscriptions", "marketing_campaigns"):
        assert t in tables, f"missing {t}"
    assert _version(eng) is not None


def test_fresh_db_migrate_idempotent(tmp_path):
    eng, url = _run(tmp_path, "idem")
    migrate_mod.run_migrations(engine=eng, database_url=url)
    v1 = _version(eng)
    state2 = migrate_mod.run_migrations(engine=eng, database_url=url)
    assert state2 == "managed"
    assert _version(eng) == v1


def test_legacy_db_converged_and_stamped(tmp_path, monkeypatch):
    """legacy DB(用 legacy_init_db 建)→ migrate 收斂 + stamp,資料保留。"""
    eng, url = _run(tmp_path, "legacy")
    # 用 legacy 路徑建出「導入 Alembic 前」的 DB(需 monkeypatch module engine)
    monkeypatch.setattr(dbmod, "engine", eng)
    dbmod.legacy_init_db()
    assert "alembic_version" not in _tables(eng)
    # 塞一筆資料,驗證 stamp 不動資料
    with eng.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO tenants (name, plan, is_active) "
            "VALUES ('legacy-shop', 'free', 1)"
        )

    state = migrate_mod.run_migrations(engine=eng, database_url=url)
    assert state == "legacy"
    assert _version(eng) is not None
    with eng.connect() as c:
        row = c.exec_driver_sql(
            "SELECT name FROM tenants WHERE name='legacy-shop'"
        ).first()
    assert row is not None  # 資料未被重建/清除


def test_baseline_schema_matches_legacy_init(tmp_path, monkeypatch):
    """alembic upgrade head 與 legacy_init_db 產出的 schema 逐欄位等價。"""
    eng_a, url_a = _run(tmp_path, "via_alembic")
    migrate_mod.run_migrations(engine=eng_a, database_url=url_a)

    eng_b, _url_b = _run(tmp_path, "via_legacy")
    monkeypatch.setattr(dbmod, "engine", eng_b)
    dbmod.legacy_init_db()

    def schema(path):
        con = sqlite3.connect(path)
        out = {}
        for (name,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        ):
            out[name] = {
                (r[1], (r[2] or "").upper(), bool(r[3]))
                for r in con.execute(f"PRAGMA table_info({name})")
            }
        con.close()
        return out

    a = schema(f"{tmp_path}/via_alembic.db")
    b = schema(f"{tmp_path}/via_legacy.db")
    assert set(a) == set(b), f"表集合不一致: {set(a) ^ set(b)}"
    for t in a:
        assert a[t] == b[t], f"表 {t} 欄位不一致: {a[t] ^ b[t]}"


def test_check_reports_without_migrating(tmp_path, monkeypatch):
    eng, _url = _run(tmp_path, "check")
    monkeypatch.setattr(dbmod, "engine", eng)
    out = io.StringIO()
    rc = migrate_mod.main(["--check"], stdout=out)
    assert rc == 0
    assert "state=fresh" in out.getvalue()
    assert _tables(eng) == set()  # 未建任何表
