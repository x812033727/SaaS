"""備份機制契約測試（不需 docker / 不連 DB）。

shell 腳本難做行為單元測試；改驗證**契約**——確保未來重構不會靜默拿掉自動備份、
保留輪替或還原能力。檢查的是「關鍵指令字串存在」與「compose 服務正確接線」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKER = ROOT / "docker"


def _read(rel: str) -> str:
    path = ROOT / rel
    assert path.exists(), f"缺少檔案：{rel}"
    return path.read_text(encoding="utf-8")


def test_backup_script_does_pg_dump_and_rotation() -> None:
    body = _read("docker/backup.sh")
    # custom format dump + 保留輪替 + atomic（.tmp → rename）
    assert "pg_dump -Fc" in body
    assert "BACKUP_RETENTION_DAYS" in body
    assert "-mtime" in body and "-delete" in body
    assert ".tmp" in body and "mv " in body


def test_restore_script_guards_destructive_restore() -> None:
    body = _read("docker/restore.sh")
    assert "pg_restore" in body
    assert "--clean" in body and "--if-exists" in body
    # 破壞性還原必須有 FORCE 防誤覆寫
    assert "FORCE" in body


def test_scheduler_loop_is_daily_with_heartbeat() -> None:
    body = _read("docker/db-backup-entrypoint.sh")
    assert "BACKUP_TIME" in body
    assert "backup.sh" in body
    assert "heartbeat" in body


def test_compose_wires_db_backup_service() -> None:
    compose = _read("docker-compose.yml")
    assert "db-backup:" in compose
    assert "./backups:/backups" in compose
    assert "db-backup-entrypoint.sh" in compose
    # 比照其他服務的安全強化
    assert "no-new-privileges:true" in compose


def test_backups_dir_is_gitignored() -> None:
    assert "/backups/" in _read(".gitignore")


@pytest.mark.parametrize(
    "script", ["backup.sh", "restore.sh", "db-backup-entrypoint.sh"]
)
def test_scripts_have_shebang(script: str) -> None:
    assert _read(f"docker/{script}").startswith("#!/usr/bin/env bash")
