"""AI 答不好的問題(D4,FAQ 自學)。

Revision ID: c4f8a25d7b19
Revises: b3e9d47f1a28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c4f8a25d7b19'
down_revision: Union[str, None] = 'b3e9d47f1a28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("ai_unanswered_questions"):
        return
    op.create_table(
        "ai_unanswered_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("question_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "hit_count", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "question_hash", name="uq_ai_unanswered_tenant_hash"
        ),
    )
    op.create_index(
        "ix_ai_unanswered_questions_tenant_id",
        "ai_unanswered_questions",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_table("ai_unanswered_questions")
