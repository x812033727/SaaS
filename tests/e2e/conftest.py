"""E2E 冒煙(F5)— subprocess uvicorn(tmp sqlite,全 stub)+ playwright chromium。

主 CI 不跑(run_tests.sh -m "not e2e");nightly workflow / 本地
``pytest -m e2e tests/e2e`` 執行。需 ``pip install -e ".[e2e]"`` +
``playwright install chromium``。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="e2e 需 playwright(pip install -e '.[e2e]')")

from playwright.sync_api import sync_playwright  # noqa: E402

_SRC = str(Path(__file__).resolve().parents[2] / "src")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(tmp_path_factory) -> dict:
    """啟動獨立 uvicorn(tmp sqlite;憑證全空 → 全 stub)。"""
    db_path = tmp_path_factory.mktemp("e2e") / "e2e.db"
    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": _SRC,
        "SAAS_DATABASE_URL": f"sqlite:///{db_path}",
        "SAAS_ENV": "test",
        "SAAS_TESTING": "1",
        "SAAS_BCRYPT_ROUNDS": "4",
        "SAAS_RATE_LIMIT_ENABLED": "false",
        "SAAS_FEATURES_DEFAULT_ENABLED": "true",
        "SAAS_METRICS_TOKEN": "",
        "SAAS_PUBLIC_BASE_URL": "",
        "SAAS_SMTP_HOST": "",
        "SAAS_MINIMAX_API_KEY": "",
        "SAAS_SENTRY_DSN": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "saas_mvp.app:create_app",
         "--factory", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        import urllib.request

        for _ in range(100):
            if proc.poll() is not None:
                out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                raise RuntimeError(f"uvicorn died:\n{out[-3000:]}")
            try:
                with urllib.request.urlopen(f"{base}/healthz", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise RuntimeError("uvicorn 未在時限內就緒")
        yield {"base": base, "db_path": str(db_path)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def console_server(server) -> dict:
    """啟動 Next.js console(next start,basePath=/console),透過
    SAAS_API_INTERNAL_URL 反打同一顆 uvicorn。需先 `npm ci && npm run build`
    (CI 步驟預建;本地跑前自行 build)。無 .next 產物則 skip。"""
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    if not (frontend / ".next").exists():
        pytest.skip("console 未 build(cd frontend && npm run build)")
    port = _free_port()
    env = {
        **os.environ,
        "PORT": str(port),
        "SAAS_API_INTERNAL_URL": server["base"],
        "NODE_ENV": "production",
    }
    proc = subprocess.Popen(
        ["npm", "run", "start"],
        cwd=str(frontend),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        import urllib.request

        for _ in range(150):
            if proc.poll() is not None:
                out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                raise RuntimeError(f"next 啟動失敗:\n{out[-3000:]}")
            try:
                with urllib.request.urlopen(f"{base}/console/login", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise RuntimeError("next 未在時限內就緒")
        yield {"base": base}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()
