import os
import subprocess
import sys


def test_init_db_registers_line_webhook_event_table(tmp_path):
    db_path = tmp_path / "init_db.sqlite"
    script = """
from sqlalchemy import inspect
from saas_mvp import db as db_module

db_module.init_db()
tables = inspect(db_module.engine).get_table_names()
assert "line_webhook_events" in tables, tables
"""
    env = os.environ.copy()
    env["SAAS_DATABASE_URL"] = f"sqlite:///{db_path}"

    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        cwd=tmp_path,
        env=env,
        timeout=10,
    )
