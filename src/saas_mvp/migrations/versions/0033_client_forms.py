"""Add appointment-linked client intake and consent forms.

Revision ID: b4f92d71ac06
Revises: a8d29c4ef731
"""

from __future__ import annotations
import sqlalchemy as sa
from alembic import op

revision = "b4f92d71ac06"
down_revision = "a8d29c4ef731"
branch_labels = None
depends_on = None


def upgrade() -> None:
    expected = {
        "client_form_templates",
        "client_form_questions",
        "client_form_requests",
    }
    existing = set(sa.inspect(op.get_bind()).get_table_names()) & expected
    if existing == expected:
        return
    if existing:
        raise RuntimeError(
            f"partial client form schema; missing: {', '.join(sorted(expected-existing))}"
        )
    op.create_table(
        "client_form_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("intro", sa.Text(), nullable=True),
        sa.Column("consent_text", sa.Text(), nullable=False),
        sa.Column(
            "require_signature", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["service_id"], ["booking_services.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_client_form_template_name"),
    )
    op.create_index(
        "ix_client_form_templates_tenant_id", "client_form_templates", ["tenant_id"]
    )
    op.create_index(
        "ix_client_form_templates_service_id", "client_form_templates", ["service_id"]
    )
    op.create_table(
        "client_form_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("field_type", sa.String(16), nullable=False),
        sa.Column(
            "is_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("options_json", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["template_id"], ["client_form_templates.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_client_form_questions_tenant_id", "client_form_questions", ["tenant_id"]
    )
    op.create_index(
        "ix_client_form_questions_template_id", "client_form_questions", ["template_id"]
    )
    op.create_index(
        "ix_client_form_question_order",
        "client_form_questions",
        ["tenant_id", "template_id", "sort_order"],
    )
    op.create_table(
        "client_form_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("template_name_snapshot", sa.String(128), nullable=False),
        sa.Column("intro_snapshot", sa.Text(), nullable=True),
        sa.Column("consent_text_snapshot", sa.Text(), nullable=False),
        sa.Column("questions_json", sa.Text(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("require_signature_snapshot", sa.Boolean(), nullable=False),
        sa.Column("answers_json", sa.Text(), nullable=True),
        sa.Column("signer_name", sa.String(128), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_ip", sa.String(64), nullable=True),
        sa.Column("submitted_user_agent", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["template_id"], ["client_form_templates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["booking_reservations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["booking_customers.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("token", name="uq_client_form_request_token"),
        sa.UniqueConstraint(
            "tenant_id",
            "template_id",
            "reservation_id",
            name="uq_client_form_request_reservation",
        ),
    )
    for c in ("tenant_id", "template_id", "reservation_id", "customer_id"):
        op.create_index(f"ix_client_form_requests_{c}", "client_form_requests", [c])
    op.create_index(
        "ix_client_form_request_customer_status",
        "client_form_requests",
        ["tenant_id", "customer_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("client_form_requests")
    op.drop_table("client_form_questions")
    op.drop_table("client_form_templates")
