"""email_tokens 表 + users.email_verified_at（B3 onboarding）。

Revision ID: e6f01b3c8a92
Revises: c2d94ab07e61
Create Date: 2026-07-10

冪等守衛：比照 0004–0007。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e6f01b3c8a92'
down_revision: Union[str, None] = 'c2d94ab07e61'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    user_cols = {c["name"] for c in inspector.get_columns("users")}
    with op.batch_alter_table('users', schema=None) as batch_op:
        if 'email_verified_at' not in user_cols:
            batch_op.add_column(
                sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True)
            )

    if 'email_tokens' not in inspector.get_table_names():
        op.create_table(
            'email_tokens',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'user_id', sa.Integer(),
                sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False,
            ),
            sa.Column('purpose', sa.String(length=16), nullable=False),
            sa.Column('token_hash', sa.String(length=64), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index('ix_email_tokens_id', 'email_tokens', ['id'])
        op.create_index('ix_email_tokens_user_id', 'email_tokens', ['user_id'])
        op.create_index(
            'ix_email_tokens_token_hash', 'email_tokens', ['token_hash'], unique=True
        )


def downgrade() -> None:
    op.drop_table('email_tokens')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('email_verified_at')
