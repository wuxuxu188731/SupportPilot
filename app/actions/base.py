"""退款/补偿审批与可靠执行的领域契约。

本模块定义动作工作流的枚举、实体、异常、Store 接口以及跨方法返回的结果类型。
领域对象均为不可变值对象（frozen dataclass），数据库读写全部由 ``ActionStore``
协议的实现负责，且每个方法都必须显式接收 ``organization_id`` 以保证租户隔离。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ActionType(str, Enum):
    """动作类型，决定提案背后的业务工作流。"""

    REFUND = "refund"  # 退款动作
    COMPENSATION = "compensation"  # 优惠券补偿动作


class RefundReasonCode(str, Enum):
    """退款原因固定枚举。

    枚举值一旦进入历史记录不得改名或改变含义，后续只能新增或停用。
    """

    CUSTOMER_CANCELLATION = "customer_cancellation"  # 客户在允许阶段主动取消订单
    CHANGED_MIND_RETURN = "changed_mind_return"  # 符合无理由退货条件
    QUALITY_ISSUE = "quality_issue"  # 商品存在功能、性能或制造质量问题
    DAMAGED_ITEM = "damaged_item"  # 商品到货时破损
    WRONG_ITEM = "wrong_item"  # 实际收到的商品与订单不符
    MISSING_ITEM = "missing_item"  # 订单内部分或全部商品缺失
    NOT_AS_DESCRIBED = "not_as_described"  # 商品与页面描述存在实质差异
    OUT_OF_STOCK = "out_of_stock"  # 商家缺货，无法履约
    DELIVERY_DELAY = "delivery_delay"  # 配送超时，客户选择退款
    LOST_IN_TRANSIT = "lost_in_transit"  # 物流确认丢件
    OTHER = "other"  # 其他未覆盖原因，必须填写详细说明


class CompensationReasonCode(str, Enum):
    """补偿原因固定枚举。"""

    DELAYED_SHIPMENT = "delayed_shipment"  # 发货延迟
    TRANSIT_DELAY = "transit_delay"  # 运输途中延误
    CUSTOMER_DISPUTE = "customer_dispute"  # 客户争议
    OTHER = "other"  # 其他未覆盖原因


class RefundScope(str, Enum):
    """退款范围，属于退款动作的类型专属参数。"""

    FULL = "full"  # 全额退款：金额必须等于执行时可退余额
    PARTIAL = "partial"  # 部分退款：人工确认的明确金额


class ActionRunStatus(str, Enum):
    """动作 Run 的生命周期状态。"""

    QUEUED = "queued"  # 已创建但尚未开始执行图
    RUNNING = "running"  # 图正在执行
    AWAITING_APPROVAL = "awaiting_approval"  # 已暂停等待审批决定
    SUCCEEDED = "succeeded"  # 终态：执行成功
    FAILED = "failed"  # 终态：校验或执行失败
    CANCELLED = "cancelled"  # 终态：审批拒绝导致的人工终止


class ProposalStatus(str, Enum):
    """提案的生命周期状态。"""

    AWAITING_APPROVAL = "awaiting_approval"  # 等待审批
    APPROVED = "approved"  # 已批准，等待执行
    EXECUTING = "executing"  # 正在执行
    SUCCEEDED = "succeeded"  # 执行成功
    REJECTED = "rejected"  # 审批拒绝
    FAILED = "failed"  # 执行失败
    CANCELLED = "cancelled"  # 已取消


class ApprovalStatus(str, Enum):
    """审批请求的状态。"""

    PENDING = "pending"  # 等待决定
    APPROVED = "approved"  # 已批准
    APPROVED_WITH_CHANGES = "approved_with_changes"  # 修改后批准
    REJECTED = "rejected"  # 已拒绝


class ApprovalDecisionType(str, Enum):
    """审批决定的类型。"""

    APPROVED = "approved"  # 批准
    APPROVED_WITH_CHANGES = "approved_with_changes"  # 修改后批准
    REJECTED = "rejected"  # 拒绝


class ToolExecutionStatus(str, Enum):
    """单次工具执行的状态。"""

    CLAIMED = "claimed"  # 已按幂等键认领，尚未开始执行
    RUNNING = "running"  # 正在执行
    SUCCEEDED = "succeeded"  # 终态：执行成功
    FAILED_RETRYABLE = "failed_retryable"  # 可重试失败，可用原幂等键再次认领
    FAILED_TERMINAL = "failed_terminal"  # 终态：不可恢复失败


class AuditActorType(str, Enum):
    """审计事件的执行主体类型。"""

    USER = "user"  # 由登录用户触发
    SYSTEM = "system"  # 由系统自动触发


@dataclass(frozen=True)
class ActionRun:
    """一次动作工作流的运行记录。"""

    run_id: str  # Run 唯一标识（主键）
    organization_id: str  # 所属企业标识，所有读写必须限定该字段
    conversation_id: str  # 发起提案的会话标识，由 ChatService 验证后传入
    turn_id: str  # 发起提案的聊天回合标识，用于抑制同回合重复提案
    created_by_user_id: str  # 发起提案的客服用户标识
    workflow_type: ActionType  # 工作流类型：退款或补偿
    status: ActionRunStatus  # 当前运行状态
    thread_id: str  # LangGraph checkpoint 线程标识，默认等于 run_id
    proposal_id: str | None  # 关联提案标识（冗余列，权威关系见 action_proposals.run_id）
    last_error_code: str | None  # 最近一次失败的稳定错误码，成功时为 None
    last_error_retryable: bool  # 最近一次失败是否允许显式恢复重试
    created_at: str  # 创建时间（UTC 文本格式）
    updated_at: str  # 最后更新时间（UTC 文本格式）
    completed_at: str | None  # 进入终态的时间，未完成时为 None


@dataclass(frozen=True)
class ActionProposal:
    """一次业务申请（退款或补偿）的根记录。"""

    proposal_id: str  # 提案唯一标识（主键）
    organization_id: str  # 所属企业标识
    run_id: str  # 关联的动作 Run 标识
    order_id: str  # 关联的订单标识
    action_type: ActionType  # 动作类型：退款或补偿
    status: ProposalStatus  # 提案当前状态
    current_version_id: str | None  # 当前生效的版本标识，创建初始版本后即指向它
    created_by_user_id: str  # 提案创建人用户标识
    created_at: str  # 创建时间（UTC 文本格式）
    updated_at: str  # 最后更新时间（UTC 文本格式）


@dataclass(frozen=True)
class ActionProposalVersion:
    """提案的不可变版本，每次修改后批准产生新版本，历史版本不可覆盖。"""

    version_id: str  # 版本唯一标识（主键）
    organization_id: str  # 所属企业标识
    proposal_id: str  # 所属提案标识
    version_no: int  # 版本序号，从 1 开始只增不减
    amount_cents: int  # 金额，单位为分，必须为正整数
    currency: str  # 币种，必须等于订单币种
    reason_code: str  # 原因码，使用固定枚举值
    reason_text: str  # 客服给出的补充说明
    parameters_json: str  # 类型专属参数（退款 refund_scope / 补偿 coupon_valid_days）的严格 JSON
    created_by_user_id: str  # 版本创建人（提案人或审批管理员）
    created_at: str  # 创建时间（UTC 文本格式）


@dataclass(frozen=True)
class Approval:
    """一次审批请求。"""

    approval_id: str  # 审批唯一标识（主键）
    organization_id: str  # 所属企业标识
    proposal_id: str  # 关联提案标识
    requested_version_id: str  # 请求审批的版本标识
    requested_by_user_id: str  # 发起审批的提案人用户标识
    status: ApprovalStatus  # 审批当前状态
    created_at: str  # 创建时间（UTC 文本格式）
    decided_at: str | None  # 作出决定的时间，未决定时为 None


@dataclass(frozen=True)
class ApprovalDecision:
    """审批的不可变决定，一旦落库不可更新或删除。"""

    decision_id: str  # 决定唯一标识（主键）
    organization_id: str  # 所属企业标识
    approval_id: str  # 关联审批标识
    decision: ApprovalDecisionType  # 决定类型：批准 / 修改后批准 / 拒绝
    decided_version_id: str  # 决定采用的版本标识（拒绝时指向被拒绝的请求版本）
    decided_by_user_id: str  # 作出决定的管理员用户标识
    comment: str | None  # 审批备注，可为空
    created_at: str  # 决定时间（UTC 文本格式）


@dataclass(frozen=True)
class ToolExecution:
    """单次动作执行的幂等记录。"""

    execution_id: str  # 执行唯一标识（主键）
    organization_id: str  # 所属企业标识
    proposal_id: str  # 关联提案标识
    proposal_version_id: str  # 执行的提案版本标识
    action_type: ActionType  # 动作类型
    idempotency_key: str  # 幂等键，由服务端生成并版本化
    status: ToolExecutionStatus  # 执行当前状态
    attempt_count: int  # 尝试次数，重试时递增
    result_json: str | None  # 成功结果 JSON，未成功时为 None
    error_code: str | None  # 失败时的稳定错误码
    error_retryable: bool  # 失败是否可重试
    claimed_at: str  # 首次认领时间（UTC 文本格式）
    updated_at: str  # 最后更新时间（UTC 文本格式）
    completed_at: str | None  # 完成时间，未完成时为 None


@dataclass(frozen=True)
class RefundRecord:
    """模拟退款业务结果。"""

    record_id: str  # 退款记录唯一标识（主键）
    organization_id: str  # 所属企业标识
    order_id: str  # 关联订单标识
    proposal_id: str  # 关联提案标识
    proposal_version_id: str  # 执行的提案版本标识
    tool_execution_id: str  # 关联执行标识
    amount_cents: int  # 退款金额，单位为分
    currency: str  # 币种
    reason_code: str  # 退款原因码
    status: str  # 结果状态，本阶段固定为 simulated_succeeded
    created_at: str  # 创建时间（UTC 文本格式）


@dataclass(frozen=True)
class CompensationRecord:
    """模拟优惠券补偿业务结果。"""

    record_id: str  # 补偿记录唯一标识（主键）
    organization_id: str  # 所属企业标识
    order_id: str  # 关联订单标识
    proposal_id: str  # 关联提案标识
    proposal_version_id: str  # 执行的提案版本标识
    tool_execution_id: str  # 关联执行标识
    amount_cents: int  # 补偿金额，单位为分
    currency: str  # 币种
    reason_code: str  # 补偿原因码
    coupon_valid_days: int  # 优惠券有效天数，本阶段固定为 30
    status: str  # 结果状态，本阶段固定为 simulated_succeeded
    created_at: str  # 创建时间（UTC 文本格式）


@dataclass(frozen=True)
class AuditLog:
    """一次审计事件。"""

    log_id: str  # 审计记录唯一标识（主键）
    organization_id: str  # 所属企业标识
    run_id: str  # 关联的动作 Run 标识
    proposal_id: str | None  # 关联提案标识，Run 级事件可为空
    actor_type: AuditActorType  # 执行主体类型：用户或系统
    actor_user_id: str | None  # 触发用户标识，系统触发时为 None
    event_type: str  # 事件类型，如 run_created / awaiting_approval / decision_recorded 等
    resource_type: str  # 事件涉及资源类型
    resource_id: str  # 事件涉及资源标识
    details_json: str  # 事件详情 JSON，不得包含 Token、reasoning 或异常堆栈
    created_at: str  # 事件时间（UTC 文本格式）


@dataclass(frozen=True)
class NewProposalVersion:
    """创建新提案版本所需的输入参数（用于修改后批准）。"""

    amount_cents: int  # 新版本金额，单位为分
    currency: str  # 新版本币种，必须与原版本一致
    reason_code: str  # 新版本原因码
    reason_text: str  # 新版本补充说明
    parameters_json: str  # 新版本类型专属参数 JSON


@dataclass(frozen=True)
class WorkflowCreation:
    """创建动作工作流的完整业务结果。"""

    run: ActionRun  # 新建的动作 Run
    proposal: ActionProposal  # 新建的提案
    version: ActionProposalVersion  # 提案初始版本（版本 1）
    approval: Approval  # 新建的审批请求


@dataclass(frozen=True)
class DecisionResult:
    """审批决定的完整业务结果。"""

    decision: ApprovalDecision  # 落库的决定
    approval: Approval  # 更新后的审批
    proposal: ActionProposal  # 更新后的提案
    run: ActionRun  # 关联的 Run（状态可能在决定后仍等待图恢复）
    decided_version: ActionProposalVersion  # 决定采用的版本


@dataclass(frozen=True)
class ExecutionSuccess:
    """动作执行成功的完整业务结果。"""

    execution: ToolExecution  # 更新后的执行记录
    business_record_id: str  # 写入的退款或补偿记录标识
    order_marked_refunded: bool  # 本次执行是否将订单主状态置为 refunded


@dataclass(frozen=True)
class ExecutionClaim:
    """执行认领结果，用于区分本次调用是否取得执行权。"""

    execution: ToolExecution  # 当前稳定的执行记录
    acquired: bool  # 本次调用是否新取得执行权；False 时禁止重复执行动作

    @property
    def execution_id(self) -> str:
        """返回执行记录标识，兼容只需要稳定标识的调用方。"""
        return self.execution.execution_id

    @property
    def status(self) -> ToolExecutionStatus:
        """返回当前执行状态。"""
        return self.execution.status

    @property
    def attempt_count(self) -> int:
        """返回当前尝试次数。"""
        return self.execution.attempt_count

    @property
    def error_code(self) -> str | None:
        """返回当前稳定错误码。"""
        return self.execution.error_code


class ActionError(Exception):
    """动作领域异常的基类，子类携带稳定错误码。"""

    code: str = "ACTION_ERROR"  # 稳定错误码，供 API 与工具层统一映射


class ActionOrderNotFoundError(ActionError):
    """订单不存在或不属于当前企业。"""

    code = "ACTION_ORDER_NOT_FOUND"


class ActionInvalidAmountError(ActionError):
    """金额非正或格式非法。"""

    code = "ACTION_INVALID_AMOUNT"


class ActionCurrencyMismatchError(ActionError):
    """币种与订单币种不一致。"""

    code = "ACTION_CURRENCY_MISMATCH"


class ActionActiveProposalExistsError(ActionError):
    """同订单同动作类型已存在非终态提案。"""

    code = "ACTION_ACTIVE_PROPOSAL_EXISTS"


class ActionRefundBalanceExceededError(ActionError):
    """退款金额超过当前可退余额。"""

    code = "ACTION_REFUND_BALANCE_EXCEEDED"


class ActionCompensationDuplicateError(ActionError):
    """同一订单同一补偿原因已成功补偿过。"""

    code = "ACTION_COMPENSATION_DUPLICATE"


class ActionCompensationCapExceededError(ActionError):
    """补偿累计超过订单金额 50% 上限。"""

    code = "ACTION_COMPENSATION_CAP_EXCEEDED"


class ApprovalNotFoundError(ActionError):
    """审批不存在或不属于当前企业。"""

    code = "APPROVAL_NOT_FOUND"


class ApprovalAdminRequiredError(ActionError):
    """作出审批决定的调用者不是当前企业管理员。"""

    code = "APPROVAL_ADMIN_REQUIRED"


class ApprovalAlreadyDecidedError(ActionError):
    """审批已被决定，且新决定与既有决定不同。"""

    code = "APPROVAL_ALREADY_DECIDED"


class ApprovalInvalidChangesError(ActionError):
    """修改后批准的字段非法或未实际改变任何允许字段。"""

    code = "APPROVAL_INVALID_CHANGES"


class ProposalNotFoundError(ActionError):
    """提案不存在或不属于当前企业。"""

    code = "PROPOSAL_NOT_FOUND"


class RunNotFoundError(ActionError):
    """动作 Run 不存在或不属于当前企业。"""

    code = "RUN_NOT_FOUND"


class RunNotResumableError(ActionError):
    """动作 Run 处于不可恢复状态（已终态或缺少批准决定）。"""

    code = "RUN_NOT_RESUMABLE"


class RunStateConflictError(ActionError):
    """动作 Run 状态机发生非法跳转。"""

    code = "RUN_STATE_CONFLICT"


class ExecutionNotApprovedError(ActionError):
    """缺少持久化的批准决定时尝试执行动作。"""

    code = "EXECUTION_NOT_APPROVED"


class ExecutionRetryableFailureError(ActionError):
    """执行遇到可重试基础设施失败。"""

    code = "EXECUTION_RETRYABLE_FAILURE"


class ExecutionDataIntegrityError(ActionError):
    """执行引用链损坏或批准版本不存在等不可恢复数据一致性问题。"""

    code = "EXECUTION_DATA_INTEGRITY_ERROR"


class CheckpointUnavailableError(ActionError):
    """LangGraph checkpoint 基础设施不可用。"""

    code = "CHECKPOINT_UNAVAILABLE"


class ActionStore(Protocol):
    """动作工作流存储接口。

    所有方法都显式接收 ``organization_id``，禁止提供不带租户条件的按 ID 查询方法。
    实现方负责事务、状态转换、幂等认领、业务结果写入与审计。
    """

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
        """原子创建 Run、提案、版本 1 与审批请求，并写入审计。"""
        raise NotImplementedError

    def get_run(
        self,
        *,
        organization_id: str,
        run_id: str,
    ) -> ActionRun:
        raise NotImplementedError

    def get_refundable_balance(
        self,
        *,
        organization_id: str,
        order_id: str,
    ) -> int | None:
        """返回订单当前可退余额（分）。

        供执行器在执行前计算订单 refunded 标记使用；订单不存在或不属于
        当前企业时返回 None，调用方按引用链损坏处理。
        """
        raise NotImplementedError

    def get_order_number(
        self,
        *,
        organization_id: str,
        order_id: str,
    ) -> str | None:
        """返回订单号，用于审批中断载荷中的掩码展示。

        订单不存在或不属于当前企业时返回 None，调用方按引用链损坏处理。
        """
        raise NotImplementedError

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
        """原子校验并转换 Run 状态，同时写入对应审计事件。"""
        raise NotImplementedError

    def get_proposal(
        self,
        *,
        organization_id: str,
        proposal_id: str,
    ) -> ActionProposal:
        raise NotImplementedError

    def get_proposal_by_run(
        self,
        *,
        organization_id: str,
        run_id: str,
    ) -> ActionProposal:
        raise NotImplementedError

    def get_approval(
        self,
        *,
        organization_id: str,
        approval_id: str,
    ) -> Approval:
        raise NotImplementedError

    def get_approval_by_proposal(
        self,
        *,
        organization_id: str,
        proposal_id: str,
    ) -> Approval:
        raise NotImplementedError

    def get_decision(
        self,
        *,
        organization_id: str,
        approval_id: str,
    ) -> ApprovalDecision | None:
        raise NotImplementedError

    def get_version(
        self,
        *,
        organization_id: str,
        version_id: str,
    ) -> ActionProposalVersion:
        raise NotImplementedError

    def list_versions(
        self,
        *,
        organization_id: str,
        proposal_id: str,
    ) -> list[ActionProposalVersion]:
        raise NotImplementedError

    def list_approvals(
        self,
        *,
        organization_id: str,
        status: ApprovalStatus | None,
        limit: int,
        offset: int,
    ) -> list[Approval]:
        raise NotImplementedError

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
        """原子记录审批决定，修改后批准时在同一事务内创建新版本。"""
        raise NotImplementedError

    def claim_execution(
        self,
        *,
        organization_id: str,
        proposal_id: str,
        proposal_version_id: str,
        action_type: ActionType,
        idempotency_key: str,
    ) -> ExecutionClaim:
        """按幂等键认领执行；重复调用返回稳定记录但不重复授予执行权。"""
        raise NotImplementedError

    def reclaim_interrupted_execution(
        self,
        *,
        organization_id: str,
        execution_id: str,
    ) -> ExecutionClaim:
        """重新认领上次进程中断留下的 claimed/running 执行。

        调用方必须已持有单进程执行互斥权，确保本进程内没有
        其他调用者正在执行该幂等键对应的副作用。
        """
        raise NotImplementedError

    def get_execution(
        self,
        *,
        organization_id: str,
        execution_id: str,
    ) -> ToolExecution:
        raise NotImplementedError

    def get_execution_by_version(
        self,
        *,
        organization_id: str,
        proposal_version_id: str,
    ) -> ToolExecution | None:
        """按批准版本读取稳定执行记录，尚未认领时返回 None。"""
        raise NotImplementedError

    def mark_execution_running(
        self,
        *,
        organization_id: str,
        execution_id: str,
    ) -> ToolExecution:
        raise NotImplementedError

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

        订单是否标记 refunded 由 Store 按事务内最新余额自主决定；
        `mark_order_refunded` 仅为兼容既有调用签名保留。
        """
        raise NotImplementedError

    def record_execution_failure(
        self,
        *,
        organization_id: str,
        execution_id: str,
        error_code: str,
        retryable: bool,
    ) -> ToolExecution:
        """把执行标记为可重试或终态失败，并更新关联 Run 状态。"""
        raise NotImplementedError

    def record_workflow_failure(
        self,
        *,
        organization_id: str,
        run_id: str,
        error_code: str,
        retryable: bool,
    ) -> ActionRun:
        """在尚无执行记录时原子记录 Proposal 与 Run 的失败终态。"""
        raise NotImplementedError

    def list_audit_logs(
        self,
        *,
        organization_id: str,
        run_id: str,
    ) -> list[AuditLog]:
        raise NotImplementedError

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
        """追加不伴随状态变化的审计事件，例如显式恢复请求。"""
        raise NotImplementedError
