from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0009_knowledge_base"
down_revision: str | None = "0008_ticket_comments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("active_version_id", sa.Text(), nullable=True),
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
            "source_type IN ('markdown', 'text')",
            name="ck_documents_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'active', 'disabled', 'failed')",
            name="ck_documents_status",
        ),
        sa.CheckConstraint(
            "length(trim(title)) > 0",
            name="ck_documents_title_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "uploaded_by_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_documents_uploader_membership",
        ),
        # A document may only activate a version that belongs to the same
        # organization and document; NULL keeps the previous active version.
        sa.ForeignKeyConstraint(
            ["organization_id", "id", "active_version_id"],
            [
                "document_versions.organization_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
            name="fk_documents_active_version",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_documents_org_id",
        ),
    )

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("loader_version", sa.Text(), nullable=False),
        sa.Column("chunker_version", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "version_no > 0",
            name="ck_document_versions_positive_version_no",
        ),
        sa.CheckConstraint(
            "embedding_dimensions > 0",
            name="ck_document_versions_positive_dimensions",
        ),
        sa.CheckConstraint(
            "length(trim(content_hash)) > 0",
            name="ck_document_versions_content_hash_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["documents.organization_id", "documents.id"],
            name="fk_document_versions_document",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_versions"),
        sa.UniqueConstraint(
            "organization_id",
            "document_id",
            "id",
            name="uq_document_versions_org_document_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "document_id",
            "version_no",
            name="uq_document_versions_org_document_version_no",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "document_id",
            "content_hash",
            name="uq_document_versions_org_document_hash",
        ),
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("version_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_document_chunks_non_negative_ordinal",
        ),
        sa.CheckConstraint(
            "token_count >= 0",
            name="ck_document_chunks_non_negative_token_count",
        ),
        sa.CheckConstraint(
            "start_offset >= 0",
            name="ck_document_chunks_non_negative_start",
        ),
        sa.CheckConstraint(
            "end_offset > start_offset",
            name="ck_document_chunks_end_after_start",
        ),
        sa.CheckConstraint(
            "length(trim(content)) > 0",
            name="ck_document_chunks_content_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "document_id", "version_id"],
            [
                "document_versions.organization_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
            name="fk_document_chunks_version",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_chunks"),
        sa.UniqueConstraint(
            "organization_id",
            "version_id",
            "ordinal",
            name="uq_document_chunks_org_version_ordinal",
        ),
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("version_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_ingestion_jobs_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_ingestion_jobs_non_negative_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "document_id", "version_id"],
            [
                "document_versions.organization_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
            name="fk_ingestion_jobs_version",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_jobs"),
    )

    op.create_table(
        "retrieval_events",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=True),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("original_query", sa.Text(), nullable=False),
        sa.Column("planned_queries_json", sa.Text(), nullable=False),
        sa.Column("round_count", sa.Integer(), nullable=False),
        sa.Column("candidate_json", sa.Text(), nullable=False),
        sa.Column("selected_chunk_ids_json", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "strategy IN ('baseline', 'single', 'multi', 'none')",
            name="ck_retrieval_events_strategy",
        ),
        sa.CheckConstraint(
            "outcome IN ('sufficient', 'insufficient', 'failed')",
            name="ck_retrieval_events_outcome",
        ),
        sa.CheckConstraint(
            "round_count >= 0",
            name="ck_retrieval_events_non_negative_rounds",
        ),
        sa.CheckConstraint(
            "latency_ms >= 0",
            name="ck_retrieval_events_non_negative_latency",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_retrieval_events"),
    )

    op.create_index(
        "idx_documents_org_status",
        "documents",
        ["organization_id", "status"],
        unique=False,
    )
    op.create_index(
        "idx_versions_org_document",
        "document_versions",
        ["organization_id", "document_id", "version_no"],
        unique=False,
    )
    op.create_index(
        "idx_chunks_org_version",
        "document_chunks",
        ["organization_id", "document_id", "version_id", "ordinal"],
        unique=False,
    )
    op.create_index(
        "idx_jobs_org_document_created",
        "ingestion_jobs",
        ["organization_id", "document_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_retrieval_events_org_created",
        "retrieval_events",
        ["organization_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_retrieval_events_org_created", table_name="retrieval_events")
    op.drop_table("retrieval_events")
    op.drop_index("idx_jobs_org_document_created", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
    op.drop_index("idx_chunks_org_version", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("idx_versions_org_document", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_index("idx_documents_org_status", table_name="documents")
    op.drop_table("documents")
