"""退款/补偿提案与审批应用服务。

本模块实现设计文档 Task 3：创建提案、审批列表与详情、审批决定、
Run 状态查询与显式恢复，以及领域错误到 HTTP 状态码的映射。

职责边界：

- RBAC 依据当前 Membership 重新读取，不允许使用 checkpoint 或历史角色；
- 「修改后批准」的允许修改字段在此层校验，不可修改字段在结构上不暴露；
- 业务事务提交后由注入的 ``ActionWorkflowRunner`` 推进确定性工作流
  （Task 5 由 LangGraph 实现）；运行器不可用或失败时，创建与决定结果
  仍然持久化，通过结果标记告知调用方需要稍后显式恢复。
"""

import json
from dataclasses import dataclass, replace
from typing import Protocol

from app.actions.base import (
    ActionError,
    ActionProposal,
    ActionProposalVersion,
    ActionRun,
    ActionRunStatus,
    ActionStore,
    ActionType,
    Approval,
    ApprovalAdminRequiredError,
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalInvalidChangesError,
    ApprovalNotFoundError,
    ApprovalStatus,
    AuditActorType,
    CheckpointUnavailableError,
    DecisionResult,
    ExecutionDataIntegrityError,
    NewProposalVersion,
    RefundScope,
    RunNotFoundError,
    RunNotResumableError,
    ToolExecution,
    WorkflowCreation,
)
from app.actions.sqlite_store import COMPENSATION_COUPON_VALID_DAYS
from app.application.organization_service import (
    OrganizationAccessDeniedError,
    OrganizationService,
)
from app.organizations.base import MembershipRole

# —— 应用服务追加的审计事件类型 ——
# 设计 9.9 要求审计记录管理员显式恢复请求。
EVENT_RUN_RESUME_REQUESTED = "run_resume_requested"  # 管理员显式恢复请求

# —— 错误映射（设计 13.4 建议状态码） ——

# 稳定错误码到建议 HTTP 状态码的映射表，供 Task 6 路由统一使用。
HTTP_STATUS_BY_ERROR_CODE: dict[str, int] = {
    "ACTION_ERROR": 422,
    "ACTION_ORDER_NOT_FOUND": 404,
    "ACTION_INVALID_AMOUNT": 422,
    "ACTION_CURRENCY_MISMATCH": 422,
    "ACTION_ACTIVE_PROPOSAL_EXISTS": 409,
    "ACTION_REFUND_BALANCE_EXCEEDED": 409,
    "ACTION_COMPENSATION_DUPLICATE": 409,
    "ACTION_COMPENSATION_CAP_EXCEEDED": 409,
    "APPROVAL_NOT_FOUND": 404,
    "APPROVAL_ADMIN_REQUIRED": 403,
    "APPROVAL_ALREADY_DECIDED": 409,
    "APPROVAL_INVALID_CHANGES": 422,
    "PROPOSAL_NOT_FOUND": 404,
    "RUN_NOT_FOUND": 404,
    "RUN_NOT_RESUMABLE": 409,
    "RUN_STATE_CONFLICT": 409,
    "EXECUTION_NOT_APPROVED": 409,
    "EXECUTION_RETRYABLE_FAILURE": 503,
    "EXECUTION_DATA_INTEGRITY_ERROR": 500,
    "CHECKPOINT_UNAVAILABLE": 503,
}
DEFAULT_HTTP_STATUS = 500  # 未知错误码的兜底状态码，防止静默吞掉未知异常


def http_status_for_action_error(error: ActionError) -> int:
    """把动作领域错误映射为建议 HTTP 状态码，供 Task 6 审批与恢复路由统一使用。"""
    return HTTP_STATUS_BY_ERROR_CODE.get(error.code, DEFAULT_HTTP_STATUS)


class ActionWorkflowRunner(Protocol):
    """确定性动作工作流运行器。

    Task 5 由 LangGraph 实现：只负责按 Run 推进确定性状态机，
    不调用 LLM、不选择工具、不判断调用者权限。失败必须抛出携带
    稳定错误码的 ``ActionError``。

    所有方法必须显式接收 ``organization_id``：运行器需要按租户读取
    Run（含 checkpoint 的 ``thread_id``）与持久化决定，不允许使用
    不带租户条件的查询。
    """

    def start(self, *, organization_id: str, run_id: str) -> None:
        """首次启动：从当前 Run 位置运行图直到 interrupt 或终态。"""
        raise NotImplementedError

    def resume(self, *, organization_id: str, run_id: str) -> None:
        """恢复：从既有 checkpoint 继续运行图（含首次启动失败后的重试）。"""
        raise NotImplementedError


