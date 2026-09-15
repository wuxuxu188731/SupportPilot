"""放开 documents.source_type 的 CHECK 约束，纳入 word 与 pdf。

背景：0009 建表时把 source_type 约束为 ``IN ('markdown', 'text')``，领域枚举里
虽然一直有 ``word``，但数据库层从未放行——这既解释了「docx 上传不了」，
也意味着新增 ``pdf`` 必须先改约束。

SQLite 不支持直接修改 CHECK 约束，沿用 0010 已验证过的做法：
``batch_alter_table(recreate="always")`` 重建整张表。
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0012_documents_source_type_extended"
down_revision: str | None = "0011_action_approval_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0009 建表时的原始约束，降级时要一字不差地还原。
LEGACY_SOURCE_TYPES = "'markdown', 'text'"
# 新增 word（领域枚举早已存在）与 pdf（本次 loader 改造引入）。
EXTENDED_SOURCE_TYPES = LEGACY_SOURCE_TYPES + ", 'word', 'pdf'"
# 降级时会丢数据的取值：存在这些行时拒绝降级，而不是静默把约束改回去。
NON_LEGACY_SOURCE_TYPES = ("word", "pdf")


def _replace_source_type_check(allowed: str) -> None:
    with op.batch_alter_table("documents", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_documents_source_type", type_="check")
        batch_op.create_check_constraint(
            "ck_documents_source_type",
            f"source_type IN ({allowed})",
        )


def upgrade() -> None:
    _replace_source_type_check(EXTENDED_SOURCE_TYPES)


def downgrade() -> None:
    bind = op.get_bind()
    # 回退到只有 markdown/text 的约束会让 word/pdf 文档失去合法性，
    # 因此先确认库里没有这类数据；有则拒绝降级，交由运维决定如何处理。
    placeholders = ", ".join(f":value{index}" for index in range(len(NON_LEGACY_SOURCE_TYPES)))
    existing = bind.execute(
        sa.text(
            "SELECT count(*) FROM documents "
            f"WHERE source_type IN ({placeholders})"
        ),
        {
            f"value{index}": value
            for index, value in enumerate(NON_LEGACY_SOURCE_TYPES)
        },
    ).scalar_one()
    if existing:
        raise RuntimeError(
            "cannot downgrade documents.source_type while word/pdf rows exist"
        )
    _replace_source_type_check(LEGACY_SOURCE_TYPES)
