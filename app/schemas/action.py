"""退款/补偿审批与 Run 查询的 HTTP 请求与响应模型（设计 13）。

本模块只负责请求结构的严格校验与响应的稳定组装：

- 请求体使用 ``extra='forbid'``，枚举拒绝未知值，字符串统一去除首尾
  空白并限制长度（设计 14）；
- 业务校验（RBAC、允许修改字段、金额一致性）由 ``ActionWorkflowService``
  完成，不在本层重复实现；
- 响应不暴露模型 reasoning、``thread_id``、幂等键等内部标识
  （设计 12.6 / 9.9）。
"""

import json

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing import Annotated

from app.actions.base import (
    ActionProposal,
    ActionProposalVersion,
    ActionRun,
    ActionRunStatus,
    ActionType,
    Approval,
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalStatus,
    ExecutionDataIntegrityError,
    ProposalStatus,
    RefundScope,
    ToolExecution,
    ToolExecutionStatus,
)
from app.actions.service import (
    ApprovalDetail,
    ApprovalListItem,
    DecisionOutcome,
    ResumeOutcome,
    RunStatusView,
)

# 审批备注：去除首尾空白并限制长度，None 表示未填写
CommentField = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=1000)
]
# 原因码：去除首尾空白并限制长度，枚举合法性由服务层按动作类型校验
ReasonCodeField = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]
# 原因说明：去除首尾空白并限制长度
ReasonTextField = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]


# —— 请求模型 ——


class DecisionChanges(BaseModel):
    """「修改后批准」允许提交的修改字段，未提供的字段沿用请求版本（设计 13.2）。"""

    model_config = ConfigDict(extra="forbid")

    amount_cents: int | None = Field(  # 修改后的金额（分），必须为严格正整数
        default=None,
        gt=0,
        strict=True,
    )
    reason_code: ReasonCodeField | None = None  # 修改后的原因码，必须属于固定枚举
    reason_text: ReasonTextField | None = None  # 修改后的补充说明
    refund_scope: RefundScope | None = None  # 退款专属参数；补偿动作不允许修改


class DecisionRequest(BaseModel):
    """审批决定请求体（设计 13.2）。"""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecisionType  # 决定类型：approved / approved_with_changes / rejected
    changes: DecisionChanges | None = None  # 修改后批准的字段；批准或拒绝时必须为空
    comment: CommentField | None = None  # 审批备注，可为空


# —— 视图模型 ——


class RunView(BaseModel):
    """动作 Run 的视图，不暴露 thread_id 等内部标识。"""

    run_id: str  # Run 唯一标识
    workflow_type: ActionType  # 工作流类型：退款或补偿
    status: ActionRunStatus  # Run 当前状态
    created_by_user_id: str  # 发起提案的用户标识
    last_error_code: str | None  # 最近一次失败的稳定错误码，成功时为 None
    last_error_retryable: bool  # 最近一次失败是否可显式恢复重试
    created_at: str  # 创建时间（UTC 文本格式）
    updated_at: str  # 最后更新时间（UTC 文本格式）
    completed_at: str | None  # 进入终态的时间，未完成时为 None

    @classmethod
    def from_domain(cls, run: ActionRun) -> "RunView":
        """把领域 Run 对象组装为响应视图。"""
        return cls(
            run_id=run.run_id,
            workflow_type=run.workflow_type,
            status=run.status,
            created_by_user_id=run.created_by_user_id,
            last_error_code=run.last_error_code,
            last_error_retryable=run.last_error_retryable,
            created_at=run.created_at,
            updated_at=run.updated_at,
            completed_at=run.completed_at,
        )


class ProposalView(BaseModel):
    """提案根记录的视图。"""

    proposal_id: str  # 提案唯一标识
    order_id: str  # 关联订单标识
    action_type: ActionType  # 动作类型：退款或补偿
    status: ProposalStatus  # 提案当前状态
    created_by_user_id: str  # 提案创建人用户标识
    created_at: str  # 创建时间（UTC 文本格式）
    updated_at: str  # 最后更新时间（UTC 文本格式）

    @classmethod
    def from_domain(cls, proposal: ActionProposal) -> "ProposalView":
        """把领域提案对象组装为响应视图。"""
        return cls(
            proposal_id=proposal.proposal_id,
            order_id=proposal.order_id,
            action_type=proposal.action_type,
            status=proposal.status,
            created_by_user_id=proposal.created_by_user_id,
            created_at=proposal.created_at,
            updated_at=proposal.updated_at,
        )


