"""批次/cron 作業執行心跳(R6-C3)— 每次執行一列,供 last-success metric 與 admin。"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, Index, Integer, String

from saas_mvp.db import Base

JOB_RUNNING = "running"
JOB_SUCCESS = "success"
JOB_FAILED = "failed"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class JobRun(Base):
    __tablename__ = "job_runs"

    id = Column(Integer, primary_key=True)
    job_name = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default=JOB_RUNNING)
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    detail = Column(String(500), nullable=True)

    __table_args__ = (Index("ix_job_runs_name_started", "job_name", "started_at"),)
