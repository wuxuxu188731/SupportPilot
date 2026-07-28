from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006_shipments"
down_revision: str | None = "0005_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shipments",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("shipment_no", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("carrier", sa.Text(), nullable=False),
        sa.Column("tracking_no", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("last_event", sa.Text(), nullable=True),
        sa.Column("shipped_at", sa.Text(), nullable=True),
        sa.Column("estimated_delivery_at", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.Text(), nullable=True),
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
                'pending',
                'picked_up',
                'in_transit',
                'delivered',
                'exception',
                'returned'
            )
            """,
            name="ck_shipments_status",
        ),
        sa.CheckConstraint(
            "length(trim(shipment_no)) > 0",
            name="ck_shipments_shipment_no_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(carrier)) > 0",
            name="ck_shipments_carrier_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(tracking_no)) > 0",
            name="ck_shipments_tracking_no_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            name="fk_shipments_order",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "shipment_no",
            name="uq_shipments_org_shipment_no",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "tracking_no",
            name="uq_shipments_org_tracking_no",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "order_id",
            name="uq_shipments_org_order",
        ),
    )
    op.create_index(
        "idx_shipments_org_status",
        "shipments",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_shipments_org_status", table_name="shipments")
    op.drop_table("shipments")
