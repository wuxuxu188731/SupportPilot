/*
 * 审批与 Action Run API 请求/响应类型（DTO）。
 *
 * 与后端 `app/schemas/action.py`、`docs/frontend/api-inventory.md` 4.5 节
 * 一一对应；所有枚举值均以后端 `app/actions/base.py` 为准，不得猜测。
 * 所有属性均带中文注释；字段可空时显式标注 null（与后端 JSON 一致）。
 *
 * 命名说明：枚举使用字符串别名类型（而非 union 字面量扩散到每个字段），
 * 便于与后端枚举同步演进；未知值时由展示层安全降级为原始字符串。
 */

// —— 枚举取值（仅从后端代码取值，不新增不改名） ——

/** 动作类型：退款或优惠券补偿（后端 ActionType）。 */
export type ActionTypeValue = 'refund' | 'compensation'

/** 审批状态（后端 ApprovalStatus）。 */
export type ApprovalStatusValue =
  | 'pending'
  | 'approved'
  | 'approved_with_changes'
  | 'rejected'

/** 审批决定类型（后端 ApprovalDecisionType）。 */
export type ApprovalDecisionTypeValue =
  | 'approved'
  | 'approved_with_changes'
  | 'rejected'

/** Action Run 生命周期状态（后端 ActionRunStatus）。 */
export type ActionRunStatusValue =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

/** 提案生命周期状态（后端 ProposalStatus）。 */
export type ProposalStatusValue =
  | 'awaiting_approval'
  | 'approved'
  | 'executing'
  | 'succeeded'
  | 'rejected'
  | 'failed'
  | 'cancelled'

/** 工具执行状态（后端 ToolExecutionStatus）。 */
export type ToolExecutionStatusValue =
  | 'claimed'
  | 'running'
  | 'succeeded'
  | 'failed_retryable'
  | 'failed_terminal'

/** 退款范围（后端 RefundScope）：仅退款提案使用。 */
export type RefundScopeValue = 'full' | 'partial'

/** 退款原因固定枚举（后端 RefundReasonCode，共 11 个）。 */
export type RefundReasonCodeValue =
  | 'customer_cancellation'
  | 'changed_mind_return'
  | 'quality_issue'
  | 'damaged_item'
  | 'wrong_item'
  | 'missing_item'
  | 'not_as_described'
  | 'out_of_stock'
  | 'delivery_delay'
  | 'lost_in_transit'
  | 'other'

/** 补偿原因固定枚举（后端 CompensationReasonCode，共 4 个）。 */
export type CompensationReasonCodeValue =
  | 'delayed_shipment'
  | 'transit_delay'
  | 'customer_dispute'
  | 'other'

// —— 视图模型（后端 app/schemas/action.py 的 View 模型） ——

/** 动作 Run 的视图（后端 RunView）：不暴露 thread_id 等内部标识。 */
export interface RunView {
  /** Run 唯一标识 */
  run_id: string
  /** 工作流类型：refund（退款）或 compensation（优惠券补偿） */
  workflow_type: ActionTypeValue
  /** Run 当前状态 */
  status: ActionRunStatusValue
  /** 发起提案的用户标识（提案人） */
  created_by_user_id: string
  /** 最近一次失败的稳定错误码；从未失败或已成功时为 null */
  last_error_code: string | null
  /** 最近一次失败是否允许显式恢复重试 */
  last_error_retryable: boolean
  /** 创建时间（UTC 文本格式） */
  created_at: string
  /** 最后更新时间（UTC 文本格式） */
  updated_at: string
  /** 进入终态的时间；未完成或未进入终态时为 null */
  completed_at: string | null
}

/** 提案根记录的视图（后端 ProposalView）。 */
export interface ProposalView {
  /** 提案唯一标识 */
  proposal_id: string
  /** 关联订单标识 */
  order_id: string
  /** 动作类型：退款或补偿 */
  action_type: ActionTypeValue
  /** 提案当前状态 */
  status: ProposalStatusValue
  /** 提案创建人用户标识 */
  created_by_user_id: string
  /** 创建时间（UTC 文本格式） */
  created_at: string
  /** 最后更新时间（UTC 文本格式） */
  updated_at: string
}

