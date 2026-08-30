"""退款/补偿动作工作流的 SQLite 存储实现。

本模块实现 ``ActionStore`` 协议的全部方法，是动作工作流唯一的业务读写入口。
所有写操作都在 ``BEGIN IMMEDIATE`` 事务中完成，保证状态转换、幂等认领、
金额上限重验、业务结果与审计事件的原子性；数据库层的唯一约束与触发器是
最终防线，本模块在事务内先行重验，提供稳定业务错误码。
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from app.actions.base import (
    ActionActiveProposalExistsError,
    ActionCompensationCapExceededError,
    ActionCompensationDuplicateError,
    ActionCurrencyMismatchError,
    ActionError,
    ActionInvalidAmountError,
    ActionOrderNotFoundError,
    ActionProposal,
    ActionProposalVersion,
    ActionRefundBalanceExceededError,
    ActionRun,
    ActionRunStatus,
    ActionStore,
    ActionType,
    Approval,
    ApprovalAdminRequiredError,
    ApprovalAlreadyDecidedError,
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalInvalidChangesError,
    ApprovalNotFoundError,
    ApprovalStatus,
    AuditActorType,
    AuditLog,
    DecisionResult,
    ExecutionClaim,
    ExecutionDataIntegrityError,
    ExecutionSuccess,
    CompensationReasonCode,
    NewProposalVersion,
    ProposalNotFoundError,
    ProposalStatus,
    RefundReasonCode,
    RefundScope,
    RunNotFoundError,
    RunStateConflictError,
    ToolExecution,
    ToolExecutionStatus,
    WorkflowCreation,
)
from app.db.migrations import upgrade_database

# —— 审计事件类型常量 ——
# 设计 9.9 要求至少记录：Run 创建、提案版本创建、进入等待、审批决定、
# 恢复请求、执行认领、执行成功/失败和终态。进入等待与终态事件由调用方
# 通过 transition_run 提供，其余事件由本模块内部固定写入。

EVENT_RUN_CREATED = "run_created"  # Run 创建事件
EVENT_PROPOSAL_VERSION_CREATED = "proposal_version_created"  # 提案版本创建事件
EVENT_APPROVAL_REQUESTED = "approval_requested"  # 审批请求创建事件
EVENT_DECISION_RECORDED = "decision_recorded"  # 审批决定落库事件
EVENT_EXECUTION_CLAIMED = "execution_claimed"  # 执行按幂等键认领事件
EVENT_EXECUTION_SUCCEEDED = "execution_succeeded"  # 执行成功事件
EVENT_EXECUTION_FAILED = "execution_failed"  # 执行失败事件
EVENT_RUN_SUCCEEDED = "run_succeeded"  # Run 成功终态事件
EVENT_RUN_FAILED = "run_failed"  # Run 失败事件

# 补偿优惠券有效期，本阶段固定为 30 天，数据库检查约束同步保护。
COMPENSATION_COUPON_VALID_DAYS = 30

# 创建与执行时按幂等键重复使用的错误信息片段，用于识别部分唯一索引冲突。
_ACTIVE_PROPOSAL_INDEX = "uq_action_proposals_org_order_type_active"
_SAME_TURN_INDEX = "uq_action_runs_org_conversation_turn_type"

# Run 的合法状态边由 Store 固定控制，调用者提供的 expected_statuses 只用于并发比较。
_RUN_TRANSITIONS = {
    ActionRunStatus.QUEUED: {
        ActionRunStatus.RUNNING,
        ActionRunStatus.FAILED,
    },
    ActionRunStatus.RUNNING: {
        ActionRunStatus.AWAITING_APPROVAL,
        ActionRunStatus.SUCCEEDED,
        ActionRunStatus.FAILED,
        ActionRunStatus.CANCELLED,
    },
    ActionRunStatus.AWAITING_APPROVAL: {
        ActionRunStatus.RUNNING,
        ActionRunStatus.FAILED,
    },
    ActionRunStatus.FAILED: {ActionRunStatus.RUNNING},
    ActionRunStatus.SUCCEEDED: set(),
    ActionRunStatus.CANCELLED: set(),
}


class SQLiteActionStore(ActionStore):
    """动作工作流 SQLite 存储，基于 BEGIN IMMEDIATE 事务与统一时钟。"""

    def __init__(self, database_path: str | Path):
        self._database_path = database_path
        upgrade_database(database_path)

    # —— 连接与事务管理 ——

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """只读查询使用的普通连接，随上下文自动提交或回滚。"""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务连接。

        显式 BEGIN IMMEDIATE 预先取得写锁，让并发写事务串行化，
        避免「先读后写」之间的竞争窗口，保证金额重验与状态转换互不干扰。
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _utc_now() -> str:
        """统一时钟：所有动作业务时间使用同一 UTC ISO 文本格式。"""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # —— 行转换 ——

    @staticmethod
    def _to_run(row: sqlite3.Row) -> ActionRun:
        return ActionRun(
            run_id=row["id"],
            organization_id=row["organization_id"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            created_by_user_id=row["created_by_user_id"],
            workflow_type=ActionType(row["workflow_type"]),
            status=ActionRunStatus(row["status"]),
            thread_id=row["thread_id"],
            proposal_id=row["proposal_id"],
            last_error_code=row["last_error_code"],
            last_error_retryable=bool(row["last_error_retryable"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _to_proposal(row: sqlite3.Row) -> ActionProposal:
        return ActionProposal(
            proposal_id=row["id"],
            organization_id=row["organization_id"],
            run_id=row["run_id"],
            order_id=row["order_id"],
            action_type=ActionType(row["action_type"]),
            status=ProposalStatus(row["status"]),
            current_version_id=row["current_version_id"],
            created_by_user_id=row["created_by_user_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_version(row: sqlite3.Row) -> ActionProposalVersion:
        return ActionProposalVersion(
            version_id=row["id"],
            organization_id=row["organization_id"],
            proposal_id=row["proposal_id"],
            version_no=row["version_no"],
            amount_cents=row["amount_cents"],
            currency=row["currency"],
            reason_code=row["reason_code"],
            reason_text=row["reason_text"],
            parameters_json=row["parameters_json"],
            created_by_user_id=row["created_by_user_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_approval(row: sqlite3.Row) -> Approval:
        return Approval(
            approval_id=row["id"],
            organization_id=row["organization_id"],
            proposal_id=row["proposal_id"],
            requested_version_id=row["requested_version_id"],
            requested_by_user_id=row["requested_by_user_id"],
            status=ApprovalStatus(row["status"]),
            created_at=row["created_at"],
            decided_at=row["decided_at"],
        )

    @staticmethod
    def _to_decision(row: sqlite3.Row) -> ApprovalDecision:
        return ApprovalDecision(
            decision_id=row["id"],
            organization_id=row["organization_id"],
            approval_id=row["approval_id"],
            decision=ApprovalDecisionType(row["decision"]),
            decided_version_id=row["decided_version_id"],
            decided_by_user_id=row["decided_by_user_id"],
            comment=row["comment"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_execution(row: sqlite3.Row) -> ToolExecution:
        return ToolExecution(
            execution_id=row["id"],
            organization_id=row["organization_id"],
            proposal_id=row["proposal_id"],
            proposal_version_id=row["proposal_version_id"],
            action_type=ActionType(row["action_type"]),
            idempotency_key=row["idempotency_key"],
            status=ToolExecutionStatus(row["status"]),
            attempt_count=row["attempt_count"],
            result_json=row["result_json"],
            error_code=row["error_code"],
            error_retryable=bool(row["error_retryable"]),
            claimed_at=row["claimed_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _to_audit_log(row: sqlite3.Row) -> AuditLog:
        return AuditLog(
            log_id=row["id"],
            organization_id=row["organization_id"],
            run_id=row["run_id"],
            proposal_id=row["proposal_id"],
            actor_type=AuditActorType(row["actor_type"]),
            actor_user_id=row["actor_user_id"],
            event_type=row["event_type"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            details_json=row["details_json"],
            created_at=row["created_at"],
        )

    # —— 内部辅助方法 ——

    @staticmethod
    def _validate_action_inputs(
        *,
        action_type: ActionType,
        reason_code: str,
        reason_text: str,
        parameters_json: str,
        approval_changes: bool = False,
    ) -> dict:
        """严格校验原因枚举和类型专属参数，返回解析后的参数对象。"""
        error_type = ApprovalInvalidChangesError if approval_changes else ActionError
        try:
            parsed = json.loads(parameters_json)
        except json.JSONDecodeError as exc:
            raise error_type("parameters_json 必须是合法 JSON") from exc
        if not isinstance(parsed, dict):
            raise error_type("parameters_json 必须是 JSON 对象")

        if action_type is ActionType.REFUND:
            try:
                reason = RefundReasonCode(reason_code)
            except ValueError as exc:
                raise error_type("退款原因码不在固定枚举中") from exc
            if set(parsed) != {"refund_scope"}:
                raise error_type("退款参数只能包含 refund_scope")
            try:
                RefundScope(parsed["refund_scope"])
            except (TypeError, ValueError) as exc:
                raise error_type("refund_scope 必须是 full 或 partial") from exc
        else:
            try:
                reason = CompensationReasonCode(reason_code)
            except ValueError as exc:
                raise error_type("补偿原因码不在固定枚举中") from exc
            if set(parsed) != {"coupon_valid_days"}:
                raise error_type("补偿参数只能包含 coupon_valid_days")
            if (
                type(parsed["coupon_valid_days"]) is not int
                or parsed["coupon_valid_days"]
                != COMPENSATION_COUPON_VALID_DAYS
            ):
                raise error_type("补偿优惠券有效期必须为 30 天")
        if reason.value == "other" and not reason_text.strip():
            raise error_type("other 原因必须提供非空详细说明")
        return parsed

    @staticmethod
    def _load_order_row(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        order_id: str,
    ) -> sqlite3.Row:
        """按租户读取订单，不存在时抛稳定业务错误。"""
        row = connection.execute(
            """
            SELECT id, organization_id, status, total_amount_cents, currency
            FROM orders
            WHERE organization_id = ? AND id = ?
            """,
            (organization_id, order_id),
        ).fetchone()
        if row is None:
            raise ActionOrderNotFoundError("订单不存在或不属于当前企业")
        return row

    @staticmethod
    def _sum_refunded_cents(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        order_id: str,
    ) -> int:
        """统计订单已成功退款累计金额（分）。"""
        row = connection.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM refund_records
            WHERE organization_id = ? AND order_id = ?
            """,
            (organization_id, order_id),
        ).fetchone()
        return int(row["total"])

    @staticmethod
    def _sum_compensated_cents(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        order_id: str,
    ) -> int:
        """统计订单已成功补偿累计金额（分）。"""
        row = connection.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM compensation_records
            WHERE organization_id = ? AND order_id = ?
            """,
            (organization_id, order_id),
        ).fetchone()
        return int(row["total"])

    @staticmethod
    def _validate_refund_amount(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        order_id: str,
        amount_cents: int,
        refund_scope: RefundScope,
    ) -> None:
        """重验退款金额不超过当前可退余额（与写结果同事务，防金额竞争）。"""
        order_row = SQLiteActionStore._load_order_row(
            connection,
            organization_id=organization_id,
            order_id=order_id,
        )
        refunded = SQLiteActionStore._sum_refunded_cents(
            connection,
            organization_id=organization_id,
            order_id=order_id,
        )
        remaining = order_row["total_amount_cents"] - refunded
        if amount_cents > remaining:
            raise ActionRefundBalanceExceededError(
                "退款金额超过订单当前可退余额"
            )
        if refund_scope is RefundScope.FULL and amount_cents != remaining:
            raise ActionInvalidAmountError(
                "全额退款金额必须等于当前可退余额"
            )

    @staticmethod
    def _validate_compensation_amount(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        order_id: str,
        amount_cents: int,
        reason_code: str,
    ) -> None:
        """重验补偿重复原因与 50% 累计上限（与写结果同事务，防金额竞争）。"""
        order_row = SQLiteActionStore._load_order_row(
            connection,
            organization_id=organization_id,
            order_id=order_id,
        )
        duplicate = connection.execute(
            """
            SELECT 1
            FROM compensation_records
            WHERE organization_id = ? AND order_id = ? AND reason_code = ?
            LIMIT 1
            """,
            (organization_id, order_id, reason_code),
        ).fetchone()
        if duplicate is not None:
            raise ActionCompensationDuplicateError(
                "同一订单同一补偿原因只能成功补偿一次"
            )
        compensated = SQLiteActionStore._sum_compensated_cents(
            connection,
            organization_id=organization_id,
            order_id=order_id,
        )
        # 50% 上限使用向下取整。
        cap_cents = order_row["total_amount_cents"] // 2
        if compensated + amount_cents > cap_cents:
            raise ActionCompensationCapExceededError(
                "补偿累计金额超过订单总金额的 50%"
            )

    @staticmethod
    def _load_run_row(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        run_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT *
            FROM action_runs
            WHERE organization_id = ? AND id = ?
            """,
            (organization_id, run_id),
        ).fetchone()
        if row is None:
            raise RunNotFoundError("动作 Run 不存在或不属于当前企业")
        return row

    @staticmethod
    def _load_proposal_row(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        proposal_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT *
            FROM action_proposals
            WHERE organization_id = ? AND id = ?
            """,
            (organization_id, proposal_id),
        ).fetchone()
        if row is None:
            raise ProposalNotFoundError("提案不存在或不属于当前企业")
        return row

    @staticmethod
    def _load_version_row(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        version_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT *
            FROM action_proposal_versions
            WHERE organization_id = ? AND id = ?
            """,
            (organization_id, version_id),
        ).fetchone()
        if row is None:
            raise ExecutionDataIntegrityError("提案版本不存在")
        return row

    @staticmethod
    def _append_audit(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        run_id: str,
        proposal_id: str | None,
        actor_type: AuditActorType,
        actor_user_id: str | None,
        event_type: str,
        resource_type: str,
        resource_id: str,
        details_json: str,
        created_at: str,
    ) -> None:
        """在已开启的事务内追加一条审计事件。"""
        try:
            details = json.loads(details_json)
        except json.JSONDecodeError as exc:
            raise ExecutionDataIntegrityError(
                "审计详情必须是合法 JSON"
            ) from exc
        if not isinstance(details, dict):
            raise ExecutionDataIntegrityError(
                "审计详情必须是 JSON 对象"
            )
        if proposal_id is not None:
            linked = connection.execute(
                """
                SELECT 1
                FROM action_proposals
                WHERE organization_id = ? AND id = ? AND run_id = ?
                LIMIT 1
                """,
                (organization_id, proposal_id, run_id),
            ).fetchone()
            if linked is None:
                raise ExecutionDataIntegrityError(
                    "审计提案不属于指定 Run"
                )
        if actor_type is AuditActorType.SYSTEM:
            actor_user_id = None
        log_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO audit_logs(
                id, organization_id, run_id, proposal_id, actor_type,
                actor_user_id, event_type, resource_type, resource_id,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                organization_id,
                run_id,
                proposal_id,
                actor_type.value,
                actor_user_id,
                event_type,
                resource_type,
                resource_id,
                details_json,
                created_at,
            ),
        )

    @staticmethod
    def _transition_proposal(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        proposal_id: str,
        allowed_statuses: set[ProposalStatus],
        new_status: ProposalStatus,
        updated_at: str,
    ) -> sqlite3.Row:
        """在事务内按允许集合原子转换提案状态。"""
        proposal = SQLiteActionStore._load_proposal_row(
            connection,
            organization_id=organization_id,
            proposal_id=proposal_id,
        )
        if ProposalStatus(proposal["status"]) not in allowed_statuses:
            raise RunStateConflictError(
                "提案状态不允许本次转换"
            )
        connection.execute(
            """
            UPDATE action_proposals
            SET status = ?, updated_at = ?
            WHERE organization_id = ? AND id = ?
            """,
            (new_status.value, updated_at, organization_id, proposal_id),
        )
        return SQLiteActionStore._load_proposal_row(
            connection,
            organization_id=organization_id,
            proposal_id=proposal_id,
        )

    @staticmethod
    def _transition_run_row(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        run_id: str,
        allowed_statuses: set[ActionRunStatus],
        new_status: ActionRunStatus,
        error_code: str | None,
        error_retryable: bool,
        updated_at: str,
    ) -> sqlite3.Row:
        """在事务内按允许集合原子转换 Run 状态。

        failed + 可重试不算终态（completed_at 保持 NULL），可被显式恢复；
        终态失败与成功才写入 completed_at。
        """
        run = SQLiteActionStore._load_run_row(
            connection,
            organization_id=organization_id,
            run_id=run_id,
        )
        if ActionRunStatus(run["status"]) not in allowed_statuses:
            raise RunStateConflictError("Run 状态不允许本次转换")
        completed_at = None
        last_error_code = None
        last_error_retryable = False
        if new_status is ActionRunStatus.FAILED:
            last_error_code = error_code
            last_error_retryable = error_retryable
            if not error_retryable:
                completed_at = updated_at
        elif new_status in (
            ActionRunStatus.SUCCEEDED,
            ActionRunStatus.CANCELLED,
        ):
            completed_at = updated_at
        connection.execute(
            """
            UPDATE action_runs
            SET status = ?,
                last_error_code = ?,
                last_error_retryable = ?,
                completed_at = ?,
                updated_at = ?
            WHERE organization_id = ? AND id = ?
            """,
            (
                new_status.value,
                last_error_code,
                int(last_error_retryable),
                completed_at,
                updated_at,
                organization_id,
                run_id,
            ),
        )
        return SQLiteActionStore._load_run_row(
            connection,
            organization_id=organization_id,
            run_id=run_id,
        )

    @staticmethod
    def _load_decision_result(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        approval: Approval,
        decision: ApprovalDecision,
        created: bool,
    ) -> DecisionResult:
        """按已落库决定组装完整 DecisionResult，任何引用缺失都按数据损坏处理。"""
        proposal = SQLiteActionStore._load_proposal_row(
            connection,
            organization_id=organization_id,
            proposal_id=approval.proposal_id,
        )
        run = SQLiteActionStore._load_run_row(
            connection,
            organization_id=organization_id,
            run_id=proposal["run_id"],
        )
        version = SQLiteActionStore._load_version_row(
            connection,
            organization_id=organization_id,
            version_id=decision.decided_version_id,
        )
        return DecisionResult(
            created=created,
            decision=decision,
            approval=SQLiteActionStore._to_approval(
                connection.execute(
                    """
                    SELECT *
                    FROM approvals
                    WHERE organization_id = ? AND id = ?
                    """,
                    (organization_id, approval.approval_id),
                ).fetchone()
            ),
            proposal=SQLiteActionStore._to_proposal(proposal),
            run=SQLiteActionStore._to_run(run),
            decided_version=SQLiteActionStore._to_version(version),
        )

    # —— 创建与查询 ——

    def create_workflow(
        self,
        *,
        organization_id: str,
        conversation_id: str,
        turn_id: str,
        created_by_user_id: str,
        order_id: str,
        action_type: ActionType,
        amount_cents: int,
        currency: str,
        reason_code: str,
        reason_text: str,
        parameters_json: str,
    ) -> WorkflowCreation:
        """原子创建 Run、提案、版本 1 与审批请求并写审计。

        设计 10.1：验证订单归属、金额与成功记录、非终态提案冲突，
        创建四件套与审计，全部在同一 BEGIN IMMEDIATE 事务中完成。
        """
        if amount_cents <= 0:
            raise ActionInvalidAmountError("金额必须为正整数（单位：分）")
        parameters = self._validate_action_inputs(
            action_type=action_type,
            reason_code=reason_code,
            reason_text=reason_text,
            parameters_json=parameters_json,
        )
        normalized_currency = currency.strip().upper()

        run_id = str(uuid4())
        proposal_id = str(uuid4())
        version_id = str(uuid4())
        approval_id = str(uuid4())
        now = self._utc_now()

        try:
            with self._immediate_transaction() as connection:
                order_row = self._load_order_row(
                    connection,
                    organization_id=organization_id,
                    order_id=order_id,
                )
                if normalized_currency != order_row["currency"]:
                    raise ActionCurrencyMismatchError(
                        "币种必须等于订单币种"
                    )
                if action_type is ActionType.REFUND:
                    self._validate_refund_amount(
                        connection,
                        organization_id=organization_id,
                        order_id=order_id,
                        amount_cents=amount_cents,
                        refund_scope=RefundScope(parameters["refund_scope"]),
                    )
                else:
                    self._validate_compensation_amount(
                        connection,
                        organization_id=organization_id,
                        order_id=order_id,
                        amount_cents=amount_cents,
                        reason_code=reason_code,
                    )
                active = connection.execute(
                    """
                    SELECT 1
                    FROM action_proposals
                    WHERE organization_id = ?
                      AND order_id = ?
                      AND action_type = ?
                      AND status IN ('awaiting_approval', 'approved', 'executing')
                    LIMIT 1
                    """,
                    (organization_id, order_id, action_type.value),
                ).fetchone()
                if active is not None:
                    raise ActionActiveProposalExistsError(
                        "同订单同动作类型已存在非终态提案"
                    )

                # Run 先落库，proposal_id 在提案创建后回填（触发器校验引用链）。
                connection.execute(
                    """
                    INSERT INTO action_runs(
                        id, organization_id, conversation_id, turn_id,
                        created_by_user_id, workflow_type, status, thread_id,
                        proposal_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, NULL, ?, ?)
                    """,
                    (
                        run_id,
                        organization_id,
                        conversation_id,
                        turn_id,
                        created_by_user_id,
                        action_type.value,
                        run_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO action_proposals(
                        id, organization_id, run_id, order_id, action_type,
                        status, current_version_id, created_by_user_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'awaiting_approval', NULL, ?, ?, ?)
                    """,
                    (
                        proposal_id,
                        organization_id,
                        run_id,
                        order_id,
                        action_type.value,
                        created_by_user_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO action_proposal_versions(
                        id, organization_id, proposal_id, version_no,
                        amount_cents, currency, reason_code, reason_text,
                        parameters_json, created_by_user_id, created_at
                    ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        organization_id,
                        proposal_id,
                        amount_cents,
                        normalized_currency,
                        reason_code,
                        reason_text,
                        parameters_json,
                        created_by_user_id,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE action_proposals
                    SET current_version_id = ?, updated_at = ?
                    WHERE organization_id = ? AND id = ?
                    """,
                    (version_id, now, organization_id, proposal_id),
                )
                connection.execute(
                    """
                    INSERT INTO approvals(
                        id, organization_id, proposal_id,
                        requested_version_id, requested_by_user_id, status,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        approval_id,
                        organization_id,
                        proposal_id,
                        version_id,
                        created_by_user_id,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE action_runs
                    SET proposal_id = ?, updated_at = ?
                    WHERE organization_id = ? AND id = ?
                    """,
                    (proposal_id, now, organization_id, run_id),
                )

                # 审计：Run 创建、提案版本创建、审批请求。
                self._append_audit(
                    connection,
                    organization_id=organization_id,
                    run_id=run_id,
                    proposal_id=proposal_id,
                    actor_type=AuditActorType.USER,
                    actor_user_id=created_by_user_id,
                    event_type=EVENT_RUN_CREATED,
                    resource_type="action_run",
                    resource_id=run_id,
                    details_json=json.dumps(
                        {
                            "workflow_type": action_type.value,
                            "order_id": order_id,
                        }
                    ),
                    created_at=now,
                )
                self._append_audit(
                    connection,
                    organization_id=organization_id,
                    run_id=run_id,
                    proposal_id=proposal_id,
                    actor_type=AuditActorType.USER,
                    actor_user_id=created_by_user_id,
                    event_type=EVENT_PROPOSAL_VERSION_CREATED,
                    resource_type="action_proposal_version",
                    resource_id=version_id,
                    details_json=json.dumps(
                        {
                            "version_no": 1,
                            "amount_cents": amount_cents,
                            "currency": normalized_currency,
                            "reason_code": reason_code,
                        }
                    ),
                    created_at=now,
                )
                self._append_audit(
                    connection,
                    organization_id=organization_id,
                    run_id=run_id,
                    proposal_id=proposal_id,
                    actor_type=AuditActorType.USER,
                    actor_user_id=created_by_user_id,
                    event_type=EVENT_APPROVAL_REQUESTED,
                    resource_type="approval",
                    resource_id=approval_id,
                    details_json=json.dumps({"proposal_id": proposal_id}),
                    created_at=now,
                )

                run_row = self._load_run_row(
                    connection,
                    organization_id=organization_id,
                    run_id=run_id,
                )
                proposal_row = self._load_proposal_row(
                    connection,
                    organization_id=organization_id,
                    proposal_id=proposal_id,
                )
                version_row = self._load_version_row(
                    connection,
                    organization_id=organization_id,
                    version_id=version_id,
                )
                approval_row = connection.execute(
                    """
                    SELECT *
                    FROM approvals
                    WHERE organization_id = ? AND id = ?
                    """,
                    (organization_id, approval_id),
                ).fetchone()
                if approval_row is None:
                    raise ExecutionDataIntegrityError("审批请求创建后无法读取")
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if (
                _ACTIVE_PROPOSAL_INDEX in message
                or _SAME_TURN_INDEX in message
            ):
                raise ActionActiveProposalExistsError(
                    "同订单同动作类型已存在非终态提案，或同一聊天回合重复提案"
                ) from exc
            raise ActionError("创建动作工作流失败：数据库约束冲突") from exc

        return WorkflowCreation(
            run=self._to_run(run_row),
            proposal=self._to_proposal(proposal_row),
            version=self._to_version(version_row),
            approval=self._to_approval(approval_row),
        )

    def get_run(
        self,
        *,
        organization_id: str,
        run_id: str,
    ) -> ActionRun:
        with self._connection() as connection:
            row = self._load_run_row(
                connection,
                organization_id=organization_id,
                run_id=run_id,
            )
        return self._to_run(row)

    def get_refundable_balance(
        self,
        *,
        organization_id: str,
        order_id: str,
    ) -> int | None:
        """返回订单当前可退余额（分）。

        设计 8.2：执行成功后是否把订单标记为 refunded 以最新可退余额是否
        为零判断，而不是只看 refund_scope；本方法供执行器在执行前计算该
        标记使用。订单不存在或不属于当前企业时返回 None。
        """
        with self._connection() as connection:
            order_row = connection.execute(
                """
                SELECT total_amount_cents
                FROM orders
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, order_id),
            ).fetchone()
            if order_row is None:
                return None
            refunded = self._sum_refunded_cents(
                connection,
                organization_id=organization_id,
                order_id=order_id,
            )
        return int(order_row["total_amount_cents"]) - refunded

    def get_order_number(
        self,
        *,
        organization_id: str,
        order_id: str,
    ) -> str | None:
        """返回订单号，供审批中断载荷掩码展示使用。

        订单不存在或不属于当前企业时返回 None，调用方按引用链损坏处理。
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT order_no
                FROM orders
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, order_id),
            ).fetchone()
        if row is None:
            return None
        return row["order_no"]

    def transition_run(
        self,
        *,
        organization_id: str,
        run_id: str,
        expected_statuses: tuple[ActionRunStatus, ...],
        new_status: ActionRunStatus,
        error_code: str | None,
        error_retryable: bool,
        actor_type: AuditActorType,
        actor_user_id: str | None,
        event_type: str,
        details_json: str,
    ) -> ActionRun:
        """原子校验并转换 Run 状态，同时写入对应审计事件。

        设计 8.4：succeeded / cancelled 以及 failed（不可重试）是终态，
        一旦进入不允许任何转换；failed + 可重试仍可被显式恢复。
        """
        with self._immediate_transaction() as connection:
            current_row = self._load_run_row(
                connection,
                organization_id=organization_id,
                run_id=run_id,
            )
            current = ActionRunStatus(current_row["status"])
            if current in (
                ActionRunStatus.SUCCEEDED,
                ActionRunStatus.CANCELLED,
            ):
                raise RunStateConflictError("终态 Run 不允许转换")
            if (
                current is ActionRunStatus.FAILED
                and not bool(current_row["last_error_retryable"])
            ):
                raise RunStateConflictError("终态 Run 不允许转换")
            if current not in expected_statuses:
                raise RunStateConflictError(
                    f"Run 状态 {current.value} 不在预期集合中"
                )
            if new_status not in _RUN_TRANSITIONS[current]:
                raise RunStateConflictError(
                    f"Run 不允许从 {current.value} 转换到 {new_status.value}"
                )
            if new_status is ActionRunStatus.FAILED:
                if not error_code or not error_code.strip():
                    raise RunStateConflictError("Run 失败必须记录稳定错误码")
            elif error_code is not None or error_retryable:
                raise RunStateConflictError(
                    "非失败状态不允许携带错误码或可重试标记"
                )
            now = self._utc_now()
            updated_row = self._transition_run_row(
                connection,
                organization_id=organization_id,
                run_id=run_id,
                allowed_statuses={current},
                new_status=new_status,
                error_code=error_code,
                error_retryable=error_retryable,
                updated_at=now,
            )
            self._append_audit(
                connection,
                organization_id=organization_id,
                run_id=run_id,
                proposal_id=updated_row["proposal_id"],
                actor_type=actor_type,
                actor_user_id=actor_user_id,
                event_type=event_type,
                resource_type="action_run",
                resource_id=run_id,
                details_json=details_json,
                created_at=now,
            )
        return self._to_run(updated_row)

    def get_proposal(
        self,
        *,
        organization_id: str,
        proposal_id: str,
    ) -> ActionProposal:
        with self._connection() as connection:
            row = self._load_proposal_row(
                connection,
                organization_id=organization_id,
                proposal_id=proposal_id,
            )
        return self._to_proposal(row)

    def get_proposal_by_run(
        self,
        *,
        organization_id: str,
        run_id: str,
    ) -> ActionProposal:
        with self._connection() as connection:
            run_row = self._load_run_row(
                connection,
                organization_id=organization_id,
                run_id=run_id,
            )
            proposal_id = run_row["proposal_id"]
            if proposal_id is None:
                raise ExecutionDataIntegrityError("Run 未关联提案")
            row = connection.execute(
                """
                SELECT *
                FROM action_proposals
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, proposal_id),
            ).fetchone()
            if row is None:
                raise ExecutionDataIntegrityError("Run 关联的提案不存在")
        return self._to_proposal(row)

    def get_approval(
        self,
        *,
        organization_id: str,
        approval_id: str,
    ) -> Approval:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM approvals
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, approval_id),
            ).fetchone()
        if row is None:
            raise ApprovalNotFoundError("审批不存在或不属于当前企业")
        return self._to_approval(row)

    def get_approval_by_proposal(
        self,
        *,
        organization_id: str,
        proposal_id: str,
    ) -> Approval:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM approvals
                WHERE organization_id = ? AND proposal_id = ?
                """,
                (organization_id, proposal_id),
            ).fetchone()
        if row is None:
            raise ApprovalNotFoundError("审批不存在或不属于当前企业")
        return self._to_approval(row)

    def get_decision(
        self,
        *,
        organization_id: str,
        approval_id: str,
    ) -> ApprovalDecision | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM approval_decisions
                WHERE organization_id = ? AND approval_id = ?
                """,
                (organization_id, approval_id),
            ).fetchone()
        if row is None:
            return None
        return self._to_decision(row)

    def get_version(
        self,
        *,
        organization_id: str,
        version_id: str,
    ) -> ActionProposalVersion:
        with self._connection() as connection:
            row = self._load_version_row(
                connection,
                organization_id=organization_id,
                version_id=version_id,
            )
        return self._to_version(row)

    def list_versions(
        self,
        *,
        organization_id: str,
        proposal_id: str,
    ) -> list[ActionProposalVersion]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM action_proposal_versions
                WHERE organization_id = ? AND proposal_id = ?
                ORDER BY version_no
                """,
                (organization_id, proposal_id),
            ).fetchall()
        return [self._to_version(row) for row in rows]

    def count_versions(
        self,
        *,
        organization_id: str,
        proposal_id: str,
    ) -> int:
        """返回提案的版本总数（审批列表「历史版本摘要」用）。"""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM action_proposal_versions
                WHERE organization_id = ? AND proposal_id = ?
                """,
                (organization_id, proposal_id),
            ).fetchone()
        return int(row["total"])

    def list_approvals(
        self,
        *,
        organization_id: str,
        status: ApprovalStatus | None,
        limit: int,
        offset: int,
    ) -> list[Approval]:
        if status is None:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM approvals
                    WHERE organization_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ? OFFSET ?
                    """,
                    (organization_id, limit, offset),
                ).fetchall()
        else:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM approvals
                    WHERE organization_id = ? AND status = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ? OFFSET ?
                    """,
                    (organization_id, status.value, limit, offset),
                ).fetchall()
        return [self._to_approval(row) for row in rows]

    # —— 审批决定 ——

    def decide_approval(
        self,
        *,
        organization_id: str,
        approval_id: str,
        decided_by_user_id: str,
        decision: ApprovalDecisionType,
        comment: str | None,
        new_version: NewProposalVersion | None,
    ) -> DecisionResult:
        """原子记录审批决定，修改后批准在同一事务内创建下一版本。

        设计 10.2 / 15.1：先锁定并读取审批与既有决定；内容相同的重复提交
        返回首次决定（不比较决定人），内容不同返回冲突；修改后批准严格校验
        变更字段并创建新版本；决定落库后更新审批与提案状态并写审计。
        """
        with self._immediate_transaction() as connection:
            approval_row = connection.execute(
                """
                SELECT *
                FROM approvals
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, approval_id),
            ).fetchone()
            if approval_row is None:
                raise ApprovalNotFoundError("审批不存在或不属于当前企业")
            approval = self._to_approval(approval_row)

            # 决定人必须是当前企业成员（角色是否 admin 由应用服务校验，
            # 成员外键是最终防线，这里先行给出稳定错误码）。
            membership = connection.execute(
                """
                SELECT 1
                FROM memberships
                WHERE organization_id = ? AND user_id = ?
                LIMIT 1
                """,
                (organization_id, decided_by_user_id),
            ).fetchone()
            if membership is None:
                raise ApprovalAdminRequiredError(
                    "决定人不是当前企业成员"
                )

            existing_row = connection.execute(
                """
                SELECT *
                FROM approval_decisions
                WHERE organization_id = ? AND approval_id = ?
                """,
                (organization_id, approval_id),
            ).fetchone()
            if existing_row is not None:
                existing = self._to_decision(existing_row)
                if self._is_same_decision(
                    connection,
                    organization_id=organization_id,
                    existing=existing,
                    decision=decision,
                    comment=comment,
                    new_version=new_version,
                ):
                    # 内容相同的重复提交返回首次决定与当前状态。
                    return self._load_decision_result(
                        connection,
                        organization_id=organization_id,
                        approval=approval,
                        decision=existing,
                        created=False,
                    )
                raise ApprovalAlreadyDecidedError(
                    "审批已由其他决定占用"
                )

            if approval.status is not ApprovalStatus.PENDING:
                raise ExecutionDataIntegrityError(
                    "审批状态与决定记录不一致"
                )

            requested_version_row = self._load_version_row(
                connection,
                organization_id=organization_id,
                version_id=approval.requested_version_id,
            )
            if requested_version_row["proposal_id"] != approval.proposal_id:
                raise ExecutionDataIntegrityError(
                    "审批请求版本不属于该提案"
                )

            proposal_row = self._load_proposal_row(
                connection,
                organization_id=organization_id,
                proposal_id=approval.proposal_id,
            )
            now = self._utc_now()
            decided_version_row = self._resolve_decided_version(
                connection,
                organization_id=organization_id,
                approval=approval,
                proposal_row=proposal_row,
                requested_version_row=requested_version_row,
                decision=decision,
                new_version=new_version,
                decided_by_user_id=decided_by_user_id,
                created_at=now,
            )
            run_id = proposal_row["run_id"]

            try:
                decision_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO approval_decisions(
                        id, organization_id, approval_id, decision,
                        decided_version_id, decided_by_user_id, comment,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        organization_id,
                        approval_id,
                        decision.value,
                        decided_version_row["id"],
                        decided_by_user_id,
                        comment,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ApprovalAdminRequiredError(
                    "决定人不是当前企业成员"
                ) from exc

            if decision is ApprovalDecisionType.REJECTED:
                approval_status = ApprovalStatus.REJECTED
                proposal_status = ProposalStatus.REJECTED
            else:
                approval_status = (
                    ApprovalStatus.APPROVED_WITH_CHANGES
                    if decision is ApprovalDecisionType.APPROVED_WITH_CHANGES
                    else ApprovalStatus.APPROVED
                )
                proposal_status = ProposalStatus.APPROVED
            connection.execute(
                """
                UPDATE approvals
                SET status = ?, decided_at = ?
                WHERE organization_id = ? AND id = ?
                """,
                (approval_status.value, now, organization_id, approval_id),
            )
            connection.execute(
                """
                UPDATE action_proposals
                SET status = ?, current_version_id = ?, updated_at = ?
                WHERE organization_id = ? AND id = ?
                """,
                (
                    proposal_status.value,
                    decided_version_row["id"],
                    now,
                    organization_id,
                    approval.proposal_id,
                ),
            )

            self._append_audit(
                connection,
                organization_id=organization_id,
                run_id=run_id,
                proposal_id=approval.proposal_id,
                actor_type=AuditActorType.USER,
                actor_user_id=decided_by_user_id,
                event_type=EVENT_DECISION_RECORDED,
                resource_type="approval",
                resource_id=approval_id,
                details_json=json.dumps(
                    {
                        "decision": decision.value,
                        "decided_version_id": decided_version_row["id"],
                        "proposer_user_id": proposal_row[
                            "created_by_user_id"
                        ],
                        "decider_user_id": decided_by_user_id,
                        "self_approved": (
                            proposal_row["created_by_user_id"]
                            == decided_by_user_id
                        ),
                    }
                ),
                created_at=now,
            )
            if decision is ApprovalDecisionType.APPROVED_WITH_CHANGES:
                self._append_audit(
                    connection,
                    organization_id=organization_id,
                    run_id=run_id,
                    proposal_id=approval.proposal_id,
                    actor_type=AuditActorType.USER,
                    actor_user_id=decided_by_user_id,
                    event_type=EVENT_PROPOSAL_VERSION_CREATED,
                    resource_type="action_proposal_version",
                    resource_id=decided_version_row["id"],
                    details_json=json.dumps(
                        {
                            "version_no": decided_version_row["version_no"],
                            "amount_cents": decided_version_row[
                                "amount_cents"
                            ],
                            "currency": decided_version_row["currency"],
                            "reason_code": decided_version_row["reason_code"],
                        }
                    ),
                    created_at=now,
                )

            decision_row = connection.execute(
                """
                SELECT *
                FROM approval_decisions
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, decision_id),
            ).fetchone()
            if decision_row is None:
                raise ExecutionDataIntegrityError("审批决定落库后无法读取")
            updated_approval = self._to_approval(
                connection.execute(
                    """
                    SELECT *
                    FROM approvals
                    WHERE organization_id = ? AND id = ?
                    """,
                    (organization_id, approval_id),
                ).fetchone()
            )
        with self._connection() as read_connection:
            return self._load_decision_result(
                connection=read_connection,
                organization_id=organization_id,
                approval=updated_approval,
                decision=self._to_decision(decision_row),
                created=True,
            )

    @staticmethod
    def _is_same_decision(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        existing: ApprovalDecision,
        decision: ApprovalDecisionType,
        comment: str | None,
        new_version: NewProposalVersion | None,
    ) -> bool:
        """比较重复提交与既有决定的内容是否完全相同（不含决定人）。"""
        if existing.decision is not decision:
            return False
        if existing.comment != comment:
            return False
        decided_version = SQLiteActionStore._load_version_row(
            connection,
            organization_id=organization_id,
            version_id=existing.decided_version_id,
        )
        if decision is ApprovalDecisionType.APPROVED_WITH_CHANGES:
            if new_version is None:
                return False
            return (
                new_version.amount_cents == decided_version["amount_cents"]
                and new_version.currency.strip().upper()
                == decided_version["currency"]
                and new_version.reason_code == decided_version["reason_code"]
                and new_version.reason_text == decided_version["reason_text"]
                and new_version.parameters_json
                == decided_version["parameters_json"]
            )
        # approved / rejected：决定版本固定为请求版本，且不允许携带变更内容。
        return new_version is None

    @staticmethod
    def _resolve_decided_version(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        approval: Approval,
        proposal_row: sqlite3.Row,
        requested_version_row: sqlite3.Row,
        decision: ApprovalDecisionType,
        new_version: NewProposalVersion | None,
        decided_by_user_id: str,
        created_at: str,
    ) -> sqlite3.Row:
        """确定决定采用的版本；修改后批准时校验并创建下一版本。"""
        if decision is ApprovalDecisionType.APPROVED_WITH_CHANGES:
            if new_version is None:
                raise ApprovalInvalidChangesError(
                    "修改后批准必须提供变更内容"
                )
            if new_version.amount_cents <= 0:
                raise ApprovalInvalidChangesError("金额必须为正整数")
            normalized_currency = new_version.currency.strip().upper()
            if normalized_currency != requested_version_row["currency"]:
                raise ApprovalInvalidChangesError("币种不允许修改")
            parameters = SQLiteActionStore._validate_action_inputs(
                action_type=ActionType(proposal_row["action_type"]),
                reason_code=new_version.reason_code,
                reason_text=new_version.reason_text,
                parameters_json=new_version.parameters_json,
                approval_changes=True,
            )
            # 至少改变一个允许字段，禁止以“修改后批准”原样重复原版本。
            if (
                new_version.amount_cents == requested_version_row["amount_cents"]
                and normalized_currency == requested_version_row["currency"]
                and new_version.reason_code
                == requested_version_row["reason_code"]
                and new_version.reason_text
                == requested_version_row["reason_text"]
                and new_version.parameters_json
                == requested_version_row["parameters_json"]
            ):
                raise ApprovalInvalidChangesError(
                    "修改后批准必须至少改变一个允许字段"
                )
            SQLiteActionStore._validate_approval_version_business_rules(
                connection,
                organization_id=organization_id,
                proposal_row=proposal_row,
                amount_cents=new_version.amount_cents,
                reason_code=new_version.reason_code,
                parameters=parameters,
                invalid_changes=True,
            )
            return SQLiteActionStore._insert_next_version(
                connection,
                organization_id=organization_id,
                proposal_id=approval.proposal_id,
                decided_by_user_id=decided_by_user_id,
                new_version=new_version,
                created_at=created_at,
            )
        if new_version is not None:
            raise ApprovalInvalidChangesError(
                "批准或拒绝不允许附带变更内容"
            )
        if decision is ApprovalDecisionType.APPROVED:
            parameters = SQLiteActionStore._validate_action_inputs(
                action_type=ActionType(proposal_row["action_type"]),
                reason_code=requested_version_row["reason_code"],
                reason_text=requested_version_row["reason_text"],
                parameters_json=requested_version_row["parameters_json"],
                approval_changes=True,
            )
            SQLiteActionStore._validate_approval_version_business_rules(
                connection,
                organization_id=organization_id,
                proposal_row=proposal_row,
                amount_cents=requested_version_row["amount_cents"],
                reason_code=requested_version_row["reason_code"],
                parameters=parameters,
                invalid_changes=False,
            )
        return requested_version_row

    @staticmethod
    def _validate_approval_version_business_rules(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        proposal_row: sqlite3.Row,
        amount_cents: int,
        reason_code: str,
        parameters: dict,
        invalid_changes: bool,
    ) -> None:
        """在审批决定事务内重验金额、重复原因和类型参数一致性。"""
        try:
            if ActionType(proposal_row["action_type"]) is ActionType.REFUND:
                SQLiteActionStore._validate_refund_amount(
                    connection,
                    organization_id=organization_id,
                    order_id=proposal_row["order_id"],
                    amount_cents=amount_cents,
                    refund_scope=RefundScope(parameters["refund_scope"]),
                )
            else:
                SQLiteActionStore._validate_compensation_amount(
                    connection,
                    organization_id=organization_id,
                    order_id=proposal_row["order_id"],
                    amount_cents=amount_cents,
                    reason_code=reason_code,
                )
        except (
            ActionInvalidAmountError,
            ActionRefundBalanceExceededError,
            ActionCompensationDuplicateError,
            ActionCompensationCapExceededError,
        ) as exc:
            if invalid_changes:
                raise ApprovalInvalidChangesError(str(exc)) from exc
            raise

    @staticmethod
    def _insert_next_version(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        proposal_id: str,
        decided_by_user_id: str,
        new_version: NewProposalVersion,
        created_at: str,
    ) -> sqlite3.Row:
        """在事务内为提案创建下一个不可变版本，版本号在事务内计算。"""
        row = connection.execute(
            """
            SELECT COALESCE(MAX(version_no), 0) AS max_no
            FROM action_proposal_versions
            WHERE organization_id = ? AND proposal_id = ?
            """,
            (organization_id, proposal_id),
        ).fetchone()
        next_no = int(row["max_no"]) + 1
        version_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO action_proposal_versions(
                id, organization_id, proposal_id, version_no, amount_cents,
                currency, reason_code, reason_text, parameters_json,
                created_by_user_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                organization_id,
                proposal_id,
                next_no,
                new_version.amount_cents,
                new_version.currency.strip().upper(),
                new_version.reason_code,
                new_version.reason_text,
                new_version.parameters_json,
                decided_by_user_id,
                created_at,
            ),
        )
        return SQLiteActionStore._load_version_row(
            connection,
            organization_id=organization_id,
            version_id=version_id,
        )

    # —— 幂等认领与执行记录 ——

    def claim_execution(
        self,
        *,
        organization_id: str,
        proposal_id: str,
        proposal_version_id: str,
        action_type: ActionType,
        idempotency_key: str,
    ) -> ExecutionClaim:
        """按幂等键认领或读取已有执行记录。

        设计 8.4 / 15.2：同一幂等键只有一个执行记录；成功执行原样返回；
        可重试失败用原幂等键再次认领并递增尝试次数；认领同时把提案推进到
        executing，并写执行认领审计。
        """
        with self._immediate_transaction() as connection:
            existing_row = connection.execute(
                """
                SELECT *
                FROM tool_executions
                WHERE organization_id = ? AND idempotency_key = ?
                """,
                (organization_id, idempotency_key),
            ).fetchone()
            if existing_row is not None:
                execution = self._to_execution(existing_row)
                self._assert_execution_chain(
                    execution,
                    proposal_id=proposal_id,
                    proposal_version_id=proposal_version_id,
                    action_type=action_type,
                )
                if execution.status is ToolExecutionStatus.SUCCEEDED:
                    # 稳定结果读取：直接返回既有成功执行，不重复写业务记录。
                    return ExecutionClaim(execution=execution, acquired=False)
                if execution.status is ToolExecutionStatus.FAILED_TERMINAL:
                    raise RunStateConflictError(
                        "执行已进入终态，不能重新认领"
                    )
                if execution.status is ToolExecutionStatus.FAILED_RETRYABLE:
                    now = self._utc_now()
                    connection.execute(
                        """
                        UPDATE tool_executions
                        SET status = 'claimed',
                            attempt_count = attempt_count + 1,
                            error_code = NULL,
                            error_retryable = 0,
                            completed_at = NULL,
                            updated_at = ?
                        WHERE organization_id = ? AND id = ?
                        """,
                        (now, organization_id, execution.execution_id),
                    )
                    self._transition_proposal(
                        connection,
                        organization_id=organization_id,
                        proposal_id=proposal_id,
                        allowed_statuses={
                            ProposalStatus.APPROVED,
                            ProposalStatus.EXECUTING,
                            ProposalStatus.FAILED,
                        },
                        new_status=ProposalStatus.EXECUTING,
                        updated_at=now,
                    )
                    self._append_execution_claim_audit(
                        connection,
                        organization_id=organization_id,
                        proposal_id=proposal_id,
                        execution_id=execution.execution_id,
                        attempt_count=execution.attempt_count + 1,
                    )
                    return ExecutionClaim(
                        execution=self._to_execution(self._load_execution_row(
                            connection,
                            organization_id=organization_id,
                            execution_id=execution.execution_id,
                        )),
                        acquired=True,
                    )
                # claimed / running：视为上次进程中断后的重续，原样返回。
                return ExecutionClaim(execution=execution, acquired=False)

            proposal_row = self._load_proposal_row(
                connection,
                organization_id=organization_id,
                proposal_id=proposal_id,
            )
            version_row = self._load_version_row(
                connection,
                organization_id=organization_id,
                version_id=proposal_version_id,
            )
            if version_row["proposal_id"] != proposal_id:
                raise ExecutionDataIntegrityError(
                    "执行版本不属于该提案"
                )
            if proposal_row["action_type"] != action_type.value:
                raise ExecutionDataIntegrityError(
                    "执行动作类型与提案不一致"
                )
            execution_id = str(uuid4())
            now = self._utc_now()
            try:
                connection.execute(
                    """
                    INSERT INTO tool_executions(
                        id, organization_id, proposal_id,
                        proposal_version_id, action_type, idempotency_key,
                        status, attempt_count, claimed_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'claimed', 1, ?, ?)
                    """,
                    (
                        execution_id,
                        organization_id,
                        proposal_id,
                        proposal_version_id,
                        action_type.value,
                        idempotency_key,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ExecutionDataIntegrityError(
                    "执行认领违反引用链或唯一约束"
                ) from exc
            self._transition_proposal(
                connection,
                organization_id=organization_id,
                proposal_id=proposal_id,
                allowed_statuses={
                    ProposalStatus.APPROVED,
                    ProposalStatus.EXECUTING,
                    ProposalStatus.FAILED,
                },
                new_status=ProposalStatus.EXECUTING,
                updated_at=now,
            )
            self._append_execution_claim_audit(
                connection,
                organization_id=organization_id,
                proposal_id=proposal_id,
                execution_id=execution_id,
                attempt_count=1,
            )
            execution_row = self._load_execution_row(
                connection,
                organization_id=organization_id,
                execution_id=execution_id,
            )
        return ExecutionClaim(
            execution=self._to_execution(execution_row),
            acquired=True,
        )

    def reclaim_interrupted_execution(
        self,
        *,
        organization_id: str,
        execution_id: str,
    ) -> ExecutionClaim:
        """重新认领上次进程中断留下的执行记录。

        设计非目标不包含多实例调度；本方法只能由已持有进程内
        幂等键互斥锁的执行器调用。每次重新认领都递增尝试次数，
        并沿用原执行记录和幂等键。
        """
        with self._immediate_transaction() as connection:
            row = self._load_execution_row(
                connection,
                organization_id=organization_id,
                execution_id=execution_id,
            )
            execution = self._to_execution(row)
            if execution.status not in (
                ToolExecutionStatus.CLAIMED,
                ToolExecutionStatus.RUNNING,
            ):
                return ExecutionClaim(execution=execution, acquired=False)
            now = self._utc_now()
            attempt_count = execution.attempt_count + 1
            connection.execute(
                """
                UPDATE tool_executions
                SET status = 'claimed',
                    attempt_count = ?,
                    error_code = NULL,
                    error_retryable = 0,
                    completed_at = NULL,
                    updated_at = ?
                WHERE organization_id = ? AND id = ?
                """,
                (
                    attempt_count,
                    now,
                    organization_id,
                    execution_id,
                ),
            )
            self._transition_proposal(
                connection,
                organization_id=organization_id,
                proposal_id=execution.proposal_id,
                allowed_statuses={
                    ProposalStatus.APPROVED,
                    ProposalStatus.EXECUTING,
                },
                new_status=ProposalStatus.EXECUTING,
                updated_at=now,
            )
            self._append_execution_claim_audit(
                connection,
                organization_id=organization_id,
                proposal_id=execution.proposal_id,
                execution_id=execution_id,
                attempt_count=attempt_count,
            )
            reclaimed_row = self._load_execution_row(
                connection,
                organization_id=organization_id,
                execution_id=execution_id,
            )
        return ExecutionClaim(
            execution=self._to_execution(reclaimed_row),
            acquired=True,
        )

    @staticmethod
    def _assert_execution_chain(
        execution: ToolExecution,
        *,
        proposal_id: str,
        proposal_version_id: str,
        action_type: ActionType,
    ) -> None:
        """校验既有执行记录与本次请求的批准链一致，防止幂等键串接。"""
        if (
            execution.proposal_id != proposal_id
            or execution.proposal_version_id != proposal_version_id
            or execution.action_type is not action_type
        ):
            raise ExecutionDataIntegrityError(
                "执行记录与提案批准链不一致"
            )

    def _append_execution_claim_audit(
        self,
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        proposal_id: str,
        execution_id: str,
        attempt_count: int,
    ) -> None:
        """写执行认领审计（系统主体）。"""
        run_row = self._load_run_row(
            connection,
            organization_id=organization_id,
            run_id=self._load_proposal_row(
                connection,
                organization_id=organization_id,
                proposal_id=proposal_id,
            )["run_id"],
        )
        self._append_audit(
            connection,
            organization_id=organization_id,
            run_id=run_row["id"],
            proposal_id=proposal_id,
            actor_type=AuditActorType.SYSTEM,
            actor_user_id=None,
            event_type=EVENT_EXECUTION_CLAIMED,
            resource_type="tool_execution",
            resource_id=execution_id,
            details_json=json.dumps(
                {"proposal_id": proposal_id, "attempt_count": attempt_count}
            ),
            created_at=self._utc_now(),
        )

    @staticmethod
    def _load_execution_row(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        execution_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT *
            FROM tool_executions
            WHERE organization_id = ? AND id = ?
            """,
            (organization_id, execution_id),
        ).fetchone()
        if row is None:
            raise ExecutionDataIntegrityError("执行记录不存在")
        return row

    def get_execution(
        self,
        *,
        organization_id: str,
        execution_id: str,
    ) -> ToolExecution:
        with self._connection() as connection:
            row = self._load_execution_row(
                connection,
                organization_id=organization_id,
                execution_id=execution_id,
            )
        return self._to_execution(row)

    def get_execution_by_version(
        self,
        *,
        organization_id: str,
        proposal_version_id: str,
    ) -> ToolExecution | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM tool_executions
                WHERE organization_id = ? AND proposal_version_id = ?
                """,
                (organization_id, proposal_version_id),
            ).fetchone()
        if row is None:
            return None
        return self._to_execution(row)

    def mark_execution_running(
        self,
        *,
        organization_id: str,
        execution_id: str,
    ) -> ToolExecution:
        with self._immediate_transaction() as connection:
            row = self._load_execution_row(
                connection,
                organization_id=organization_id,
                execution_id=execution_id,
            )
            execution = self._to_execution(row)
            if execution.status in (
                ToolExecutionStatus.SUCCEEDED,
                ToolExecutionStatus.RUNNING,
            ):
                return execution
            if execution.status is not ToolExecutionStatus.CLAIMED:
                raise RunStateConflictError(
                    "失败执行必须先重新认领"
                )
            now = self._utc_now()
            connection.execute(
                """
                UPDATE tool_executions
                SET status = 'running', updated_at = ?
                WHERE organization_id = ? AND id = ?
                """,
                (now, organization_id, execution_id),
            )
        return self.get_execution(
            organization_id=organization_id,
            execution_id=execution_id,
        )

    def record_execution_success(
        self,
        *,
        organization_id: str,
        execution_id: str,
        proposal_id: str,
        proposal_version_id: str,
        order_id: str,
        action_type: ActionType,
        amount_cents: int,
        currency: str,
        reason_code: str,
        business_record_id: str,
        coupon_valid_days: int | None,
        mark_order_refunded: bool,
    ) -> ExecutionSuccess:
        """在同一事务内重验上限、写入业务结果并更新执行/提案/Run 状态。

        设计 10.3：金额上限必须在写业务结果的同一 BEGIN IMMEDIATE 事务中
        重新计算；已成功的执行原样返回既有业务结果（节点重放安全）；
        订单是否标记 refunded 完全以事务内最新可退余额是否为零决定。
        `mark_order_refunded` 仅为兼容 Task 2 既有接口保留，不参与判断。
        """
        if action_type is ActionType.REFUND:
            if coupon_valid_days is not None:
                raise ExecutionDataIntegrityError(
                    "退款执行不允许携带优惠券参数"
                )
        elif coupon_valid_days != COMPENSATION_COUPON_VALID_DAYS:
            raise ExecutionDataIntegrityError(
                "补偿优惠券有效期必须为 30 天"
            )

        with self._immediate_transaction() as connection:
            execution_row = self._load_execution_row(
                connection,
                organization_id=organization_id,
                execution_id=execution_id,
            )
            execution = self._to_execution(execution_row)
            self._assert_execution_chain(
                execution,
                proposal_id=proposal_id,
                proposal_version_id=proposal_version_id,
                action_type=action_type,
            )
            if execution.status is ToolExecutionStatus.SUCCEEDED:
                # 重放读取：返回既有业务结果，不再次写业务记录。
                record_id = self._find_business_record_id(
                    connection,
                    organization_id=organization_id,
                    execution_id=execution_id,
                    action_type=action_type,
                )
                try:
                    result = json.loads(execution.result_json or "{}")
                except json.JSONDecodeError as exc:
                    raise ExecutionDataIntegrityError(
                        "执行结果 JSON 损坏"
                    ) from exc
                return ExecutionSuccess(
                    execution=execution,
                    business_record_id=record_id,
                    order_marked_refunded=bool(
                        result.get("order_marked_refunded", False)
                    ),
                )
            if execution.status is ToolExecutionStatus.FAILED_TERMINAL:
                raise RunStateConflictError("执行已进入终态，不能记录成功")
            if execution.status is not ToolExecutionStatus.RUNNING:
                raise RunStateConflictError(
                    "只有 running 执行可以记录成功结果"
                )

            proposal_row = self._load_proposal_row(
                connection,
                organization_id=organization_id,
                proposal_id=proposal_id,
            )
            version_row = self._load_version_row(
                connection,
                organization_id=organization_id,
                version_id=proposal_version_id,
            )
            if version_row["proposal_id"] != proposal_id:
                raise ExecutionDataIntegrityError("执行版本不属于该提案")
            if proposal_row["order_id"] != order_id:
                raise ExecutionDataIntegrityError(
                    "执行订单与提案订单不一致"
                )
            if (
                version_row["amount_cents"] != amount_cents
                or version_row["currency"] != currency
                or version_row["reason_code"] != reason_code
            ):
                raise ExecutionDataIntegrityError(
                    "执行参数与批准版本不一致"
                )
            order_row = self._load_order_row(
                connection,
                organization_id=organization_id,
                order_id=order_id,
            )
            if currency != order_row["currency"]:
                raise ExecutionDataIntegrityError(
                    "执行币种与订单币种不一致"
                )

            expected_mark_refunded = False
            if action_type is ActionType.REFUND:
                refunded = self._sum_refunded_cents(
                    connection,
                    organization_id=organization_id,
                    order_id=order_id,
                )
                remaining = order_row["total_amount_cents"] - refunded
                if amount_cents > remaining:
                    raise ActionRefundBalanceExceededError(
                        "退款金额超过订单当前可退余额"
                    )
                expected_mark_refunded = amount_cents == remaining
            else:
                duplicate = connection.execute(
                    """
                    SELECT 1
                    FROM compensation_records
                    WHERE organization_id = ?
                      AND order_id = ?
                      AND reason_code = ?
                    LIMIT 1
                    """,
                    (organization_id, order_id, reason_code),
                ).fetchone()
                if duplicate is not None:
                    raise ActionCompensationDuplicateError(
                        "同一订单同一补偿原因只能成功补偿一次"
                    )
                compensated = self._sum_compensated_cents(
                    connection,
                    organization_id=organization_id,
                    order_id=order_id,
                )
                cap_cents = order_row["total_amount_cents"] // 2
                if compensated + amount_cents > cap_cents:
                    raise ActionCompensationCapExceededError(
                        "补偿累计金额超过订单总金额的 50%"
                    )
            now = self._utc_now()
            try:
                if action_type is ActionType.REFUND:
                    connection.execute(
                        """
                        INSERT INTO refund_records(
                            id, organization_id, order_id, proposal_id,
                            proposal_version_id, tool_execution_id,
                            amount_cents, currency, reason_code, status,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'simulated_succeeded', ?)
                        """,
                        (
                            business_record_id,
                            organization_id,
                            order_id,
                            proposal_id,
                            proposal_version_id,
                            execution_id,
                            amount_cents,
                            currency,
                            reason_code,
                            now,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO compensation_records(
                            id, organization_id, order_id, proposal_id,
                            proposal_version_id, tool_execution_id,
                            amount_cents, currency, reason_code,
                            coupon_valid_days, status, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 30, 'simulated_succeeded', ?)
                        """,
                        (
                            business_record_id,
                            organization_id,
                            order_id,
                            proposal_id,
                            proposal_version_id,
                            execution_id,
                            amount_cents,
                            currency,
                            reason_code,
                            now,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise ExecutionDataIntegrityError(
                    "业务结果写入违反引用链或唯一约束"
                ) from exc

            connection.execute(
                """
                UPDATE tool_executions
                SET status = 'succeeded',
                    result_json = ?,
                    error_code = NULL,
                    error_retryable = 0,
                    completed_at = ?,
                    updated_at = ?
                WHERE organization_id = ? AND id = ?
                """,
                (
                    json.dumps(
                        {
                            "business_record_id": business_record_id,
                            "order_marked_refunded": expected_mark_refunded,
                        }
                    ),
                    now,
                    now,
                    organization_id,
                    execution_id,
                ),
            )
            self._transition_proposal(
                connection,
                organization_id=organization_id,
                proposal_id=proposal_id,
                allowed_statuses={
                    ProposalStatus.APPROVED,
                    ProposalStatus.EXECUTING,
                },
                new_status=ProposalStatus.SUCCEEDED,
                updated_at=now,
            )
            run_row = self._transition_run_row(
                connection,
                organization_id=organization_id,
                run_id=proposal_row["run_id"],
                allowed_statuses={
                    ActionRunStatus.RUNNING,
                },
                new_status=ActionRunStatus.SUCCEEDED,
                error_code=None,
                error_retryable=False,
                updated_at=now,
            )
            if expected_mark_refunded:
                connection.execute(
                    """
                    UPDATE orders
                    SET status = 'refunded', updated_at = ?
                    WHERE organization_id = ? AND id = ?
                    """,
                    (now, organization_id, order_id),
                )

            self._append_audit(
                connection,
                organization_id=organization_id,
                run_id=run_row["id"],
                proposal_id=proposal_id,
                actor_type=AuditActorType.SYSTEM,
                actor_user_id=None,
                event_type=EVENT_EXECUTION_SUCCEEDED,
                resource_type="tool_execution",
                resource_id=execution_id,
                details_json=json.dumps(
                    {
                        "business_record_id": business_record_id,
                        "amount_cents": amount_cents,
                        "currency": currency,
                    }
                ),
                created_at=now,
            )
            self._append_audit(
                connection,
                organization_id=organization_id,
                run_id=run_row["id"],
                proposal_id=proposal_id,
                actor_type=AuditActorType.SYSTEM,
                actor_user_id=None,
                event_type=EVENT_RUN_SUCCEEDED,
                resource_type="action_run",
                resource_id=run_row["id"],
                details_json=json.dumps({}),
                created_at=now,
            )
            updated_execution_row = self._load_execution_row(
                connection,
                organization_id=organization_id,
                execution_id=execution_id,
            )
        return ExecutionSuccess(
            execution=self._to_execution(updated_execution_row),
            business_record_id=business_record_id,
            order_marked_refunded=expected_mark_refunded,
        )

    @staticmethod
    def _find_business_record_id(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        execution_id: str,
        action_type: ActionType,
    ) -> str:
        """按执行记录定位既有业务结果标识。"""
        table = (
            "refund_records"
            if action_type is ActionType.REFUND
            else "compensation_records"
        )
        row = connection.execute(
            f"""
            SELECT id
            FROM {table}
            WHERE organization_id = ? AND tool_execution_id = ?
            """,
            (organization_id, execution_id),
        ).fetchone()
        if row is None:
            raise ExecutionDataIntegrityError(
                "已成功执行缺少对应业务结果"
            )
        return row["id"]

    def record_execution_failure(
        self,
        *,
        organization_id: str,
        execution_id: str,
        error_code: str,
        retryable: bool,
    ) -> ToolExecution:
        """把执行标记为可重试或终态失败，并更新关联 Run 状态。

        设计 8.4：succeeded 与 failed_terminal 是终态，绝不覆盖；
        可重试失败保留原幂等键，显式恢复后可用同一键再次认领。
        """
        with self._immediate_transaction() as connection:
            row = self._load_execution_row(
                connection,
                organization_id=organization_id,
                execution_id=execution_id,
            )
            execution = self._to_execution(row)
            if execution.status in (
                ToolExecutionStatus.SUCCEEDED,
                ToolExecutionStatus.FAILED_TERMINAL,
            ):
                return execution
            if execution.status is not ToolExecutionStatus.RUNNING:
                raise RunStateConflictError(
                    "只有 running 执行可以记录失败结果"
                )
            now = self._utc_now()
            new_status = (
                ToolExecutionStatus.FAILED_RETRYABLE
                if retryable
                else ToolExecutionStatus.FAILED_TERMINAL
            )
            connection.execute(
                """
                UPDATE tool_executions
                SET status = ?,
                    error_code = ?,
                    error_retryable = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE organization_id = ? AND id = ?
                """,
                (
                    new_status.value,
                    error_code,
                    int(retryable),
                    None if retryable else now,
                    now,
                    organization_id,
                    execution_id,
                ),
            )
            proposal_row = self._load_proposal_row(
                connection,
                organization_id=organization_id,
                proposal_id=execution.proposal_id,
            )
            self._transition_proposal(
                connection,
                organization_id=organization_id,
                proposal_id=execution.proposal_id,
                allowed_statuses={
                    ProposalStatus.APPROVED,
                    ProposalStatus.EXECUTING,
                    ProposalStatus.FAILED,
                },
                new_status=ProposalStatus.FAILED,
                updated_at=now,
            )
            run_row = self._transition_run_row(
                connection,
                organization_id=organization_id,
                run_id=proposal_row["run_id"],
                allowed_statuses={
                    ActionRunStatus.QUEUED,
                    ActionRunStatus.RUNNING,
                    ActionRunStatus.AWAITING_APPROVAL,
                    ActionRunStatus.FAILED,
                },
                new_status=ActionRunStatus.FAILED,
                error_code=error_code,
                error_retryable=retryable,
                updated_at=now,
            )
            self._append_audit(
                connection,
                organization_id=organization_id,
                run_id=run_row["id"],
                proposal_id=execution.proposal_id,
                actor_type=AuditActorType.SYSTEM,
                actor_user_id=None,
                event_type=EVENT_EXECUTION_FAILED,
                resource_type="tool_execution",
                resource_id=execution_id,
                details_json=json.dumps(
                    {"error_code": error_code, "retryable": retryable}
                ),
                created_at=now,
            )
            self._append_audit(
                connection,
                organization_id=organization_id,
                run_id=run_row["id"],
                proposal_id=execution.proposal_id,
                actor_type=AuditActorType.SYSTEM,
                actor_user_id=None,
                event_type=EVENT_RUN_FAILED,
                resource_type="action_run",
                resource_id=run_row["id"],
                details_json=json.dumps(
                    {"error_code": error_code, "retryable": retryable}
                ),
                created_at=now,
            )
        return self.get_execution(
            organization_id=organization_id,
            execution_id=execution_id,
        )

    def record_workflow_failure(
        self,
        *,
        organization_id: str,
        run_id: str,
        error_code: str,
        retryable: bool,
    ) -> ActionRun:
        """在执行认领前失败时原子更新 Proposal、Run 与失败审计。

        批准链损坏等错误可能发生在创建 ``tool_executions`` 之前，此时不能
        调用 ``record_execution_failure``。本方法保证图进入失败终点前，
        业务数据库中已经形成可查询的稳定失败事实。
        """
        with self._immediate_transaction() as connection:
            run_row = self._load_run_row(
                connection,
                organization_id=organization_id,
                run_id=run_id,
            )
            run = self._to_run(run_row)
            if run.status is ActionRunStatus.FAILED:
                return run
            if run.status is not ActionRunStatus.RUNNING:
                raise RunStateConflictError(
                    "只有 running 状态的 Run 可以记录工作流失败"
                )
            if run.proposal_id is None:
                raise ExecutionDataIntegrityError(
                    "工作流失败时 Run 缺少提案引用"
                )
            proposal_row = self._load_proposal_row(
                connection,
                organization_id=organization_id,
                proposal_id=run.proposal_id,
            )
            if proposal_row["run_id"] != run_id:
                raise ExecutionDataIntegrityError(
                    "工作流失败时 Run 与提案引用链不一致"
                )
            now = self._utc_now()
            self._transition_proposal(
                connection,
                organization_id=organization_id,
                proposal_id=run.proposal_id,
                allowed_statuses={
                    ProposalStatus.APPROVED,
                    ProposalStatus.EXECUTING,
                    ProposalStatus.FAILED,
                },
                new_status=ProposalStatus.FAILED,
                updated_at=now,
            )
            failed_run_row = self._transition_run_row(
                connection,
                organization_id=organization_id,
                run_id=run_id,
                allowed_statuses={ActionRunStatus.RUNNING},
                new_status=ActionRunStatus.FAILED,
                error_code=error_code,
                error_retryable=retryable,
                updated_at=now,
            )
            self._append_audit(
                connection,
                organization_id=organization_id,
                run_id=run_id,
                proposal_id=run.proposal_id,
                actor_type=AuditActorType.SYSTEM,
                actor_user_id=None,
                event_type=EVENT_RUN_FAILED,
                resource_type="action_run",
                resource_id=run_id,
                details_json=json.dumps(
                    {"error_code": error_code, "retryable": retryable}
                ),
                created_at=now,
            )
        return self._to_run(failed_run_row)

    # —— 审计 ——

    def list_audit_logs(
        self,
        *,
        organization_id: str,
        run_id: str,
    ) -> list[AuditLog]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM audit_logs
                WHERE organization_id = ? AND run_id = ?
                ORDER BY rowid
                """,
                (organization_id, run_id),
            ).fetchall()
        return [self._to_audit_log(row) for row in rows]

    def append_audit_log(
        self,
        *,
        organization_id: str,
        run_id: str,
        proposal_id: str | None,
        actor_type: AuditActorType,
        actor_user_id: str | None,
        event_type: str,
        resource_type: str,
        resource_id: str,
        details_json: str,
    ) -> AuditLog:
        """追加不伴随状态变化的审计事件，例如管理员显式恢复请求。"""
        with self._immediate_transaction() as connection:
            self._load_run_row(
                connection,
                organization_id=organization_id,
                run_id=run_id,
            )
            if proposal_id is not None:
                self._load_proposal_row(
                    connection,
                    organization_id=organization_id,
                    proposal_id=proposal_id,
                )
            now = self._utc_now()
            self._append_audit(
                connection,
                organization_id=organization_id,
                run_id=run_id,
                proposal_id=proposal_id,
                actor_type=actor_type,
                actor_user_id=actor_user_id,
                event_type=event_type,
                resource_type=resource_type,
                resource_id=resource_id,
                details_json=details_json,
                created_at=now,
            )
            row = connection.execute(
                """
                SELECT *
                FROM audit_logs
                WHERE organization_id = ? AND run_id = ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (organization_id, run_id),
            ).fetchone()
        if row is None:
            raise ExecutionDataIntegrityError("审计事件追加后无法读取")
        return self._to_audit_log(row)
