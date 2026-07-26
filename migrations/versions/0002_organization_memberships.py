from typing import Sequence
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "0002_organization_memberships"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_table(
        "memberships",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "role IN ('admin', 'agent')",
            name="ck_memberships_role",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("organization_id", "user_id"),
    )
    op.create_index(
        "idx_memberships_user",
        "memberships",
        ["user_id", "organization_id"],
        unique=False,
    )

    connection = op.get_bind()
    users = connection.execute(
        sa.text("SELECT id, username FROM users ORDER BY id")
    ).mappings()
    for user in users:
        organization_id = str(uuid4())
        connection.execute(
            sa.text(
                """
                INSERT INTO organizations(id, name)
                VALUES (:organization_id, :name)
                """
            ),
            {
                "organization_id": organization_id,
                "name": f"{user['username']} organization",
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO memberships(organization_id, user_id, role)
                VALUES (:organization_id, :user_id, 'admin')
                """
            ),
            {
                "organization_id": organization_id,
                "user_id": user["id"],
            },
        )


def downgrade() -> None:
    op.drop_index("idx_memberships_user", table_name="memberships")
    op.drop_table("memberships")
    op.drop_table("organizations")
