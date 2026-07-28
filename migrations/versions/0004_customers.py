from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004_customers"
down_revision: str | None = "0003_conversation_tenant_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("customer_no", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "length(trim(customer_no)) > 0",
            name="ck_customers_customer_no_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(name)) > 0",
            name="ck_customers_name_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_customers_organization",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "customer_no",
            name="uq_customers_org_customer_no",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_customers_org_id",
        ),
    )
    op.create_index(
        "idx_customers_org_name",
        "customers",
        ["organization_id", "name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_customers_org_name", table_name="customers")
    op.drop_table("customers")
