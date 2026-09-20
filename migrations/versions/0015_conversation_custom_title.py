"""为会话增加可持久化的手动标题。"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015_conversation_custom_title"
down_revision: str | None = "0014_message_citations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """旧会话保持空值，继续使用首条用户消息生成标题。"""
    op.add_column("conversations", sa.Column("custom_title", sa.Text(), nullable=True))


def downgrade() -> None:
    """已有手动标题时拒绝降级，避免静默丢失用户设置。"""
    connection = op.get_bind()
    existing = connection.execute(
        sa.text("SELECT 1 FROM conversations WHERE custom_title IS NOT NULL LIMIT 1")
    ).first()
    if existing is not None:
        raise RuntimeError("已有会话手动标题，降级会丢失数据")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("custom_title")