class ApprovalView(BaseModel):
    """审批请求的视图。"""

    approval_id: str  # 审批唯一标识
    proposal_id: str  # 关联提案标识
    requested_version_id: str  # 请求审批的版本标识
    requested_by_user_id: str  # 发起审批的提案人用户标识
    status: ApprovalStatus  # 审批当前状态
    created_at: str  # 创建时间（UTC 文本格式）
    decided_at: str | None  # 作出决定的时间，未决定时为 None

    @classmethod
    def from_domain(cls, approval: Approval) -> "ApprovalView":
        """把领域审批对象组装为响应视图。"""
        return cls(
            approval_id=approval.approval_id,
            proposal_id=approval.proposal_id,
            requested_version_id=approval.requested_version_id,
            requested_by_user_id=approval.requested_by_user_id,
            status=approval.status,
            created_at=approval.created_at,
            decided_at=approval.decided_at,
        )


class DecisionView(BaseModel):
    """审批决定摘要的视图（不可变，一旦落库不可更新或删除）。"""

    decision_id: str  # 决定唯一标识
    decision: ApprovalDecisionType  # 决定类型
    decided_version_id: str  # 决定采用的版本标识（拒绝时指向被拒绝的请求版本）
    decided_by_user_id: str  # 作出决定的管理员用户标识
    comment: str | None  # 审批备注，可为空
    created_at: str  # 决定时间（UTC 文本格式）

    @classmethod
    def from_domain(cls, decision: ApprovalDecision) -> "DecisionView":
        """把领域决定对象组装为响应视图。"""
        return cls(
            decision_id=decision.decision_id,
            decision=decision.decision,
            decided_version_id=decision.decided_version_id,
            decided_by_user_id=decision.decided_by_user_id,
            comment=decision.comment,
            created_at=decision.created_at,
        )


class VersionView(BaseModel):
    """提案版本视图：金额、原因与类型专属参数（设计 8.2 / 8.3）。"""

    version_id: str  # 版本唯一标识
    version_no: int  # 版本序号，从 1 开始只增不减
    amount_cents: int  # 金额（分），必须为正整数
    currency: str  # 币种，必须等于订单币种
    reason_code: str  # 原因码，使用固定枚举值
    reason_text: str  # 客服给出的补充说明
    refund_scope: RefundScope | None  # 退款专属参数；补偿版本为 None
    coupon_valid_days: int | None  # 补偿券有效期；退款版本为 None，本阶段固定 30 天
    created_by_user_id: str  # 版本创建人（提案人或审批管理员）
    created_at: str  # 创建时间（UTC 文本格式）

    @classmethod
    def from_domain(cls, version: ActionProposalVersion) -> "VersionView":
        """把领域版本对象组装为响应视图，类型专属参数从严格 JSON 解析。"""
        try:
            parameters = json.loads(version.parameters_json)
        except json.JSONDecodeError as exc:
            # 版本参数属于业务事实，损坏时按数据完整性错误暴露，不静默吞掉。
            raise ExecutionDataIntegrityError("提案版本参数 JSON 损坏") from exc
        refund_scope = (
            RefundScope(parameters["refund_scope"])
            if "refund_scope" in parameters
            else None
        )
        coupon_valid_days = (
            int(parameters["coupon_valid_days"])
            if "coupon_valid_days" in parameters
            else None
        )
        return cls(
            version_id=version.version_id,
            version_no=version.version_no,
            amount_cents=version.amount_cents,
            currency=version.currency,
            reason_code=version.reason_code,
            reason_text=version.reason_text,
            refund_scope=refund_scope,
            coupon_valid_days=coupon_valid_days,
            created_by_user_id=version.created_by_user_id,
            created_at=version.created_at,
        )


