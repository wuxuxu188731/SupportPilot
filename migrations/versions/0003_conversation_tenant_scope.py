from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003_conversation_tenant_scope"
down_revision: str | None = "0002_organization_memberships"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("organization_id", sa.Text(), nullable=True),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE conversations
            SET organization_id = (
                SELECT m.organization_id
                FROM memberships AS m
                WHERE m.user_id = conversations.user_id
                ORDER BY m.created_at, m.organization_id
                LIMIT 1
            )
            WHERE organization_id IS NULL
            """
        )
    )
    unowned_count = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM conversations
            WHERE organization_id IS NULL
            """
        )
    ).scalar_one()
    if unowned_count:
        raise RuntimeError(
            f"{unowned_count} unowned legacy conversations require mapping"
        )

    with op.batch_alter_table("conversations") as batch:
        batch.alter_column(
            "organization_id",
            existing_type=sa.Text(),
            nullable=False,
        )
        batch.create_foreign_key(
            "fk_conversations_membership",
            "memberships",
            ["organization_id", "user_id"],
            ["organization_id", "user_id"],
        )
        batch.create_index(
            "idx_conversations_organization_user",
            ["organization_id", "user_id", "updated_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_index("idx_conversations_organization_user")
        batch.drop_constraint(
            "fk_conversations_membership",
            type_="foreignkey",
        )
        batch.drop_column("organization_id")
