"""R12-D 回歸:ops 心跳(job_runs.record)是行程第一個 ORM 觸點時的 mapper 案。

aggregate_daily_stats --apply 曾自 R6-C3 起每日靜默死:main() 先進
job_runs.record 才呼叫 import_all_models,configure_mappers 解析
Tenant→'User' relationship 失敗;dry-run 不經心跳所以一直是綠的。
修法=job_runs.record 開頭自呼 import_all_models(冪等)。

必須在「全新直譯器」重現(同行程 conftest 已 import 全部 model,
mapper 早已配置,測不到)。
"""

from __future__ import annotations

import os
import subprocess
import sys


def _env(tmp_path):
    env = dict(os.environ)
    env.update({
        "SAAS_DATABASE_URL": f"sqlite:///{tmp_path / 'ops_hb.db'}",
        "SAAS_RATE_LIMIT_ENABLED": "false",
    })
    return env


def test_aggregate_daily_stats_apply_in_fresh_interpreter(tmp_path):
    env = _env(tmp_path)
    r0 = subprocess.run(
        [sys.executable, "-c", "from saas_mvp.db import init_db; init_db()"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert r0.returncode == 0, r0.stderr[-2000:]
    r = subprocess.run(
        [sys.executable, "-m", "saas_mvp.ops.aggregate_daily_stats",
         "--apply", "--days", "1"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    assert "errors=0" in r.stdout
    assert "failed to locate a name" not in r.stderr
