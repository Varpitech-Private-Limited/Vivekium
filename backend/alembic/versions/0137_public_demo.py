"""Public demo flag on tenants and candidate users.

Revision ID: 0137_public_demo
Revises: 0136_monthly_assessment_plans
"""
from alembic import op
import sqlalchemy as sa

revision = "0137_public_demo"
down_revision = "0136_monthly_assessment_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("public_demo", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column("public_demo", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_tenants_public_demo_created",
        "tenants",
        ["created_at"],
        postgresql_where=sa.text("public_demo"),
    )
    op.create_index(
        "ix_users_public_demo_created",
        "users",
        ["created_at"],
        postgresql_where=sa.text("public_demo AND tenant_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_users_public_demo_created", table_name="users")
    op.drop_index("ix_tenants_public_demo_created", table_name="tenants")
    op.drop_column("users", "public_demo")
    op.drop_column("tenants", "public_demo")
