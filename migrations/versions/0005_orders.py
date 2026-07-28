from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005_orders"
down_revision: str | None = "0004_customers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("order_no", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("item_summary", sa.Text(), nullable=False),
        sa.Column("total_amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("placed_at", sa.Text(), nullable=False),
        sa.Column("promised_ship_at", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            """
            status IN (
                'pending_payment',
                'paid',
                'processing',
                'shipped',
                'delivered',
                'cancelled',
                'refunded'
            )
            """,
            name="ck_orders_status",
        ),
        sa.CheckConstraint(
            "length(trim(order_no)) > 0",
            name="ck_orders_order_no_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(item_summary)) > 0",
            name="ck_orders_item_summary_not_blank",
        ),
        sa.CheckConstraint(
            "total_amount_cents >= 0",
            name="ck_orders_non_negative_amount",
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_orders_currency",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_orders_customer",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "order_no",
            name="uq_orders_org_order_no",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_orders_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            "customer_id",
            name="uq_orders_org_id_customer",
        ),
    )
    op.create_index(
        "idx_orders_org_customer_placed",
        "orders",
        ["organization_id", "customer_id", "placed_at"],
        unique=False,
    )
    op.create_index(
        "idx_orders_org_status",
        "orders",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_orders_org_status", table_name="orders")
    op.drop_index("idx_orders_org_customer_placed", table_name="orders")
    op.drop_table("orders")
