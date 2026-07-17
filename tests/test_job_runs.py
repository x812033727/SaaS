"""R6-C3 — 批次作業心跳記錄 + last-success age + admin 顯示。"""

from __future__ import annotations

import datetime
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.db import Base, import_all_models  # noqa: E402

import_all_models()

from saas_mvp.models.job_run import JobRun  # noqa: E402
from saas_mvp.services import job_runs  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture(autouse=True)
def _fresh():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


class TestRecord:
    def test_success_records_start_and_finish(self):
        with job_runs.record(_Session, "job_a") as run:
            run.detail = "did 5 things"
        db = _Session()
        try:
            row = db.query(JobRun).filter_by(job_name="job_a").one()
            assert row.status == "success"
            assert row.finished_at is not None
            assert row.duration_ms is not None
            assert row.detail == "did 5 things"
        finally:
            db.close()

    def test_exception_records_failed_and_reraises(self):
        with pytest.raises(ValueError):
            with job_runs.record(_Session, "job_b"):
                raise ValueError("boom")
        db = _Session()
        try:
            row = db.query(JobRun).filter_by(job_name="job_b").one()
            assert row.status == "failed"
            assert row.finished_at is not None
        finally:
            db.close()

    def test_heartbeat_failure_never_breaks_job(self, monkeypatch):
        """心跳寫入失敗時作業照常完成(record 為 no-op sentinel)。"""
        class _BrokenFactory:
            def __call__(self):
                raise RuntimeError("db down")

        ran = []
        with job_runs.record(_BrokenFactory(), "job_c") as run:
            assert run.id is None  # 心跳未落庫
            ran.append(1)
        assert ran == [1]  # 作業區塊照跑


class TestQueries:
    def _seed(self, db, name, status, finished_min_ago):
        now = datetime.datetime.now(datetime.timezone.utc)
        db.add(JobRun(
            job_name=name, status=status,
            started_at=now - datetime.timedelta(minutes=finished_min_ago + 1),
            finished_at=now - datetime.timedelta(minutes=finished_min_ago),
            duration_ms=1000,
        ))

    def test_latest_runs_one_per_job(self):
        db = _Session()
        try:
            self._seed(db, "job_x", "success", 100)
            self._seed(db, "job_x", "failed", 10)   # 較新
            self._seed(db, "job_y", "success", 5)
            db.commit()
            latest = {j.job_name: j for j in job_runs.latest_runs(db)}
            assert latest["job_x"].status == "failed"  # 取最近一次
            assert latest["job_y"].status == "success"
        finally:
            db.close()

    def test_last_success_age(self):
        db = _Session()
        try:
            self._seed(db, "job_z", "failed", 1)      # 失敗不算
            self._seed(db, "job_z", "success", 30)    # 30 分鐘前成功
            db.commit()
            age = job_runs.last_success_age_seconds(db, "job_z")
            assert age is not None and 1700 < age < 1900  # ~30 分鐘
            assert job_runs.last_success_age_seconds(db, "never") is None
        finally:
            db.close()