class ExecutionView(BaseModel):
    """执行记录视图，不暴露幂等键（设计 12.6）。"""

    execution_id: str  # 执行唯一标识
    proposal_id: str  # 关联提案标识
    proposal_version_id: str  # 执行的提案版本标识
    action_type: ActionType  # 动作类型
    status: ToolExecutionStatus  # 执行当前状态
    attempt_count: int  # 尝试次数，重试时递增
    error_code: str | None  # 失败时的稳定错误码
    error_retryable: bool  # 失败是否可重试
    claimed_at: str  # 首次认领时间（UTC 文本格式）
    updated_at: str  # 最后更新时间（UTC 文本格式）
    completed_at: str | None  # 完成时间，未完成时为 None

    @classmethod
    def from_domain(cls, execution: ToolExecution) -> "ExecutionView":
        """把领域执行对象组装为响应视图。"""
        return cls(
            execution_id=execution.execution_id,
            proposal_id=execution.proposal_id,
            proposal_version_id=execution.proposal_version_id,
            action_type=execution.action_type,
            status=execution.status,
            attempt_count=execution.attempt_count,
            error_code=execution.error_code,
            error_retryable=execution.error_retryable,
            claimed_at=execution.claimed_at,
            updated_at=execution.updated_at,
            completed_at=execution.completed_at,
        )


# —— 响应模型 ——


class ApprovalListItemResponse(BaseModel):
    """审批列表项：审批摘要、Run 状态、当前版本与决定摘要（设计 13.1）。"""

    approval_id: str  # 审批唯一标识
    proposal_id: str  # 关联提案标识
    order_id: str  # 关联订单标识
    action_type: ActionType  # 动作类型
    approval_status: ApprovalStatus  # 审批状态
    run_status: ActionRunStatus  # 关联 Run 的状态
    current_version: VersionView | None  # 当前生效版本，尚未产生时为空
    version_count: int  # 提案版本总数（历史版本摘要）
    decision: DecisionView | None  # 已落库决定，未决定时为 None
    created_at: str  # 审批创建时间（UTC 文本格式）

    @classmethod
    def from_domain(cls, item: ApprovalListItem) -> "ApprovalListItemResponse":
        """把应用服务列表项组装为响应。"""
        return cls(
            approval_id=item.approval.approval_id,
            proposal_id=item.proposal.proposal_id,
            order_id=item.proposal.order_id,
            action_type=item.proposal.action_type,
            approval_status=item.approval.status,
            run_status=item.run_status,
            current_version=(
                VersionView.from_domain(item.current_version)
                if item.current_version is not None
                else None
            ),
            version_count=item.version_count,
            decision=(
                DecisionView.from_domain(item.decision)
                if item.decision is not None
                else None
            ),
            created_at=item.approval.created_at,
        )


class ApprovalDetailResponse(BaseModel):
    """审批详情：请求版本、全部历史版本、Run 状态与决定摘要（设计 13.1）。"""

    approval_id: str  # 审批唯一标识
    proposal_id: str  # 关联提案标识
    order_id: str  # 关联订单标识
    action_type: ActionType  # 动作类型
    approval_status: ApprovalStatus  # 审批状态
    run: RunView  # 关联动作 Run
    requested_version: VersionView  # 请求审批的版本
    current_version: VersionView | None  # 当前生效版本，尚未产生时为空
    versions: list[VersionView]  # 全部历史版本（只增不改，按版本号升序）
    decision: DecisionView | None  # 已落库决定，未决定时为 None
    self_approved: bool  # 提案人与审批人是否相同（决定已作出时有效）
    created_at: str  # 审批创建时间（UTC 文本格式）

    @classmethod
    def from_domain(cls, detail: ApprovalDetail) -> "ApprovalDetailResponse":
        """把应用服务详情组装为响应。"""
        return cls(
            approval_id=detail.approval.approval_id,
            proposal_id=detail.proposal.proposal_id,
            order_id=detail.proposal.order_id,
            action_type=detail.proposal.action_type,
            approval_status=detail.approval.status,
            run=RunView.from_domain(detail.run),
            requested_version=VersionView.from_domain(detail.requested_version),
            current_version=(
                VersionView.from_domain(detail.current_version)
                if detail.current_version is not None
                else None
            ),
            versions=[
                VersionView.from_domain(version)
                for version in detail.versions
            ],
            decision=(
                DecisionView.from_domain(detail.decision)
                if detail.decision is not None
                else None
            ),
            self_approved=detail.self_approved,
            created_at=detail.approval.created_at,
        )


