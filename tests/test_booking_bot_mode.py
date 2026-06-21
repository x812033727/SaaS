"""bot_mode 測試 — migration 向後相容 + 自助 API 設定/讀回/驗證。"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

import saas_mvp.db as dbmod  # noqa: E402
from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402

TABLE = "line_channel_configs"
COLUMN = "bot_mode"


# ── Migration（舊 DB 缺 bot_mode 欄位） ────────────────────────────────────────

def _make_old_db_engine(tmp_path):
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
        conn.execute(
            text(
                f"INSERT INTO {TABLE} "
                "(id, tenant_id, channel_secret_enc, access_token_enc) "
                "VALUES (1, 100, x'00', x'01')"
            )
        )
    return eng


def test_migrate_adds_bot_mode_and_backfills_translation(tmp_path, monkeypatch):
    eng = _make_old_db_engine(tmp_path)
    monkeypatch.setattr(dbmod, "engine", eng)

    assert COLUMN not in {c["name"] for c in inspect(eng).get_columns(TABLE)}
    dbmod._migrate_add_line_bot_mode()
    assert COLUMN in {c["name"] for c in inspect(eng).get_columns(TABLE)}

    # 既有列回填為 translation（既有翻譯店家零影響）
    with eng.begin() as conn:
        val = conn.execute(
            text(f"SELECT {COLUMN} FROM {TABLE} WHERE id=1")
        ).scalar_one()
    assert val == "translation"


def test_migrate_idempotent(tmp_path, monkeypatch):
    eng = _make_old_db_engine(tmp_path)
    monkeypatch.setattr(dbmod, "engine", eng)
    dbmod._migrate_add_line_bot_mode()
    # 第二次呼叫不報錯
    dbmod._migrate_add_line_bot_mode()
    assert COLUMN in {c["name"] for c in inspect(eng).get_columns(TABLE)}


# ── 自助 API bot_mode 設定 ────────────────────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_get_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _register(client) -> str:
    r = client.post("/auth/register", json={
        "email": f"u_{uuid.uuid4().hex[:8]}@example.com",
        "password": "Test1234!",
        "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


_PATH = "/tenants/me/line-config"
_BODY = {"channel_secret": "s" * 32, "access_token": "a" * 40}


class TestBotModeApi:
    def test_default_is_translation(self, client):
        token = _register(client)
        r = client.put(_PATH, headers=_auth(token), json=dict(_BODY))
        assert r.status_code == 200, r.text
        assert r.json()["bot_mode"] == "translation"

    def test_set_booking_and_read_back(self, client):
        token = _register(client)
        r = client.put(
            _PATH, headers=_auth(token), json={**_BODY, "bot_mode": "booking"}
        )
        assert r.status_code == 200
        assert r.json()["bot_mode"] == "booking"
        got = client.get(_PATH, headers=_auth(token))
        assert got.json()["bot_mode"] == "booking"

    def test_invalid_bot_mode_400(self, client):
        token = _register(client)
        r = client.put(
            _PATH, headers=_auth(token), json={**_BODY, "bot_mode": "nonsense"}
        )
        assert r.status_code == 400, r.text

    def test_omitting_bot_mode_keeps_existing(self, client):
        token = _register(client)
        client.put(_PATH, headers=_auth(token), json={**_BODY, "bot_mode": "booking"})
        # 後續更新不送 bot_mode → 維持 booking
        client.put(_PATH, headers=_auth(token), json=dict(_BODY))
        assert client.get(_PATH, headers=_auth(token)).json()["bot_mode"] == "booking"
