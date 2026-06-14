"""Task #1 驗收測試：src layout 套件結構、pyproject.toml、雙入口"""

import importlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
SRC_DIR = PROJECT_ROOT / "src" / "saas_mvp"
PYTHON = sys.executable


# ─────────────────────────── 1. pyproject.toml 結構 ───────────────────────────

class TestPyprojectToml:
    def test_pyproject_exists(self):
        assert PYPROJECT.exists(), "pyproject.toml 不存在"

    def test_build_system(self):
        content = PYPROJECT.read_text()
        assert "[build-system]" in content
        assert "setuptools" in content

    def test_project_name(self):
        content = PYPROJECT.read_text()
        assert 'name = "saas-mvp"' in content

    def test_console_script_entry_point(self):
        content = PYPROJECT.read_text()
        assert "[project.scripts]" in content
        assert "saas-mvp" in content
        assert "saas_mvp.__main__:main" in content

    def test_src_layout_config(self):
        content = PYPROJECT.read_text()
        assert '[tool.setuptools.packages.find]' in content
        assert 'where = ["src"]' in content

    def test_dependencies_pinned(self):
        """所有相依版本都必須釘選（含 == 版號）"""
        import tomllib
        with open(PYPROJECT, "rb") as f:
            data = tomllib.load(f)
        deps = data["project"]["dependencies"]
        assert len(deps) > 0, "dependencies 為空"
        for dep in deps:
            assert "==" in dep, f"相依未釘選版本: {dep}"

    def test_requires_python(self):
        import tomllib
        with open(PYPROJECT, "rb") as f:
            data = tomllib.load(f)
        req_python = data["project"].get("requires-python", "")
        assert req_python, "requires-python 未設定"


# ─────────────────────────── 2. src layout 檔案結構 ───────────────────────────

class TestSrcLayout:
    def test_src_saas_mvp_exists(self):
        assert SRC_DIR.is_dir(), "src/saas_mvp 目錄不存在"

    def test_init_py_exists(self):
        assert (SRC_DIR / "__init__.py").exists()

    def test_main_py_exists(self):
        assert (SRC_DIR / "__main__.py").exists()

    def test_app_py_exists(self):
        assert (SRC_DIR / "app.py").exists()

    def test_config_py_exists(self):
        assert (SRC_DIR / "config.py").exists()

    def test_main_py_has_main_func(self):
        content = (SRC_DIR / "__main__.py").read_text()
        assert "def main" in content, "__main__.py 缺少 main() 函數"

    def test_main_py_has_if_name_main(self):
        content = (SRC_DIR / "__main__.py").read_text()
        assert 'if __name__ == "__main__"' in content, "__main__.py 缺少 if __name__ == '__main__' 保護"

    def test_main_py_calls_main(self):
        content = (SRC_DIR / "__main__.py").read_text()
        assert "main()" in content


# ─────────────────────────── 3. 套件可 import ────────────────────────────────

class TestPackageImport:
    def test_import_saas_mvp(self):
        spec = importlib.util.spec_from_file_location(
            "saas_mvp",
            str(SRC_DIR / "__init__.py"),
            submodule_search_locations=[str(SRC_DIR)],
        )
        assert spec is not None

    def test_version_defined(self):
        content = (SRC_DIR / "__init__.py").read_text()
        assert "__version__" in content


# ─────────────────────────── 4. 套件已正確安裝 ───────────────────────────────

class TestPackageInstalled:
    def test_saas_mvp_importable(self):
        result = subprocess.run(
            [PYTHON, "-c", "import saas_mvp; print(saas_mvp.__version__)"],
            capture_output=True, text=True
        )
        assert result.returncode == 0, f"import 失敗: {result.stderr}"
        assert result.stdout.strip(), "版本號為空"

    def test_all_deps_importable(self):
        """驗證所有相依套件皆可 import"""
        modules = [
            ("fastapi", "fastapi"),
            ("uvicorn", "uvicorn"),
            ("sqlalchemy", "sqlalchemy"),
            ("passlib", "passlib"),
            ("jwt", "PyJWT"),
            ("multipart", "python-multipart"),
            ("pydantic", "pydantic"),
            ("pydantic_settings", "pydantic-settings"),
        ]
        for mod, pkg in modules:
            result = subprocess.run(
                [PYTHON, "-c", f"import {mod}"],
                capture_output=True, text=True
            )
            assert result.returncode == 0, f"{pkg} import 失敗: {result.stderr}"

    def test_console_script_installed(self):
        """console script saas-mvp 必須存在於 PATH 中"""
        venv_bin = Path(PYTHON).parent
        script = venv_bin / "saas-mvp"
        assert script.exists(), f"console script 不存在: {script}"


# ─────────────────────────── 5. 雙入口啟動測試 ───────────────────────────────

class TestDualEntryPoints:
    """啟動 server、健康檢查、再 kill"""

    @staticmethod
    def _free_port() -> int:
        """取得一個目前可用的 TCP port，避免硬編 port 在並行/殘留時衝突。"""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @staticmethod
    def _start_and_check(cmd: list[str], label: str) -> None:
        # 動態取空 port，並在「address already in use」時換 port 重試，
        # 徹底消除固定 port 與其他測試/殘留進程相撞的根因。
        import time, tempfile
        tmpdir = os.environ.get("TMPDIR", tempfile.gettempdir())
        last_err = ""
        for attempt in range(3):
            port = TestDualEntryPoints._free_port()
            log_path = f"{tmpdir}/{label}.log"
            db_path = f"{tmpdir}/test_task1_{label}_{port}.db"
            with open(log_path, "w") as log:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log, stderr=log,
                    env={**os.environ, "SAAS_DATABASE_URL": f"sqlite:///{db_path}",
                         "SAAS_PORT": str(port)},
                )
            try:
                # 輪詢啟動，最多 ~10 秒（取代死等，更快也更穩）
                result = None
                deadline = time.time() + 10
                while time.time() < deadline:
                    time.sleep(0.3)
                    if proc.poll() is not None:
                        break  # server 已退出（多半是 bind 失敗），跳出去看 log
                    result = subprocess.run(
                        ["curl", "-sf", f"http://127.0.0.1:{port}/"],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        break
                log_content = Path(log_path).read_text()
                # port 撞了就換一個 port 重試，而非直接 fail
                if "address already in use" in log_content and attempt < 2:
                    last_err = log_content[-1500:]
                    continue
                assert result is not None and result.returncode == 0, (
                    f"{label} health check 失敗\ncurl stderr:"
                    f" {result.stderr if result else 'n/a'}\nserver log:\n{log_content[-1500:]}"
                )
                assert '"status"' in result.stdout or "saas-mvp" in result.stdout, \
                    f"回應不符預期: {result.stdout}"
                return
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                # 清理 db
                Path(db_path).unlink(missing_ok=True)
        raise AssertionError(f"{label} 連續重試後仍無法啟動\nserver log:\n{last_err}")

    def test_python_m_entry(self):
        """`python -m saas_mvp` 能啟動 HTTP 服務"""
        self._start_and_check(
            [PYTHON, "-m", "saas_mvp"],
            label="python_m_saas_mvp",
        )

    def test_console_script_entry(self):
        """`saas-mvp` console script 能啟動 HTTP 服務"""
        venv_bin = Path(PYTHON).parent
        script = str(venv_bin / "saas-mvp")
        self._start_and_check(
            [script],
            label="console_script_saas_mvp",
        )
