"""migration 測試：舊 booking_customers 缺 points_balance/tier → 補欄並回填。"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

import saas_mvp.db as dbmod

TABLE = "booking_customers"


def _make_old_db(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE {TABLE} ("
            "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, "
            "line_user_id VARCHAR(64) NOT NULL, display_name VARCHAR(128), "
            "phone VARCHAR(32), booking_count INTEGER NOT NULL DEFAULT 0, "
            "last_booked_at DATETIME, note TEXT, "
            "created_at DATETIME, updated_at DATETIME)"
        ))
        conn.execute(text(
            f"INSERT INTO {TABLE} (id, tenant_id, line_user_id, booking_count) "
            "VALUES (1, 1, 'Uold', 3)"
        ))
    return eng


def test_migrate_adds_membership_and_backfills(tmp_path, monkeypatch):
    eng = _make_old_db(tmp_path)
    monkeypatch.setattr(dbmod, "engine", eng)

    cols_before = {c["name"] for c in inspect(eng).get_columns(TABLE)}
    assert "points_balance" not in cols_before and "tier" not in cols_before

    dbmod._migrate_add_customer_membership()

    cols_after = {c["name"] for c in inspect(eng).get_columns(TABLE)}
    assert "points_balance" in cols_after and "tier" in cols_after
    with eng.begin() as conn:
        row = conn.execute(text(
            f"SELECT points_balance, tier FROM {TABLE} WHERE id=1"
        )).one()
    assert row[0] == 0 and row[1] == "regular"


def test_migrate_idempotent(tmp_path, monkeypatch):
    eng = _make_old_db(tmp_path)
    monkeypatch.setattr(dbmod, "engine", eng)
    dbmod._migrate_add_customer_membership()
    dbmod._migrate_add_customer_membership()  # 不報錯
    assert "tier" in {c["name"] for c in inspect(eng).get_columns(TABLE)}
