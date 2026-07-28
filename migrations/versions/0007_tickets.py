from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0007_tickets"
down_revision: str | None = "0006_shipments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("ticket_no", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Text(), nullable=False),
        sa.Column("assigned_to_user_id", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
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
            category IN (
                'logistics',
                'refund',
                'damaged_item',
                'product_question',
                'other'
            )
            """,
            name="ck_tickets_category",
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'urgent')",
            name="ck_tickets_priority",
        ),
        sa.CheckConstraint(
            """
            status IN (
                'open',
                'in_progress',
                'pending_customer',
                'resolved',
                'closed'
            )
            """,
            name="ck_tickets_status",
        ),
        sa.CheckConstraint(
            "length(trim(ticket_no)) > 0",
            name="ck_tickets_ticket_no_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(summary)) > 0",
            name="ck_tickets_summary_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_tickets_customer",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "order_id", "customer_id"],
            [
                "orders.organization_id",
                "orders.id",
                "orders.customer_id",
            ],
            name="fk_tickets_order_customer",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_tickets_creator_membership",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "assigned_to_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_tickets_assignee_membership",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "ticket_no",
            name="uq_tickets_org_ticket_no",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_tickets_org_id",
        ),
    )
    op.create_index(
        "idx_tickets_org_status_priority",
        "tickets",
        ["organization_id", "status", "priority"],
        unique=False,
    )
    op.create_index(
        "idx_tickets_org_customer",
        "tickets",
        ["organization_id", "customer_id"],
        unique=False,
    )
    op.create_index(
        "idx_tickets_org_order",
        "tickets",
        ["organization_id", "order_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_tickets_org_order", table_name="tickets")
    op.drop_index("idx_tickets_org_customer", table_name="tickets")
    op.drop_index(
        "idx_tickets_org_status_priority",
        table_name="tickets",
    )
    op.drop_table("tickets")
