"""migration 測試：舊 orders 缺 merchant_trade_no → 補欄 + unique index。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

import saas_mvp.db as dbmod

TABLE = "orders"
COLUMN = "merchant_trade_no"


def _make_old_db(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE {TABLE} ("
            "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, customer_id INTEGER, "
            "line_user_id VARCHAR(64), status VARCHAR(16) NOT NULL DEFAULT 'pending', "
            "total_cents INTEGER NOT NULL DEFAULT 0, currency VARCHAR(8) NOT NULL DEFAULT 'TWD', "
            "created_at DATETIME, updated_at DATETIME, paid_at DATETIME)"
        ))
        conn.execute(text(f"INSERT INTO {TABLE} (id, tenant_id, total_cents) VALUES (1, 1, 100)"))
    return eng


def test_migrate_adds_column_and_unique_index(tmp_path, monkeypatch):
    eng = _make_old_db(tmp_path)
    monkeypatch.setattr(dbmod, "engine", eng)
    assert COLUMN not in {c["name"] for c in inspect(eng).get_columns(TABLE)}

    dbmod._migrate_add_order_merchant_trade_no()

    assert COLUMN in {c["name"] for c in inspect(eng).get_columns(TABLE)}
    # unique index 生效：重複實值 → IntegrityError（多 NULL 允許）
    with eng.begin() as conn:
        conn.execute(text(f"UPDATE {TABLE} SET {COLUMN}='OD1' WHERE id=1"))
        conn.execute(text(f"INSERT INTO {TABLE} (id, tenant_id, total_cents) VALUES (2,1,1),(3,1,1)"))
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(text(f"UPDATE {TABLE} SET {COLUMN}='OD1' WHERE id=2"))


def test_migrate_idempotent(tmp_path, monkeypatch):
    eng = _make_old_db(tmp_path)
    monkeypatch.setattr(dbmod, "engine", eng)
    dbmod._migrate_add_order_merchant_trade_no()
    dbmod._migrate_add_order_merchant_trade_no()  # 不報錯
    assert COLUMN in {c["name"] for c in inspect(eng).get_columns(TABLE)}