@dataclass(frozen=True)
class ApprovalChanges:
    """「修改后批准」允许提交的修改字段，未提供的字段沿用请求版本。"""

    amount_cents: int | None = None  # 修改后的金额（分），None 表示沿用请求版本金额
    reason_code: str | None = None  # 修改后的原因码，None 表示沿用请求版本原因码
    reason_text: str | None = None  # 修改后的补充说明，None 表示沿用请求版本说明
    refund_scope: RefundScope | None = None  # 退款专属参数；补偿动作不允许修改，必须为 None


@dataclass(frozen=True)
class ProposalCreationOutcome:
    """创建提案的完整业务结果，含首次工作流启动状态。"""

    creation: WorkflowCreation  # 新建的 Run、提案、初始版本与审批请求
    start_ok: bool  # 首次图调用是否成功；False 表示需稍后显式恢复
    start_error_code: str | None  # 首次图调用失败时的稳定错误码，成功时为 None


@dataclass(frozen=True)
class DecisionOutcome:
    """审批决定的完整结果，含工作流恢复状态与自审标记。"""

    result: DecisionResult  # 落库决定及更新后的审批、提案与 Run
    resume_required: bool  # 决定已保存但工作流尚未恢复完成，需要显式恢复
    resume_error_code: str | None  # 自动恢复失败时的稳定错误码，成功或无需恢复时为 None
    self_approved: bool  # 提案人与审批人是否为同一用户（MVP 允许自审，必须明确记录）


@dataclass(frozen=True)
class ResumeOutcome:
    """显式恢复 Run 的结果。"""

    run: ActionRun  # 恢复请求完成后重新读取的 Run 最新状态
    resume_ok: bool  # 是否成功调用工作流恢复
    error_code: str | None  # 恢复失败时的稳定错误码，成功时为 None
    result: dict | None  # 恢复后的稳定业务结果，尚未执行成功时为 None


@dataclass(frozen=True)
class ApprovalListItem:
    """审批列表项：审批请求、提案、Run 状态与决定摘要。"""

    approval: Approval  # 审批请求
    proposal: ActionProposal  # 关联提案
    run_status: ActionRunStatus  # 关联 Run 的当前状态
    current_version: ActionProposalVersion | None  # 当前生效版本，尚未产生时为空
    version_count: int  # 提案版本总数，供「历史版本摘要」展示
    decision: ApprovalDecision | None  # 已落库决定，未决定时为 None


@dataclass(frozen=True)
class ApprovalDetail:
    """审批详情：请求版本、全部历史版本、Run 状态与决定摘要。"""

    approval: Approval  # 审批请求
    proposal: ActionProposal  # 关联提案
    run: ActionRun  # 关联动作 Run
    requested_version: ActionProposalVersion  # 请求审批的版本
    current_version: ActionProposalVersion | None  # 当前生效版本，尚未产生时为空
    versions: list[ActionProposalVersion]  # 全部历史版本（只增不改，按版本号升序）
    decision: ApprovalDecision | None  # 已落库决定，未决定时为 None
    self_approved: bool  # 提案人与审批人是否相同，决定已作出时有效


@dataclass(frozen=True)
class RunStatusView:
    """Action Run 状态查询结果，供只读状态工具与查询 API 展示。"""

    run: ActionRun  # 动作 Run
    proposal: ActionProposal | None  # 关联提案，Run 未关联提案时为空
    approval: Approval | None  # 关联审批请求
    decision: ApprovalDecision | None  # 已落库决定，未决定时为 None
    current_version: ActionProposalVersion | None  # 当前生效版本，尚未产生时为空
    execution: ToolExecution | None  # 决定版本的执行记录，尚未执行时为 None
    result: dict | None  # 解析后的执行结果（business_record_id / order_marked_refunded）


