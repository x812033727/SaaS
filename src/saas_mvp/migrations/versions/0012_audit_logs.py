"""統一稽核日誌表（F1）。

Revision ID: d5a8c14e7f92
Revises: c9e3f52a8b17
Create Date: 2026-07-10

冪等守衛:比照 0004–0011。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5a8c14e7f92'
down_revision: Union[str, None] = 'c9e3f52a8b17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'audit_logs' in inspector.get_table_names():
        return
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'tenant_id', sa.Integer(),
            sa.ForeignKey('tenants.id', ondelete='SET NULL'), nullable=True,
        ),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('impersonator_user_id', sa.Integer(), nullable=True),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('target', sa.String(length=128), nullable=True),
        sa.Column('detail_json', sa.Text(), nullable=True),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_audit_logs_id', 'audit_logs', ['id'])
    op.create_index('ix_audit_tenant_created', 'audit_logs', ['tenant_id', 'created_at'])
    op.create_index('ix_audit_action_created', 'audit_logs', ['action', 'created_at'])


def downgrade() -> None:
    op.drop_table('audit_logs')