class DecisionResponse(BaseModel):
    """审批决定响应：持久化决定与工作流恢复状态（设计 13.2）。"""

    decision_id: str  # 决定唯一标识
    decision: ApprovalDecisionType  # 决定类型
    approval_id: str  # 关联审批标识
    proposal_id: str  # 关联提案标识
    run_id: str  # 关联 Run 标识
    run_status: ActionRunStatus  # 决定后重新读取的 Run 状态
    decided_version_id: str  # 决定采用的版本标识
    decided_by_user_id: str  # 作出决定的管理员用户标识
    comment: str | None  # 审批备注，可为空
    self_approved: bool  # 提案人与审批人是否相同
    resume_required: bool  # 决定已保存但工作流尚未恢复完成，需要显式恢复
    resume_error_code: str | None  # 自动恢复失败时的稳定错误码，成功时为 None
    created_at: str  # 决定时间（UTC 文本格式）

    @classmethod
    def from_outcome(cls, outcome: DecisionOutcome) -> "DecisionResponse":
        """把应用服务决定结果组装为响应。"""
        result = outcome.result
        return cls(
            decision_id=result.decision.decision_id,
            decision=result.decision.decision,
            approval_id=result.approval.approval_id,
            proposal_id=result.proposal.proposal_id,
            run_id=result.run.run_id,
            run_status=result.run.status,
            decided_version_id=result.decision.decided_version_id,
            decided_by_user_id=result.decision.decided_by_user_id,
            comment=result.decision.comment,
            self_approved=outcome.self_approved,
            resume_required=outcome.resume_required,
            resume_error_code=outcome.resume_error_code,
            created_at=result.decision.created_at,
        )


class RunStatusResponse(BaseModel):
    """Action Run 状态查询响应（设计 13.3 / 12.4）。"""

    run: RunView  # 动作 Run 视图
    proposal: ProposalView | None  # 关联提案，Run 未关联提案时为空
    approval: ApprovalView | None  # 关联审批请求
    decision: DecisionView | None  # 已落库决定，未决定时为 None
    current_version: VersionView | None  # 当前生效版本，尚未产生时为空
    execution: ExecutionView | None  # 决定版本的执行记录，尚未执行时为 None
    result: dict | None  # 解析后的执行结果（business_record_id / order_marked_refunded）

    @classmethod
    def from_view(cls, view: RunStatusView) -> "RunStatusResponse":
        """把应用服务状态视图组装为响应。"""
        return cls(
            run=RunView.from_domain(view.run),
            proposal=(
                ProposalView.from_domain(view.proposal)
                if view.proposal is not None
                else None
            ),
            approval=(
                ApprovalView.from_domain(view.approval)
                if view.approval is not None
                else None
            ),
            decision=(
                DecisionView.from_domain(view.decision)
                if view.decision is not None
                else None
            ),
            current_version=(
                VersionView.from_domain(view.current_version)
                if view.current_version is not None
                else None
            ),
            execution=(
                ExecutionView.from_domain(view.execution)
                if view.execution is not None
                else None
            ),
            result=view.result,
        )


class ResumeResponse(BaseModel):
    """显式恢复 Run 的响应（设计 13.3）。"""

    run: RunView  # 恢复请求完成后重新读取的 Run 状态
    resume_ok: bool  # 是否成功调用工作流恢复
    error_code: str | None  # 恢复失败时的稳定错误码，成功时为 None
    result: dict | None  # 恢复后的稳定业务结果，尚未执行成功时为 None

    @classmethod
    def from_outcome(cls, outcome: ResumeOutcome) -> "ResumeResponse":
        """把应用服务恢复结果组装为响应。"""
        return cls(
            run=RunView.from_domain(outcome.run),
            resume_ok=outcome.resume_ok,
            error_code=outcome.error_code,
            result=outcome.result,
        )