class ActionWorkflowService:
    """提案与审批应用服务。

    负责创建提案、审批列表与详情、审批决定、Run 状态查询与显式恢复。
    审批与恢复的 RBAC 基于当前 Membership 重新读取；工作流推进通过注入的
    ``ActionWorkflowRunner`` 完成，运行器不可用时业务事实仍然持久化，
    由调用方根据结果标记决定是否提示显式恢复。
    """

    def __init__(
        self,
        *,
        store: ActionStore,
        organization_service: OrganizationService,
        runner: ActionWorkflowRunner | None = None,
    ):
        self._store = store
        self._organization_service = organization_service
        self._runner = runner

    # —— 创建提案 ——

    def create_proposal(
        self,
        *,
        organization_id: str,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        order_id: str,
        action_type: ActionType,
        amount_cents: int,
        currency: str,
        reason_code: str,
        reason_text: str,
        refund_scope: RefundScope | None = None,
        coupon_valid_days: int | None = None,
    ) -> ProposalCreationOutcome:
        """为当前企业、当前订单创建提案，并在业务事务提交后首次启动工作流。

        设计 10.1：会话与回合标识由 ChatService 验证后传入，不允许为空；
        类型专属参数在此组装为严格 JSON；业务事务提交后调用运行器首次启动，
        首次启动失败不丢失 Run、提案、版本与审批事实，可稍后显式恢复。
        """
        if not conversation_id.strip() or not turn_id.strip():
            raise ActionError("会话与回合标识不能为空")
        if action_type is ActionType.REFUND:
            if refund_scope is None:
                raise ActionError("退款提案必须提供 refund_scope")
            parameters_json = json.dumps({"refund_scope": refund_scope.value})
        else:
            if coupon_valid_days != COMPENSATION_COUPON_VALID_DAYS:
                raise ActionError("补偿优惠券有效期必须为 30 天")
            parameters_json = json.dumps(
                {"coupon_valid_days": coupon_valid_days}
            )
        creation = self._store.create_workflow(
            organization_id=organization_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            created_by_user_id=user_id,
            order_id=order_id,
            action_type=action_type,
            amount_cents=amount_cents,
            currency=currency,
            reason_code=reason_code,
            reason_text=reason_text,
            parameters_json=parameters_json,
        )
        start_ok, start_error_code = self._try_start(
            organization_id,
            creation.run.run_id,
        )
        latest_run = self._store.get_run(
            organization_id=organization_id,
            run_id=creation.run.run_id,
        )
        creation = replace(creation, run=latest_run)
        return ProposalCreationOutcome(
            creation=creation,
            start_ok=start_ok,
            start_error_code=start_error_code,
        )

    # —— 审批列表与详情 ——

    def list_approvals(
        self,
        *,
        organization_id: str,
        status: ApprovalStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[ApprovalListItem]:
        """分页查询审批列表，默认按创建时间倒序，附 Run 状态与决定摘要。"""
        if limit < 1 or limit > 200:
            raise ValueError("limit 必须在 1-200 之间")
        if offset < 0:
            raise ValueError("offset 不能为负")
        approvals = self._store.list_approvals(
            organization_id=organization_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        return [
            self._build_list_item(
                organization_id=organization_id,
                approval=item,
            )
            for item in approvals
        ]

    def _build_list_item(
        self,
        *,
        organization_id: str,
        approval: Approval,
    ) -> ApprovalListItem:
        """把审批请求组装为包含提案、Run 状态与决定摘要的列表项。"""
        proposal = self._store.get_proposal(
            organization_id=organization_id,
            proposal_id=approval.proposal_id,
        )
        run = self._store.get_run(
            organization_id=organization_id,
            run_id=proposal.run_id,
        )
        current_version = None
        if proposal.current_version_id is not None:
            current_version = self._store.get_version(
                organization_id=organization_id,
                version_id=proposal.current_version_id,
            )
        decision = self._store.get_decision(
            organization_id=organization_id,
            approval_id=approval.approval_id,
        )
        version_count = self._store.count_versions(
            organization_id=organization_id,
            proposal_id=proposal.proposal_id,
        )
        return ApprovalListItem(
            approval=approval,
            proposal=proposal,
            run_status=run.status,
            current_version=current_version,
            version_count=version_count,
            decision=decision,
        )

    def get_approval_detail(
        self,
        *,
        organization_id: str,
        approval_id: str,
    ) -> ApprovalDetail:
        """查询审批详情：请求版本、全部历史版本、Run 状态与决定摘要。"""
        approval = self._store.get_approval(
            organization_id=organization_id,
            approval_id=approval_id,
        )
        proposal = self._store.get_proposal(
            organization_id=organization_id,
            proposal_id=approval.proposal_id,
        )
        run = self._store.get_run(
            organization_id=organization_id,
            run_id=proposal.run_id,
        )
        requested_version = self._store.get_version(
            organization_id=organization_id,
            version_id=approval.requested_version_id,
        )
        versions = self._store.list_versions(
            organization_id=organization_id,
            proposal_id=proposal.proposal_id,
        )
        current_version = None
        if proposal.current_version_id is not None:
            current_version = self._store.get_version(
                organization_id=organization_id,
                version_id=proposal.current_version_id,
            )
        decision = self._store.get_decision(
            organization_id=organization_id,
            approval_id=approval.approval_id,
        )
        self_approved = False
        if decision is not None:
            self_approved = (
                proposal.created_by_user_id == decision.decided_by_user_id
            )
        return ApprovalDetail(
            approval=approval,
            proposal=proposal,
            run=run,
            requested_version=requested_version,
            current_version=current_version,
            versions=versions,
            decision=decision,
            self_approved=self_approved,
        )

    # —— 审批决定 ——

    def decide_approval(
        self,
        *,
        organization_id: str,
        approval_id: str,
        decided_by_user_id: str,
        decision: ApprovalDecisionType,
        comment: str | None = None,
        changes: ApprovalChanges | None = None,
    ) -> DecisionOutcome:
        """作出审批决定：RBAC、修改字段校验、落库决定并自动恢复工作流。

        设计 10.2 / 14：决定前重新读取当前 Membership 并校验 admin 角色；
        批准与拒绝不允许携带修改内容，修改后批准只允许修改允许字段；
        决定落库后尝试自动恢复 Run，恢复失败不丢失决定，可稍后显式恢复；
        相同决定重复提交由 Store 返回首次结果。
        """
        self._require_admin(
            organization_id=organization_id,
            user_id=decided_by_user_id,
            not_found_error=ApprovalNotFoundError,
        )
        new_version = self._build_new_version(
            organization_id=organization_id,
            approval_id=approval_id,
            decision=decision,
            changes=changes,
        )
        result = self._store.decide_approval(
            organization_id=organization_id,
            approval_id=approval_id,
            decided_by_user_id=decided_by_user_id,
            decision=decision,
            comment=comment,
            new_version=new_version,
        )
        run = result.run
        is_terminal = (
            run.status in (
                ActionRunStatus.SUCCEEDED,
                ActionRunStatus.CANCELLED,
            )
            or (
                run.status is ActionRunStatus.FAILED
                and not run.last_error_retryable
            )
        )
        if is_terminal:
            # Run 已进入终态（例如重复提交相同决定），无需再恢复。
            latest_run = self._store.get_run(
                organization_id=organization_id,
                run_id=run.run_id,
            )
            result = replace(result, run=latest_run)
            return DecisionOutcome(
                result=result,
                resume_required=False,
                resume_error_code=None,
                self_approved=self._is_self_approved(result),
            )
        resume_ok, resume_error_code = self._try_resume(
            organization_id,
            run.run_id,
        )
        latest_run = self._store.get_run(
            organization_id=organization_id,
            run_id=run.run_id,
        )
        result = replace(result, run=latest_run)
        return DecisionOutcome(
            result=result,
            resume_required=not resume_ok,
            resume_error_code=resume_error_code,
            self_approved=self._is_self_approved(result),
        )

    @staticmethod
    def _is_self_approved(
        result: DecisionResult,
    ) -> bool:
        """根据已持久化的实际决定人判断是否为提案人自审。"""
        return (
            result.proposal.created_by_user_id
            == result.decision.decided_by_user_id
        )

    def _build_new_version(
        self,
        *,
        organization_id: str,
        approval_id: str,
        decision: ApprovalDecisionType,
        changes: ApprovalChanges | None,
    ) -> NewProposalVersion | None:
        """按决定类型构造版本变更；批准/拒绝必须无变更，修改后批准校验允许字段。

        设计 8.2 / 13.2：币种、订单、动作类型、组织与补偿券有效期不允许修改，
        未在 ``ApprovalChanges`` 暴露的字段在结构上不可修改；修改后的参数仍由
        Store 在决定事务内执行完整领域校验。
        """
        if decision is not ApprovalDecisionType.APPROVED_WITH_CHANGES:
            if changes is not None:
                raise ApprovalInvalidChangesError(
                    "批准或拒绝不允许附带修改内容"
                )
            return None
        if changes is None:
            raise ApprovalInvalidChangesError("修改后批准必须提供修改内容")
        approval = self._store.get_approval(
            organization_id=organization_id,
            approval_id=approval_id,
        )
        proposal = self._store.get_proposal(
            organization_id=organization_id,
            proposal_id=approval.proposal_id,
        )
        requested = self._store.get_version(
            organization_id=organization_id,
            version_id=approval.requested_version_id,
        )
        if (
            proposal.action_type is ActionType.COMPENSATION
            and changes.refund_scope is not None
        ):
            raise ApprovalInvalidChangesError("补偿动作不允许修改退款范围")
        if changes.amount_cents is not None and changes.amount_cents <= 0:
            raise ApprovalInvalidChangesError("金额必须为正整数（单位：分）")
        if proposal.action_type is ActionType.REFUND:
            if changes.refund_scope is not None:
                scope = changes.refund_scope
            else:
                requested_parameters = json.loads(requested.parameters_json)
                scope = RefundScope(requested_parameters["refund_scope"])
            parameters_json = json.dumps({"refund_scope": scope.value})
        else:
            parameters_json = json.dumps(
                {"coupon_valid_days": COMPENSATION_COUPON_VALID_DAYS}
            )
        return NewProposalVersion(
            amount_cents=(
                changes.amount_cents
                if changes.amount_cents is not None
                else requested.amount_cents
            ),
            currency=requested.currency,
            reason_code=(
                changes.reason_code
                if changes.reason_code is not None
                else requested.reason_code
            ),
            reason_text=(
                changes.reason_text
                if changes.reason_text is not None
                else requested.reason_text
            ),
            parameters_json=parameters_json,
        )

    # —— Run 状态查询与显式恢复 ——

    def get_run_status(
        self,
        *,
        organization_id: str,
        run_id: str,
    ) -> RunStatusView:
        """查询 Action Run 状态：提案、审批、决定、执行与结果摘要。

        设计 12.4：跨租户 Run 与不存在 Run 一样返回 ``RUN_NOT_FOUND``；
        本方法只读，不审批、不恢复、不执行动作。
        """
        run = self._store.get_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        proposal = None
        approval = None
        decision = None
        current_version = None
        execution = None
        result = None
        if run.proposal_id is not None:
            proposal = self._store.get_proposal(
                organization_id=organization_id,
                proposal_id=run.proposal_id,
            )
            if proposal.current_version_id is not None:
                current_version = self._store.get_version(
                    organization_id=organization_id,
                    version_id=proposal.current_version_id,
                )
            approval = self._store.get_approval_by_proposal(
                organization_id=organization_id,
                proposal_id=proposal.proposal_id,
            )
            decision = self._store.get_decision(
                organization_id=organization_id,
                approval_id=approval.approval_id,
            )
            if current_version is not None:
                execution = self._store.get_execution_by_version(
                    organization_id=organization_id,
                    proposal_version_id=current_version.version_id,
                )
        if execution is not None and execution.result_json:
            try:
                result = json.loads(execution.result_json)
            except json.JSONDecodeError as exc:
                raise ExecutionDataIntegrityError(
                    "执行结果 JSON 损坏"
                ) from exc
        return RunStatusView(
            run=run,
            proposal=proposal,
            approval=approval,
            decision=decision,
            current_version=current_version,
            execution=execution,
            result=result,
        )

    def resume_run(
        self,
        *,
        organization_id: str,
        run_id: str,
        requested_by_user_id: str,
    ) -> ResumeOutcome:
        """显式恢复 Run：RBAC、可恢复性校验、恢复审计与运行器调用。

        设计 11.5：禁止恢复已终态、不可恢复失败或缺少批准决定的 Run；
        恢复请求本身幂等，由运行器按 checkpoint 位置继续；恢复失败保留
        业务事实，可再次显式恢复，不需要重复审批。
        """
        self._require_admin(
            organization_id=organization_id,
            user_id=requested_by_user_id,
            not_found_error=RunNotFoundError,
        )
        run = self._store.get_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        if run.status is ActionRunStatus.SUCCEEDED:
            status = self.get_run_status(
                organization_id=organization_id,
                run_id=run_id,
            )
            return ResumeOutcome(
                run=status.run,
                resume_ok=True,
                error_code=None,
                result=status.result,
            )
        self._require_resumable(
            run,
            organization_id=organization_id,
        )
        self._store.append_audit_log(
            organization_id=organization_id,
            run_id=run_id,
            proposal_id=run.proposal_id,
            actor_type=AuditActorType.USER,
            actor_user_id=requested_by_user_id,
            event_type=EVENT_RUN_RESUME_REQUESTED,
            resource_type="action_run",
            resource_id=run_id,
            details_json=json.dumps(
                {"requested_by_user_id": requested_by_user_id}
            ),
        )
        resume_ok, error_code = self._try_resume(
            organization_id,
            run_id,
        )
        status = self.get_run_status(
            organization_id=organization_id,
            run_id=run_id,
        )
        return ResumeOutcome(
            run=status.run,
            resume_ok=resume_ok,
            error_code=error_code,
            result=status.result,
        )

    def _require_resumable(
        self,
        run: ActionRun,
        *,
        organization_id: str,
    ) -> None:
        """校验 Run 是否允许显式恢复。

        已取消或不可重试失败禁止恢复；已成功 Run 由入口幂等返回
        稳定结果；等待审批但没有持久化
        决定的 Run 属于「未批准的执行分支」，同样禁止恢复。
        """
        if run.status is ActionRunStatus.CANCELLED:
            raise RunNotResumableError("已终态的 Run 不允许恢复")
        if run.status is ActionRunStatus.FAILED and not run.last_error_retryable:
            raise RunNotResumableError("不可恢复失败的 Run 不允许恢复")
        if run.status is ActionRunStatus.AWAITING_APPROVAL:
            if run.proposal_id is None:
                raise RunNotResumableError("等待审批的 Run 缺少提案，不允许恢复")
            approval = self._store.get_approval_by_proposal(
                organization_id=organization_id,
                proposal_id=run.proposal_id,
            )
            decision = self._store.get_decision(
                organization_id=organization_id,
                approval_id=approval.approval_id,
            )
            if decision is None:
                raise RunNotResumableError(
                    "未批准的执行分支不允许恢复"
                )

    # —— 内部辅助 ——

    def _require_admin(
        self,
        *,
        organization_id: str,
        user_id: str,
        not_found_error: type[ActionError],
    ) -> None:
        """重新读取当前 Membership 并校验 admin 角色。

        设计 14：审批与恢复时重新读取 Membership，不能使用 checkpoint 中
        保存的历史角色；非成员统一表现为资源不存在（404 语义）。
        """
        try:
            context = self._organization_service.get_tenant_context(
                user_id=user_id,
                organization_id=organization_id,
            )
        except OrganizationAccessDeniedError as exc:
            raise not_found_error("资源不存在或不属于当前企业") from exc
        if context.role is not MembershipRole.ADMIN:
            raise ApprovalAdminRequiredError(
                "只有当前企业管理员可以执行此操作"
            )

    def _try_start(
        self,
        organization_id: str,
        run_id: str,
    ) -> tuple[bool, str | None]:
        """尝试首次启动工作流；失败时业务事实已持久化，可稍后显式恢复。"""
        if self._runner is None:
            return False, CheckpointUnavailableError.code
        try:
            self._runner.start(
                organization_id=organization_id,
                run_id=run_id,
            )
        except ActionError as exc:
            return False, exc.code
        return True, None

    def _try_resume(
        self,
        organization_id: str,
        run_id: str,
    ) -> tuple[bool, str | None]:
        """尝试恢复工作流；失败时决定仍然有效，可再次显式恢复。"""
        if self._runner is None:
            return False, CheckpointUnavailableError.code
        try:
            self._runner.resume(
                organization_id=organization_id,
                run_id=run_id,
            )
        except ActionError as exc:
            return False, exc.code
        return True, None