/** 审批请求的视图（后端 ApprovalView）。 */
export interface ApprovalView {
  /** 审批唯一标识 */
  approval_id: string
  /** 关联提案标识 */
  proposal_id: string
  /** 请求审批的版本标识 */
  requested_version_id: string
  /** 发起审批的提案人用户标识 */
  requested_by_user_id: string
  /** 审批当前状态 */
  status: ApprovalStatusValue
  /** 创建时间（UTC 文本格式） */
  created_at: string
  /** 作出决定的时间；未决定时为 null */
  decided_at: string | null
}

/** 审批决定摘要（后端 DecisionView）：不可变，一旦落库不可更新或删除。 */
export interface DecisionView {
  /** 决定唯一标识 */
  decision_id: string
  /** 决定类型：批准 / 修改后批准 / 拒绝 */
  decision: ApprovalDecisionTypeValue
  /** 决定采用的版本标识（拒绝时指向被拒绝的请求版本） */
  decided_version_id: string
  /** 作出决定的管理员用户标识 */
  decided_by_user_id: string
  /** 审批备注；未填写时为 null */
  comment: string | null
  /** 决定时间（UTC 文本格式） */
  created_at: string
}

/** 提案版本视图（后端 VersionView）：金额、原因与类型专属参数。 */
export interface VersionView {
  /** 版本唯一标识 */
  version_id: string
  /** 版本序号，从 1 开始只增不减 */
  version_no: number
  /** 金额（整数分，非小数金额字段） */
  amount_cents: number
  /** 币种（等于订单币种，样例数据为 CNY） */
  currency: string
  /** 原因码：退款 11 个枚举 / 补偿 4 个枚举 */
  reason_code: string
  /** 客服给出的补充说明 */
  reason_text: string
  /** 退款专属参数：退款范围；补偿提案为 null */
  refund_scope: RefundScopeValue | null
  /** 补偿券有效天数；退款提案为 null（本阶段固定 30 天） */
  coupon_valid_days: number | null
  /** 版本创建人（提案人或审批管理员） */
  created_by_user_id: string
  /** 创建时间（UTC 文本格式） */
  created_at: string
}

/** 执行记录视图（后端 ExecutionView）：不暴露幂等键。 */
export interface ExecutionView {
  /** 执行唯一标识 */
  execution_id: string
  /** 关联提案标识 */
  proposal_id: string
  /** 执行的提案版本标识 */
  proposal_version_id: string
  /** 动作类型 */
  action_type: ActionTypeValue
  /** 执行当前状态 */
  status: ToolExecutionStatusValue
  /** 尝试次数，重试时递增 */
  attempt_count: number
  /** 失败时的稳定错误码；成功时为 null */
  error_code: string | null
  /** 失败是否可重试 */
  error_retryable: boolean
  /** 首次认领时间（UTC 文本格式） */
  claimed_at: string
  /** 最后更新时间（UTC 文本格式） */
  updated_at: string
  /** 完成时间；未完成时为 null */
  completed_at: string | null
}

// —— 审批列表 ——

/** 审批列表查询参数（后端 GET /approvals/）。 */
export interface ApprovalListQuery {
  /** 按审批状态筛选；不传表示不过滤（全部） */
  status?: ApprovalStatusValue
  /** 每页条数：1–200，默认 20 */
  limit?: number
  /** 跳过的条数：≥0，默认 0 */
  offset?: number
}

/** 审批列表项（后端 ApprovalListItemResponse）。 */
export interface ApprovalListItem {
  /** 审批唯一标识 */
  approval_id: string
  /** 关联提案标识 */
  proposal_id: string
  /** 关联订单标识 */
  order_id: string
  /** 动作类型：退款或补偿 */
  action_type: ActionTypeValue
  /** 审批状态 */
  approval_status: ApprovalStatusValue
  /** 关联 Run 的状态 */
  run_status: ActionRunStatusValue
  /** 当前生效版本；尚未产生时为 null */
  current_version: VersionView | null
  /** 提案版本总数（历史版本摘要） */
  version_count: number
  /** 已落库决定；未决定时为 null */
  decision: DecisionView | null
  /** 审批创建时间（UTC 文本格式） */
  created_at: string
}

// —— 审批详情 ——

