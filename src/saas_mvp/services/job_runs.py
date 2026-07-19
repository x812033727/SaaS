"""作業心跳記錄(R6-C3)。

用法(cron/ops main 內):

    from saas_mvp.services import job_runs

    with job_runs.record(session_factory, "aggregate_daily_stats") as run:
        summary = do_work(...)
        run.detail = str(summary)          # 選填:摘要
    # 正常結束 → status=success + duration;拋例外 → status=failed(例外續拋)。

自成小交易(獨立於作業本身的 session),永不因心跳寫入失敗而擋作業:
start 失敗 → run 為 no-op sentinel;finish 失敗 → 吞掉。
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import time
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from saas_mvp.models.job_run import (
    JOB_FAILED,
    JOB_RUNNING,
    JOB_SUCCESS,
    JobRun,
)

_log = logging.getLogger(__name__)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class _RunHandle:
    """呼叫端可在 with 區塊內設 detail;id 為 None 時代表心跳未落庫(no-op)。"""

    def __init__(self, run_id: int | None) -> None:
        self.id = run_id
        self.detail: str | None = None


@contextlib.contextmanager
def record(session_factory: sessionmaker, job_name: str) -> Iterator[_RunHandle]:
    """記一次作業執行(start→finish);心跳失敗永不影響作業本身。"""
    handle = _RunHandle(None)
    started = time.monotonic()
    try:
        # R12-D:心跳是 ops 行程第一個 ORM 觸點;若呼叫端尚未 import 全部
        # model,configure_mappers 會因 relationship 字串(如 Tenant→'User')
        # 解析失敗而炸——aggregate_daily_stats 曾因此自 R6-C3 上線起每日
        # 靜默死(dry-run 不經心跳所以看不出來)。集中在此保證冪等載入。
        from saas_mvp.db import import_all_models

        import_all_models()
        with session_factory() as db:
            row = JobRun(job_name=job_name[:64], status=JOB_RUNNING, started_at=_utcnow())
            db.add(row)
            db.commit()
            handle.id = row.id
    except Exception:  # noqa: BLE001 — 心跳不得擋作業
        _log.warning("job_runs.record start failed job=%s", job_name, exc_info=True)

    failed = False
    try:
        yield handle
    except Exception:
        failed = True
        raise
    finally:
        if handle.id is not None:
            try:
                with session_factory() as db:
                    row = db.get(JobRun, handle.id)
                    if row is not None:
                        row.status = JOB_FAILED if failed else JOB_SUCCESS
                        row.finished_at = _utcnow()
                        row.duration_ms = int((time.monotonic() - started) * 1000)
                        row.detail = (handle.detail or "")[:500] or None
                        db.commit()
            except Exception:  # noqa: BLE001
                _log.warning("job_runs.record finish failed job=%s", job_name, exc_info=True)


def latest_runs(db) -> list[JobRun]:
    """每個 job_name 的最近一次執行(供 admin 顯示)。"""
    rows = db.execute(
        select(JobRun).order_by(JobRun.job_name, JobRun.started_at.desc())
    ).scalars().all()
    seen: dict[str, JobRun] = {}
    for r in rows:
        if r.job_name not in seen:
            seen[r.job_name] = r
    return list(seen.values())


def last_success_age_seconds(db, job_name: str, *, now: datetime.datetime | None = None) -> float | None:
    """該作業最後一次成功距今秒數;從未成功回 None。供 metric。"""
    row = db.execute(
        select(JobRun)
        .where(JobRun.job_name == job_name, JobRun.status == JOB_SUCCESS)
        .order_by(JobRun.finished_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None or row.finished_at is None:
        return None
    ref = now or _utcnow()
    fin = row.finished_at
    if fin.tzinfo is None:
        fin = fin.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (ref - fin).total_seconds())
