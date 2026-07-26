from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("username", sa.Text(collation="NOCASE"), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("username"),
    )
    op.create_index(
        "idx_users_username",
        "users",
        ["username"],
        unique=True,
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=True),
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
    )
    op.create_index(
        "idx_conversations_user",
        "conversations",
        ["user_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "seq",
            name="uq_messages_conversation_seq",
        ),
    )
    op.create_index(
        "idx_messages_conversation_seq",
        "messages",
        ["conversation_id", "seq"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_messages_conversation_seq", table_name="messages")
    op.drop_table("messages")
    op.drop_index("idx_conversations_user", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("idx_users_username", table_name="users")
    op.drop_table("users")
