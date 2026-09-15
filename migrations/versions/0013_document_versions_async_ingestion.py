"""支持入库异步化：版本行可以先「预留」，解析产物随后补齐。

背景：原入库流程在 HTTP 请求线程里同步跑「解析 → 分块 → 嵌入 → 写向量 → 激活」，
单份 DOCX/PDF 实测约 27 秒，大文档必然撞上前端 180 秒上传超时。异步化后接口只
创建 document/version/job 三行并立刻返回 ``queued``，解析与入库交给后台 worker。

这带来一个数据模型上的新状态：**版本行先存在、内容还没解析出来**。为此本迁移放开：

* ``content_hash`` 与 ``raw_text`` 允许 NULL —— 两者由同一次解析产出、同时回填，
  因此「是否已解析」只有一个判据：``content_hash IS NULL`` 即未解析；
* 新增 ``source_hash`` —— ``sha256(上传的原始字节)``，上传时即可算出。它承担三件事：
  1. 上传期的去重键（``(org, document, source_hash)`` 唯一约束），让「同一份文件重复上传」
     在解析之前就被拦下，省掉一次可能计费的解析；
  2. 与旧的 ``(org, document, content_hash)`` 唯一约束互补 —— SQLite 视多个 NULL 互不相等，
     所以尚未解析的版本行共存的 NULL 不会互相冲突；
  3. 上传原始字节的存放位置（``raw_bytes``），worker 解析完即清空，失败则保留供重试。

SQLite 不支持直接修改列的可空性，沿用 0010/0012 已验证过的做法：
``batch_alter_table(recreate="always")`` 重建整张表；``document_versions`` 有入向外键
（``document_chunks`` / ``ingestion_jobs`` / ``documents.active_version_id``），
重建时必须保持同样的写法。
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0013_document_versions_async_ingestion"
down_revision: str | None = "0012_documents_source_type_extended"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0009 建表时的原始约束，降级时要一字不差地还原。
CONTENT_HASH_NOT_BLANK_CHECK = "length(trim(content_hash)) > 0"


def _has_unparsed_version_rows() -> bool:
    """是否还存在「已预留但尚未解析」的版本行（``content_hash IS NULL``）。

    这类行是本迁移引入的新状态，0009 的表结构无法表达（``content_hash`` 非空）。
    降级会把这些行连同还在排队的解析任务一起抹掉，因此必须拒绝降级，
    由运维先决定是等 worker 跑完还是放弃这些上传。
    """
    connection = op.get_bind()
    row = connection.execute(
        sa.text(
            "SELECT 1 FROM document_versions WHERE content_hash IS NULL LIMIT 1"
        )
    ).first()
    return row is not None


def _add_source_hash_columns() -> None:
    """整表重建：放开 content_hash / raw_text 可空，并新增 source_hash / raw_bytes。"""
    with op.batch_alter_table(
        "document_versions",
        recreate="always",
    ) as batch_op:
        batch_op.add_column(sa.Column("source_hash", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("raw_bytes", sa.LargeBinary(), nullable=True))
        # 两张「解析产物」列一起放开：它们由同一次解析同时写入，可空性也必须一致，
        # 否则预留版本行时就会撞上 NOT NULL（这正是只放开 content_hash 的漏洞）。
        batch_op.alter_column(
            "content_hash",
            existing_type=sa.Text(),
            nullable=True,
        )
        batch_op.alter_column(
            "raw_text",
            existing_type=sa.Text(),
            nullable=True,
        )
        # 原约束在 content_hash 可为 NULL 时会因 trim(NULL) 求值成 NULL 而失效，
        # 显式改成「为空即通过，非空则不得是空白串」，保留对垃圾值的拦截。
        batch_op.drop_constraint(
            "ck_document_versions_content_hash_not_blank", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_document_versions_content_hash_not_blank",
            f"content_hash IS NULL OR {CONTENT_HASH_NOT_BLANK_CHECK}",
        )
        batch_op.create_unique_constraint(
            "uq_document_versions_org_document_source_hash",
            ["organization_id", "document_id", "source_hash"],
        )


def _drop_source_hash_columns() -> None:
    """还原成 0009 的原始形态：content_hash / raw_text 重新非空。"""
    with op.batch_alter_table(
        "document_versions",
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint(
            "uq_document_versions_org_document_source_hash", type_="unique"
        )
        batch_op.drop_constraint(
            "ck_document_versions_content_hash_not_blank", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_document_versions_content_hash_not_blank",
            CONTENT_HASH_NOT_BLANK_CHECK,
        )
        batch_op.alter_column(
            "content_hash",
            existing_type=sa.Text(),
            nullable=False,
        )
        batch_op.alter_column(
            "raw_text",
            existing_type=sa.Text(),
            nullable=False,
        )
        batch_op.drop_column("raw_bytes")
        batch_op.drop_column("source_hash")


def upgrade() -> None:
    _add_source_hash_columns()


def downgrade() -> None:
    if _has_unparsed_version_rows():
        raise RuntimeError(
            "存在尚未解析完成的版本行，拒绝降级以避免丢失排队中的入库任务"
        )
    _drop_source_hash_columns()
