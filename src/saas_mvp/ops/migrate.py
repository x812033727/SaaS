"""Schema 遷移進入點（取代啟動時直呼 init_db 的建表+手寫遷移）。

Usage:
    python -m saas_mvp.ops.migrate            # 冪等,可重複執行
    python -m saas_mvp.ops.migrate --check    # 只回報狀態,不改動

三分支邏輯（冪等）:
  1. 全新 DB（無任何業務表）        → alembic upgrade head
  2. legacy DB（有業務表、無 alembic_version）
       → 先跑 legacy_init_db()（create_all + 全部 _migrate_*,把 schema
         收斂到 baseline 等價）→ alembic stamp <baseline> → upgrade head
  3. 已納管（有 alembic_version）   → alembic upgrade head

script_location 以套件路徑解析（saas_mvp/migrations 隨 pip install 發佈）,
不依賴 repo 根目錄或 alembic.ini;URL 取 settings.database_url。
多 worker 部署應由 entrypoint 在啟動前單次執行本命令,而非每個 worker
的 lifespan 各跑一次（app.py 的 init_db 仍 delegate 到此,供本機 dev 直跑）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import TextIO

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect

_log = logging.getLogger(__name__)

# baseline revision id（migrations/versions/0001_baseline.py）
BASELINE_REVISION = "3a4af702fa42"

# 判定「legacy DB」用的哨兵業務表（自專案初版即存在）。
_SENTINEL_TABLE = "tenants"


def _alembic_config(database_url: str | None = None) -> AlembicConfig:
    """程式化 Alembic 設定：script_location 指向套件內 migrations/。"""
    from saas_mvp.config import settings

    script_location = Path(__file__).resolve().parent.parent / "migrations"
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(script_location))
    url = database_url or settings.database_url
    # env.py 讀 -x db_url 優先於 settings（測試以不同 URL 跑遷移用）。
    cfg.cmd_opts = argparse.Namespace(x=[f"db_url={url}"])
    return cfg


def _db_state(engine) -> str:
    """回傳 'fresh' | 'legacy' | 'managed'。"""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "alembic_version" in tables:
        return "managed"
    if _SENTINEL_TABLE in tables:
        return "legacy"
    return "fresh"


def run_migrations(*, engine=None, database_url: str | None = None) -> str:
    """執行三分支遷移；回傳採取的路徑（fresh/legacy/managed）供記錄。"""
    import saas_mvp.db as dbmod

    effective_engine = engine if engine is not None else dbmod.engine
    state = _db_state(effective_engine)
    cfg = _alembic_config(database_url)

    if state == "legacy":
        # 收斂 legacy schema 到 baseline 等價（create_all + 全部 _migrate_*,
        # 冪等）,再 stamp baseline（不重跑 DDL）。
        _log.info("migrate: legacy DB detected — converging then stamping")
        dbmod.legacy_init_db()
        alembic_command.stamp(cfg, BASELINE_REVISION)

    alembic_command.upgrade(cfg, "head")
    _log.info("migrate: done (state=%s)", state)
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run database schema migrations (idempotent)."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report DB state (fresh/legacy/managed) without migrating.",
    )
    return parser


def main(argv: list[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    args = build_parser().parse_args(argv)
    import saas_mvp.db as dbmod

    if args.check:
        print(f"state={_db_state(dbmod.engine)}", file=stdout)
        return 0
    state = run_migrations()
    print(f"migrated state={state}", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
