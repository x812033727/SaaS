"""job_runs heartbeat table (R6-C3 batch-job observability).

Revision ID: e2c4a8f1b063
Revises: d1b7e3c8a940

job_runs:批次/cron 作業每次執行寫一列(start→finish),記 status/duration/detail。
供最後成功時間 metric 與 admin 顯示,填補「cron 綠燈零工作」的維運盲點。

冪等守衛:比照 0050 — inspect 後已存在則跳過。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2c4a8f1b063"
down_revision = "d1b7e3c8a940"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "job_runs" not in insp.get_table_names():
        op.create_table(
            "job_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_name", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),  # running|success|failed
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("detail", sa.String(length=500), nullable=True),
        )
        op.create_index(
            "ix_job_runs_name_started", "job_runs", ["job_name", "started_at"]
        )


def downgrade() -> None:
    op.drop_index("ix_job_runs_name_started", table_name="job_runs")
    op.drop_table("job_runs")
