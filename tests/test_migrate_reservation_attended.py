"""migration 測試：舊 booking_reservations 缺 attended → 補欄（nullable）。"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

import saas_mvp.db as dbmod

TABLE = "booking_reservations"


def _make_old_db(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE {TABLE} ("
            "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, slot_id INTEGER NOT NULL, "
            "customer_id INTEGER, line_user_id VARCHAR(64), party_size INTEGER NOT NULL DEFAULT 1, "
            "status VARCHAR(16) NOT NULL DEFAULT 'confirmed', note TEXT, "
            "created_at DATETIME, updated_at DATETIME, cancelled_at DATETIME)"
        ))
        conn.execute(text(
            f"INSERT INTO {TABLE} (id, tenant_id, slot_id, party_size) VALUES (1, 1, 1, 2)"
        ))
    return eng


def test_migrate_adds_attended(tmp_path, monkeypatch):
    eng = _make_old_db(tmp_path)
    monkeypatch.setattr(dbmod, "engine", eng)
    assert "attended" not in {c["name"] for c in inspect(eng).get_columns(TABLE)}
    dbmod._migrate_add_reservation_attended()
    assert "attended" in {c["name"] for c in inspect(eng).get_columns(TABLE)}
    with eng.begin() as conn:
        val = conn.execute(text(f"SELECT attended FROM {TABLE} WHERE id=1")).scalar_one()
    assert val is None  # 既有列未標記


def test_migrate_idempotent(tmp_path, monkeypatch):
    eng = _make_old_db(tmp_path)
    monkeypatch.setattr(dbmod, "engine", eng)
    dbmod._migrate_add_reservation_attended()
    dbmod._migrate_add_reservation_attended()
    assert "attended" in {c["name"] for c in inspect(eng).get_columns(TABLE)}
