"""正式部署可攜性回歸測試（PostgreSQL / 容器 / 多 worker）。

這些守衛鎖定「只在真實 PG / standalone 執行才會炸、SQLite 單元測試漏掉」的問題：
  1. Boolean 欄位的 server_default 必須是 true/false（PG 不接受整數 1/0）。
  2. Settings 必須忽略 .env 中的非 SAAS_ 額外鍵（部署層變數不該讓 app 崩潰）。
  3. ops 腳本以 `python -m`（cron/scheduler）獨立執行時 ORM registry 必須完整。
"""

from __future__ import annotations

import os
import subprocess
import sys
import pathlib

import pytest
from sqlalchemy import Boolean

from saas_mvp.db import Base, import_all_models

_SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")


def test_boolean_columns_use_boolean_server_default():
    """所有 Boolean 欄位的 server_default 不得是整數字面量（PG DatatypeMismatch）。

    PostgreSQL 對 ``is_active BOOLEAN DEFAULT 1`` 報錯（boolean 預設須 true/false）；
    SQLite 寬容地接受 1/0，故此問題只在 PG 浮現。本測試在 SQLite/CI 即可攔截。
    """
    import_all_models()
    offenders = []
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, Boolean) and col.server_default is not None:
                txt = str(getattr(col.server_default, "arg", "")).strip().lower()
                if txt not in ("true", "false"):
                    offenders.append(f"{table.name}.{col.name} -> {txt!r}")
    assert not offenders, (
        "Boolean 欄位的 server_default 必須是 true/false（PG 相容），違規："
        + ", ".join(offenders)
    )


def test_settings_ignores_unknown_env_keys(monkeypatch):
    """.env / 環境中的非 SAAS_ 額外鍵（POSTGRES_USER、WEB_PORT…）不得讓 Settings 崩潰。"""
    monkeypatch.setenv("WEB_PORT", "8099")
    monkeypatch.setenv("POSTGRES_USER", "saas")
    monkeypatch.setenv("GUNICORN_WORKERS", "4")
    from saas_mvp.config import Settings

    # 不應拋 pydantic ValidationError(extra_forbidden)
    s = Settings(_env_file=None)
    assert s.env  # 正常建構


@pytest.mark.parametrize(
    "module",
    [
        "saas_mvp.ops.send_due_reminders",
        "saas_mvp.ops.send_due_notifications",
        "saas_mvp.ops.run_birthday_campaigns",
        "saas_mvp.ops.run_reactivation",
        "saas_mvp.ops.run_scheduled_campaigns",
    ],
)
def test_ops_script_runs_standalone(tmp_path, module):
    """ops 腳本以全新 subprocess（python -m）+ 乾淨 DB 執行 --dry-run 必須成功。

    守衛「relationship 字串（如 'Tenant'）解析失敗」回歸：standalone 執行時
    registry 未必完整，各 ops 的 main() 應先 import_all_models()。
    subprocess 隔離才能真實重現（同 process 內 conftest 已 import 所有 model）。
    """
    db = tmp_path / "ops.db"
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["SAAS_DATABASE_URL"] = f"sqlite:///{db}"
    env["SAAS_RATE_LIMIT_ENABLED"] = "false"
    # 明確以 test 身分跑（原本 pop 掉 SAAS_ENV）：subprocess 的 cwd 是 repo 根，
    # 主機正式部署的 .env（SAAS_ENV=prod）會被 pydantic-settings 讀進去；同時
    # 其他測試模組在 import 期 setdefault 的 dev 加密金鑰殘留在 os.environ、
    # 優先權又高於 .env → 組成「prod 身分 + dev 金鑰」在全量跑必炸的組合。
    # 測試意圖是「乾淨可攜」，不是模擬 prod。
    env["SAAS_ENV"] = "test"
    # 先建表（init_db），再跑 ops（dry-run，不送任何推播）
    init = subprocess.run(
        [sys.executable, "-c", "from saas_mvp.db import init_db; init_db()"],
        env=env, capture_output=True, text=True,
    )
    assert init.returncode == 0, init.stderr
    r = subprocess.run(
        [sys.executable, "-m", module, "--dry-run"],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"{module} standalone failed:\n{r.stderr}"
    assert "failed to locate a name" not in (r.stderr + r.stdout)