/** 审批详情（后端 ApprovalDetailResponse）。 */
export interface ApprovalDetailResponse {
  /** 审批唯一标识 */
  approval_id: string
  /** 关联提案标识 */
  proposal_id: string
  /** 关联订单标识 */
  order_id: string
  /** 动作类型：退款或补偿 */
  action_type: ActionTypeValue
  /** 审批状态 */
  approval_status: ApprovalStatusValue
  /** 关联动作 Run 视图 */
  run: RunView
  /** 请求审批的版本 */
  requested_version: VersionView
  /** 当前生效版本；尚未产生时为 null */
  current_version: VersionView | null
  /** 全部历史版本（只增不改，按版本号升序） */
  versions: VersionView[]
  /** 已落库决定；未决定时为 null */
  decision: DecisionView | null
  /** 提案人与审批人是否相同（决定已作出时有效） */
  self_approved: boolean
  /** 审批创建时间（UTC 文本格式） */
  created_at: string
}

// —— 审批决定 ——

/** 「修改后批准」允许提交的修改字段（后端 DecisionChanges）。 */
export interface DecisionChangesPayload {
  /** 修改后金额（整数分）；与请求版本相同时不提交 */
  amount_cents?: number
  /** 修改后原因码；与请求版本相同时不提交 */
  reason_code?: string
  /** 修改后补充说明；与请求版本相同时不提交 */
  reason_text?: string
  /** 退款专属参数；仅退款提案允许提交，补偿提案禁止 */
  refund_scope?: RefundScopeValue
}

/** 审批决定请求体（后端 DecisionRequest，extra=forbid）。 */
export interface DecisionRequestPayload {
  /** 决定类型：approved（批准）/ approved_with_changes（修改后批准）/ rejected（拒绝） */
  decision: ApprovalDecisionTypeValue
  /** 修改后批准的字段；批准或拒绝时必须为 null */
  changes: DecisionChangesPayload | null
  /** 审批备注；可为 null，去首尾空白后最长 1000 字符 */
  comment: string | null
}

/** 审批决定响应（后端 DecisionResponse，HTTP 200/201/202 共用）。 */
export interface DecisionResponse {
  /** 决定唯一标识 */
  decision_id: string
  /** 决定类型 */
  decision: ApprovalDecisionTypeValue
  /** 关联审批标识 */
  approval_id: string
  /** 关联提案标识 */
  proposal_id: string
  /** 关联 Run 标识 */
  run_id: string
  /** 决定后重新读取的 Run 状态 */
  run_status: ActionRunStatusValue
  /** 决定采用的版本标识 */
  decided_version_id: string
  /** 作出决定的管理员用户标识 */
  decided_by_user_id: string
  /** 审批备注；可为 null */
  comment: string | null
  /** 提案人与审批人是否相同 */
  self_approved: boolean
  /** 决定已保存但工作流尚未恢复完成，需要显式恢复 */
  resume_required: boolean
  /** 自动恢复失败时的稳定错误码；成功时为 null */
  resume_error_code: string | null
  /** 决定时间（UTC 文本格式） */
  created_at: string
}

/** 决定提交结果：真实 HTTP 状态码 + 响应体（前端必须按 200/201/202 分流）。 */
export interface DecisionSubmitResult {
  /** 真实 HTTP 状态码：200 幂等重复 / 201 首次且自动恢复成功 / 202 决定已保存但恢复失败 */
  status: number
  /** 决定响应体 */
  data: DecisionResponse
}

// —— Run 状态与恢复 ——

/** Run 状态查询响应（后端 RunStatusResponse）。 */
export interface RunStatusResponse {
  /** 动作 Run 视图 */
  run: RunView
  /** 关联提案；Run 未关联提案时为 null */
  proposal: ProposalView | null
  /** 关联审批请求 */
  approval: ApprovalView | null
  /** 已落库决定；未决定时为 null */
  decision: DecisionView | null
  /** 当前生效版本；尚未产生时为 null */
  current_version: VersionView | null
  /** 决定版本的执行记录；尚未执行时为 null */
  execution: ExecutionView | null
  /** 解析后的业务结果（business_record_id / order_marked_refunded）；未执行成功时为 null */
  result: { business_record_id: string; order_marked_refunded: boolean } | null
}

/** 显式恢复 Run 响应（后端 ResumeResponse）。 */
export interface ResumeResponse {
  /** 恢复请求完成后重新读取的 Run 状态 */
  run: RunView
  /** 是否成功调用工作流恢复 */
  resume_ok: boolean
  /** 恢复失败时的稳定错误码；成功时为 null */
  error_code: string | null
  /** 恢复后的稳定业务结果；尚未执行成功时为 null */
  result: { business_record_id: string; order_marked_refunded: boolean } | null
}
