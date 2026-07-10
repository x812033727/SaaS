"""Google Calendar 單向同步（E1 Step B）。

Revision ID: b3e9d47f1a28
Revises: a8d2f61c9e34
Create Date: 2026-07-10

冪等守衛:比照 0004–0015。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3e9d47f1a28'
down_revision: Union[str, None] = 'a8d2f61c9e34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if 'tenant_gcal_credentials' not in tables:
        op.create_table(
            'tenant_gcal_credentials',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'tenant_id', sa.Integer(),
                sa.ForeignKey('tenants.id', ondelete='CASCADE'),
                nullable=False, unique=True,
            ),
            sa.Column('refresh_token_enc', sa.LargeBinary(), nullable=False),
            sa.Column('calendar_id', sa.String(length=256), nullable=False),
            sa.Column('google_email', sa.String(length=256), nullable=True),
            sa.Column('status', sa.String(length=16), nullable=False),
            sa.Column('last_error', sa.String(length=255), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index('ix_tenant_gcal_credentials_id', 'tenant_gcal_credentials', ['id'])

    r_cols = {c["name"] for c in inspector.get_columns("booking_reservations")}
    with op.batch_alter_table('booking_reservations', schema=None) as batch_op:
        if 'gcal_event_id' not in r_cols:
            batch_op.add_column(sa.Column('gcal_event_id', sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('booking_reservations', schema=None) as batch_op:
        batch_op.drop_column('gcal_event_id')
    op.drop_table('tenant_gcal_credentials')
