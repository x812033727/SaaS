"""AI 對話（A2）：對話狀態表 + AI 月度計量。

Revision ID: b7d2e91f4a56
Revises: a41c85f7d203
Create Date: 2026-07-10

冪等守衛：比照 0004–0009。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7d2e91f4a56'
down_revision: Union[str, None] = 'a41c85f7d203'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if 'line_conversations' not in tables:
        op.create_table(
            'line_conversations',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'tenant_id', sa.Integer(),
                sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False,
            ),
            sa.Column('line_user_id', sa.String(length=64), nullable=False),
            sa.Column('state', sa.String(length=16), nullable=False),
            sa.Column('slots_json', sa.Text(), nullable=True),
            sa.Column('turn_count', sa.Integer(), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                'tenant_id', 'line_user_id', name='uq_line_conversation'
            ),
        )
        op.create_index('ix_line_conversations_id', 'line_conversations', ['id'])
        op.create_index(
            'ix_line_conversations_tenant_id', 'line_conversations', ['tenant_id']
        )

    if 'ai_usage' not in tables:
        op.create_table(
            'ai_usage',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'tenant_id', sa.Integer(),
                sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False,
            ),
            sa.Column('period', sa.String(length=6), nullable=False),
            sa.Column('count', sa.Integer(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint('tenant_id', 'period', name='uq_ai_usage_period'),
        )
        op.create_index('ix_ai_usage_id', 'ai_usage', ['id'])
        op.create_index('ix_ai_usage_tenant_id', 'ai_usage', ['tenant_id'])


def downgrade() -> None:
    op.drop_table('ai_usage')
    op.drop_table('line_conversations')
