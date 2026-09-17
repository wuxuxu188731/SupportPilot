"""引用随回答持久化：新增 ``message_citations`` 表与 ``messages.answer_incomplete`` 列。

背景：引用（``Citation``）此前只活在本轮 HTTP 响应里——刷新页面或重新进入会话后，
历史接口只返回问答文本，引用卡片与「查看原文位置」入口全部消失。本迁移把「回答当时
依据的证据」变成耐久记录，让刷新后的历史回答与实时展示完全一致。

为什么新建独立表，而不是给 ``messages`` 加一列 JSON：

* 一条引用一行，列名与 ``Citation`` 的公开字段一一对应（含证据片段快照 ``content``），
  **读表结构即懂**；字段演进是显式、可评审的「加列 + 迁移」，不会悄悄藏进一个不透明 blob；
* 引用与消息在同一事务写入，不会出现「有消息没引用」的半截状态；
* 将来若要按文档/版本反查「哪些回答引用了这份文档」，加一条索引即可。

为什么 ``answer_incomplete`` 放在 ``messages`` 而不是引用表：它是**回答级**属性
（这一轮引用校验是否判定不完整），而且「漏引」场景下可能一条引用都没有却仍为 ``true``；
硬塞进引用表只能造一条空引用行，语义会被扭曲。取值只允许 ``0`` / ``1``，
``NULL`` 只剩一个含义——「没有记录」（带 ``tool_calls`` 的中间助手消息、``system`` /
``tool`` 消息，以及升级前的历史行）。读侧把 ``NULL`` 与 ``0`` 一律当 ``false``。

为什么用 ``(conversation_id, seq)`` 关联而不是 ``messages.id``：``seq`` 是仓库里既有的
稳定消息业务标识（``uq_messages_conversation_seq``），``append_messages`` 本来就自己
计算 ``seq``；改用自增主键则要 ``RETURNING`` / ``lastrowid`` 回填，批量插入时更易写错。
历史读路径本来就按 ``seq`` 排序，口径统一。

为什么用两条外键（``conversation_id`` 与 ``(conversation_id, seq)``）：
防止引用行成为孤儿。SQLite 连接已开 ``PRAGMA foreign_keys = ON``。
**刻意不设 ``ON DELETE CASCADE``**：会话/消息删除接口目前不存在，本次不预置级联，
以免掩盖「删会话时引用怎么办」这个决策；将来新增删除能力时再显式决定。

降级会丢什么：``downgrade()`` 会删除 **全部引用行** 与 ``messages.answer_incomplete``
列（消息本体不受影响）。这类数据一旦回滚就无法恢复（当时就没别处存），因此与其他
破坏性迁移（0010 / 0012）保持一致：**库里已经存在引用行时拒绝降级**，
只允许在「没有任何引用数据」的库上对称回滚。
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0014_message_citations"
down_revision: str | None = "0013_document_versions_async_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_citation_rows() -> bool:
    """库里是否已经存在引用行（存在则拒绝降级，避免静默丢弃耐久证据）。"""
    connection = op.get_bind()
    row = connection.execute(
        sa.text("SELECT 1 FROM message_citations LIMIT 1")
    ).first()
    return row is not None


def upgrade() -> None:
    op.create_table(
        "message_citations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        # 引用所属消息的 messages.seq；只有 assistant 最终回答会有引用行。
        sa.Column("seq", sa.Integer(), nullable=False),
        # 引用在该回答中的展示次序（1..n），等于实时响应里 citations 数组的顺序
        # （即 [C#] 在回答正文中首次出现的顺序），**不等于** citation_id 的数字顺序。
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("citation_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=False),
        # 生成时冻结的版本标识：读取历史引用时按它取正文，绝不重新解析到当前有效版本。
        sa.Column("version_id", sa.Text(), nullable=False),
        sa.Column("chunk_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("heading_path", sa.Text(), nullable=True),
        # 证据片段快照：来自知识库的可信片段正文（非模型生成），刷新后卡片直接展示。
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_message_citations_conversation",
        ),
        # 复用既有复合唯一约束 uq_messages_conversation_seq，防止引用行成为孤儿。
        sa.ForeignKeyConstraint(
            ["conversation_id", "seq"],
            ["messages.conversation_id", "messages.seq"],
            name="fk_message_citations_message",
        ),
        # 防同一回答重复写同一引用（重试幂等）。
        sa.UniqueConstraint(
            "conversation_id",
            "seq",
            "citation_id",
            name="uq_message_citations_answer_citation",
        ),
        # 保证展示顺序唯一。
        sa.UniqueConstraint(
            "conversation_id",
            "seq",
            "ordinal",
            name="uq_message_citations_answer_ordinal",
        ),
        # 空标签无法与正文 [C#] 对应。
        sa.CheckConstraint(
            "length(trim(citation_id)) > 0",
            name="ck_message_citations_citation_id_not_blank",
        ),
        # 展示顺序从 1 开始，挡住「从 0 开始」这类口径漂移。
        sa.CheckConstraint(
            "ordinal >= 1",
            name="ck_message_citations_ordinal_positive",
        ),
        # 偏移「同生同灭」：不允许只写一个偏移。
        sa.CheckConstraint(
            "(start_offset IS NULL) = (end_offset IS NULL)",
            name="ck_message_citations_offsets_paired",
        ),
        # 挡住反向区间与零长区间。
        sa.CheckConstraint(
            "start_offset IS NULL OR end_offset > start_offset",
            name="ck_message_citations_offsets_ordered",
        ),
        # 挡住负偏移。
        sa.CheckConstraint(
            "start_offset IS NULL OR start_offset >= 0",
            name="ck_message_citations_offsets_non_negative",
        ),
        # 空证据片段（口径与 document_chunks 的同名约束一致）。
        sa.CheckConstraint(
            "length(trim(content)) > 0",
            name="ck_message_citations_content_not_blank",
        ),
    )
    # 本次读路径唯一需要的索引；将来做「哪些回答引用了这份文档」的反查时，
    # 再加一条按 (document_id, version_id) 的索引即可。
    op.create_index(
        "idx_message_citations_conversation_seq",
        "message_citations",
        ["conversation_id", "seq"],
        unique=False,
    )

    # 可空、无默认值 → SQLite 直接 ADD COLUMN，无需 batch_alter_table 重建 messages。
    op.add_column(
        "messages",
        sa.Column("answer_incomplete", sa.Integer(), nullable=True),
    )

    # SQLite 无法用 ALTER TABLE 追加带 CHECK 的约束，必须整表重建。
    # messages 没有入向外键（引用表指向它，但那是出向），重建安全；
    # 重建时把既有唯一约束与索引显式带上，避免丢失。
    with op.batch_alter_table("messages", recreate="always") as batch_op:
        batch_op.create_check_constraint(
            "ck_messages_answer_incomplete_value",
            "answer_incomplete IS NULL OR answer_incomplete IN (0, 1)",
        )


def downgrade() -> None:
    if _has_citation_rows():
        raise RuntimeError(
            "message_citations 已存在引用数据，拒绝降级以避免丢失持久化的引用"
        )

    # 先摘掉 messages 上的 CHECK（同样需要整表重建），再删列与表。
    with op.batch_alter_table("messages", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "ck_messages_answer_incomplete_value", type_="check"
        )
        batch_op.drop_column("answer_incomplete")

    op.drop_index(
        "idx_message_citations_conversation_seq",
        table_name="message_citations",
    )
    op.drop_table("message_citations")
