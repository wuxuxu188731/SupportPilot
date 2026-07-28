from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_ticket_comments"
down_revision: str | None = "0007_tickets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ticket_comments",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("author_user_id", sa.Text(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "seq > 0",
            name="ck_ticket_comments_positive_seq",
        ),
        sa.CheckConstraint(
            "visibility IN ('internal', 'public')",
            name="ck_ticket_comments_visibility",
        ),
        sa.CheckConstraint(
            "length(trim(content)) > 0",
            name="ck_ticket_comments_content_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "ticket_id"],
            ["tickets.organization_id", "tickets.id"],
            name="fk_ticket_comments_ticket",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "author_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_ticket_comments_author_membership",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "ticket_id",
            "seq",
            name="uq_ticket_comments_org_ticket_seq",
        ),
    )
    op.create_index(
        "idx_ticket_comments_org_ticket_seq",
        "ticket_comments",
        ["organization_id", "ticket_id", "seq"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_ticket_comments_org_ticket_seq",
        table_name="ticket_comments",
    )
    op.drop_table("ticket_comments")
