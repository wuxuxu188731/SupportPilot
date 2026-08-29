"""退款/补偿审批与可靠执行工作流的业务表。"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0011_action_approval_workflow"
down_revision: str | None = "0010_retrieval_event_unplanned"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "action_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("turn_id", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.Text(), nullable=False),
        sa.Column("workflow_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column(
            "last_error_retryable",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
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
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "workflow_type IN ('refund', 'compensation')",
            name="ck_action_runs_workflow_type",
        ),
        sa.CheckConstraint(
            """
            status IN (
                'queued',
                'running',
                'awaiting_approval',
                'succeeded',
                'failed',
                'cancelled'
            )
            """,
            name="ck_action_runs_status",
        ),
        sa.CheckConstraint(
            "last_error_retryable IN (0, 1)",
            name="ck_action_runs_retryable_flag",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_action_runs_creator_membership",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_action_runs_org_id",
        ),
        sa.UniqueConstraint(
            "thread_id",
            name="uq_action_runs_thread_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "conversation_id",
            "turn_id",
            "workflow_type",
            name="uq_action_runs_org_conversation_turn_type",
        ),
    )

    op.create_table(
        "action_proposals",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_version_id", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Text(), nullable=False),
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
            "action_type IN ('refund', 'compensation')",
            name="ck_action_proposals_action_type",
        ),
        sa.CheckConstraint(
            """
            status IN (
                'awaiting_approval',
                'approved',
                'executing',
                'succeeded',
                'rejected',
                'failed',
                'cancelled'
            )
            """,
            name="ck_action_proposals_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["action_runs.organization_id", "action_runs.id"],
            name="fk_action_proposals_run",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            name="fk_action_proposals_order",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_action_proposals_creator_membership",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_action_proposals_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "run_id",
            name="uq_action_proposals_org_run",
        ),
    )
    # 同一订单和动作类型最多存在一个非终态提案，用于抑制聊天重试造成的重复待审批项。
    op.create_index(
        "uq_action_proposals_org_order_type_active",
        "action_proposals",
        ["organization_id", "order_id", "action_type"],
        unique=True,
        sqlite_where=sa.text(
            "status IN ('awaiting_approval', 'approved', 'executing')"
        ),
    )

    op.create_table(
        "action_proposal_versions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=False),
        sa.Column("parameters_json", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "amount_cents > 0",
            name="ck_action_proposal_versions_positive_amount",
        ),
        sa.CheckConstraint(
            "version_no > 0",
            name="ck_action_proposal_versions_positive_version_no",
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_action_proposal_versions_currency",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "proposal_id"],
            ["action_proposals.organization_id", "action_proposals.id"],
            name="fk_action_proposal_versions_proposal",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_action_proposal_versions_creator_membership",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "proposal_id",
            "version_no",
            name="uq_action_proposal_versions_org_proposal_version",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_action_proposal_versions_org_id",
        ),
    )

    op.create_table(
        "approvals",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=False),
        sa.Column("requested_version_id", sa.Text(), nullable=False),
        sa.Column("requested_by_user_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("decided_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'approved_with_changes', 'rejected')",
            name="ck_approvals_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "proposal_id"],
            ["action_proposals.organization_id", "action_proposals.id"],
            name="fk_approvals_proposal",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "requested_version_id"],
            [
                "action_proposal_versions.organization_id",
                "action_proposal_versions.id",
            ],
            name="fk_approvals_requested_version",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "requested_by_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_approvals_requester_membership",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_approvals_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "proposal_id",
            name="uq_approvals_org_proposal",
        ),
    )

    op.create_table(
        "approval_decisions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("approval_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("decided_version_id", sa.Text(), nullable=False),
        sa.Column("decided_by_user_id", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'approved_with_changes', 'rejected')",
            name="ck_approval_decisions_decision",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "approval_id"],
            ["approvals.organization_id", "approvals.id"],
            name="fk_approval_decisions_approval",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "decided_version_id"],
            [
                "action_proposal_versions.organization_id",
                "action_proposal_versions.id",
            ],
            name="fk_approval_decisions_version",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "decided_by_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_approval_decisions_decider_membership",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_approval_decisions_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "approval_id",
            name="uq_approval_decisions_org_approval",
        ),
    )

    op.create_table(
        "tool_executions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=False),
        sa.Column("proposal_version_id", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "error_retryable",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "claimed_at",
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
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "action_type IN ('refund', 'compensation')",
            name="ck_tool_executions_action_type",
        ),
        sa.CheckConstraint(
            """
            status IN (
                'claimed',
                'running',
                'succeeded',
                'failed_retryable',
                'failed_terminal'
            )
            """,
            name="ck_tool_executions_status",
        ),
        sa.CheckConstraint(
            "attempt_count > 0",
            name="ck_tool_executions_positive_attempt",
        ),
        sa.CheckConstraint(
            "error_retryable IN (0, 1)",
            name="ck_tool_executions_retryable_flag",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "proposal_id"],
            ["action_proposals.organization_id", "action_proposals.id"],
            name="fk_tool_executions_proposal",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "proposal_version_id"],
            [
                "action_proposal_versions.organization_id",
                "action_proposal_versions.id",
            ],
            name="fk_tool_executions_version",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_tool_executions_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_tool_executions_org_idempotency_key",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "proposal_version_id",
            name="uq_tool_executions_org_proposal_version",
        ),
    )

    op.create_table(
        "refund_records",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=False),
        sa.Column("proposal_version_id", sa.Text(), nullable=False),
        sa.Column("tool_execution_id", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "amount_cents > 0",
            name="ck_refund_records_positive_amount",
        ),
        sa.CheckConstraint(
            "status IN ('simulated_succeeded')",
            name="ck_refund_records_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            name="fk_refund_records_order",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "proposal_id"],
            ["action_proposals.organization_id", "action_proposals.id"],
            name="fk_refund_records_proposal",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "proposal_version_id"],
            [
                "action_proposal_versions.organization_id",
                "action_proposal_versions.id",
            ],
            name="fk_refund_records_version",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "tool_execution_id"],
            ["tool_executions.organization_id", "tool_executions.id"],
            name="fk_refund_records_execution",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "tool_execution_id",
            name="uq_refund_records_org_execution",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "proposal_version_id",
            name="uq_refund_records_org_version",
        ),
    )

    op.create_table(
        "compensation_records",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=False),
        sa.Column("proposal_version_id", sa.Text(), nullable=False),
        sa.Column("tool_execution_id", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("coupon_valid_days", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "amount_cents > 0",
            name="ck_compensation_records_positive_amount",
        ),
        sa.CheckConstraint(
            "coupon_valid_days = 30",
            name="ck_compensation_records_coupon_valid_days",
        ),
        sa.CheckConstraint(
            "status IN ('simulated_succeeded')",
            name="ck_compensation_records_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            name="fk_compensation_records_order",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "proposal_id"],
            ["action_proposals.organization_id", "action_proposals.id"],
            name="fk_compensation_records_proposal",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "proposal_version_id"],
            [
                "action_proposal_versions.organization_id",
                "action_proposal_versions.id",
            ],
            name="fk_compensation_records_version",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "tool_execution_id"],
            ["tool_executions.organization_id", "tool_executions.id"],
            name="fk_compensation_records_execution",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "tool_execution_id",
            name="uq_compensation_records_org_execution",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "proposal_version_id",
            name="uq_compensation_records_org_version",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "order_id",
            "reason_code",
            name="uq_compensation_records_org_order_reason",
        ),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=True),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'system')",
            name="ck_audit_logs_actor_type",
        ),
        sa.CheckConstraint(
            "actor_type != 'user' OR actor_user_id IS NOT NULL",
            name="ck_audit_logs_user_actor",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["action_runs.organization_id", "action_runs.id"],
            name="fk_audit_logs_run",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "proposal_id"],
            ["action_proposals.organization_id", "action_proposals.id"],
            name="fk_audit_logs_proposal",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_audit_logs_actor_membership",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_audit_logs_org_id",
        ),
    )

    op.create_index(
        "idx_action_runs_org_created",
        "action_runs",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_action_proposals_org_order",
        "action_proposals",
        ["organization_id", "order_id"],
        unique=False,
    )
    op.create_index(
        "idx_approvals_org_status_created",
        "approvals",
        ["organization_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_tool_executions_org_proposal",
        "tool_executions",
        ["organization_id", "proposal_id"],
        unique=False,
    )
    op.create_index(
        "idx_audit_logs_org_run_created",
        "audit_logs",
        ["organization_id", "run_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_refund_records_org_order",
        "refund_records",
        ["organization_id", "order_id"],
        unique=False,
    )
    op.create_index(
        "idx_compensation_records_org_order",
        "compensation_records",
        ["organization_id", "order_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_compensation_records_org_order",
        table_name="compensation_records",
    )
    op.drop_index("idx_refund_records_org_order", table_name="refund_records")
    op.drop_index("idx_audit_logs_org_run_created", table_name="audit_logs")
    op.drop_index(
        "idx_tool_executions_org_proposal",
        table_name="tool_executions",
    )
    op.drop_index("idx_approvals_org_status_created", table_name="approvals")
    op.drop_index(
        "idx_action_proposals_org_order",
        table_name="action_proposals",
    )
    op.drop_index("idx_action_runs_org_created", table_name="action_runs")
    op.drop_table("audit_logs")
    op.drop_table("compensation_records")
    op.drop_table("refund_records")
    op.drop_table("tool_executions")
    op.drop_table("approval_decisions")
    op.drop_table("approvals")
    op.drop_table("action_proposal_versions")
    op.drop_index(
        "uq_action_proposals_org_order_type_active",
        table_name="action_proposals",
    )
    op.drop_table("action_proposals")
    op.drop_table("action_runs")
