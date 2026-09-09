/*
 * 审批与 Run 的集中显示映射（中文字案 + 状态色）。
 *
 * 事实说明：
 *  - 所有枚举值以后端 `app/actions/base.py` / `app/schemas/action.py` 为准；
 *  - 展示文案集中在本文件，禁止把中文字符串散落在多个组件；
 *  - 未知枚举值必须安全降级为原始值（displayText / tagType 兜底），
 *    不能导致页面崩溃；
 *  - 颜色不能作为唯一状态表达方式：所有状态都同时展示文字。
 */

import type { TagProps } from 'naive-ui'

import type {
  ActionRunStatusValue,
  ActionTypeValue,
  ApprovalDecisionTypeValue,
  ApprovalStatusValue,
  CompensationReasonCodeValue,
  ProposalStatusValue,
  RefundReasonCodeValue,
  RefundScopeValue,
  ToolExecutionStatusValue,
} from '@/api/actionTypes'
import type { RunStatusResponse } from '@/api/actionTypes'

/** 动作类型 → 中文标签。 */
export const ACTION_TYPE_TEXT: Record<ActionTypeValue, string> = {
  refund: '退款',
  compensation: '补偿',
}

/** 工作流类型 → 中文标签（Run 视图展示用，与动作类型取值一致）。 */
export const WORKFLOW_TYPE_TEXT: Record<ActionTypeValue, string> = {
  refund: '退款',
  compensation: '补偿',
}

/** 审批状态 → 中文标签。 */
export const APPROVAL_STATUS_TEXT: Record<ApprovalStatusValue, string> = {
  pending: '待审批',
  approved: '已批准',
  approved_with_changes: '修改后批准',
  rejected: '已拒绝',
}

/** 审批状态 → 标签色（与文字同时展示，颜色不单独表意）。 */
export const APPROVAL_STATUS_TAG: Record<ApprovalStatusValue, TagProps['type']> = {
  pending: 'warning',
  approved: 'success',
  approved_with_changes: 'info',
  rejected: 'error',
}

/** 审批决定类型 → 中文标签。 */
export const DECISION_TYPE_TEXT: Record<ApprovalDecisionTypeValue, string> = {
  approved: '批准',
  approved_with_changes: '修改后批准',
  rejected: '拒绝',
}

/** Action Run 状态 → 中文标签。 */
export const RUN_STATUS_TEXT: Record<ActionRunStatusValue, string> = {
  queued: '排队中',
  running: '执行中',
  awaiting_approval: '等待审批',
  succeeded: '已成功',
  failed: '已失败',
  cancelled: '已取消',
}

/** Action Run 状态 → 标签色（与文字同时展示）。 */
export const RUN_STATUS_TAG: Record<ActionRunStatusValue, TagProps['type']> = {
  queued: 'info',
  running: 'info',
  awaiting_approval: 'warning',
  succeeded: 'success',
  failed: 'error',
  cancelled: 'default',
}

/** 提案状态 → 中文标签。 */
export const PROPOSAL_STATUS_TEXT: Record<ProposalStatusValue, string> = {
  awaiting_approval: '等待审批',
  approved: '已批准',
  executing: '执行中',
  succeeded: '已成功',
  rejected: '已拒绝',
  failed: '已失败',
  cancelled: '已取消',
}

/** 工具执行状态 → 中文标签。 */
export const EXECUTION_STATUS_TEXT: Record<ToolExecutionStatusValue, string> = {
  claimed: '已认领',
  running: '执行中',
  succeeded: '已成功',
  failed_retryable: '失败（可重试）',
  failed_terminal: '失败（不可恢复）',
}

/** 退款范围 → 中文标签。 */
export const REFUND_SCOPE_TEXT: Record<RefundScopeValue, string> = {
  full: '全额退款',
  partial: '部分退款',
}

/** 退款原因码 → 中文标签（提交值必须保持英文枚举，仅展示层翻译）。 */
export const REFUND_REASON_TEXT: Record<RefundReasonCodeValue, string> = {
  customer_cancellation: '客户主动取消',
  changed_mind_return: '无理由退货',
  quality_issue: '商品质量问题',
  damaged_item: '到货破损',
  wrong_item: '商品与订单不符',
  missing_item: '订单商品缺失',
  not_as_described: '与页面描述不符',
  out_of_stock: '商家缺货无法履约',
  delivery_delay: '配送超时',
  lost_in_transit: '物流确认丢件',
  other: '其他原因',
}

/** 补偿原因码 → 中文标签（提交值保持英文枚举）。 */
export const COMPENSATION_REASON_TEXT: Record<CompensationReasonCodeValue, string> = {
  delayed_shipment: '发货延迟',
  transit_delay: '运输途中延误',
  customer_dispute: '客户争议',
  other: '其他原因',
}

/** 退款原因码列表（顺序即枚举声明顺序，供表单下拉使用）。 */
export const REFUND_REASON_CODES: RefundReasonCodeValue[] = [
  'customer_cancellation',
  'changed_mind_return',
  'quality_issue',
  'damaged_item',
  'wrong_item',
  'missing_item',
  'not_as_described',
  'out_of_stock',
  'delivery_delay',
  'lost_in_transit',
  'other',
]

/** 补偿原因码列表（顺序即枚举声明顺序，供表单下拉使用）。 */
export const COMPENSATION_REASON_CODES: CompensationReasonCodeValue[] = [
  'delayed_shipment',
  'transit_delay',
  'customer_dispute',
  'other',
]

/** 按动作类型返回原因码选项（value=英文枚举，label=中文文案）。 */
export function reasonCodeOptions(actionType: ActionTypeValue): {
  value: string
  label: string
}[] {
  if (actionType === 'compensation') {
    return COMPENSATION_REASON_CODES.map((code) => ({
      value: code,
      label: COMPENSATION_REASON_TEXT[code],
    }))
  }
  return REFUND_REASON_CODES.map((code) => ({
    value: code,
    label: REFUND_REASON_TEXT[code],
  }))
}

/** 通用映射查表：未知值安全降级为原始字符串，避免界面显示空。 */
export function displayText(
  map: Record<string, string>,
  value: string | null | undefined,
): string {
  if (value === null || value === undefined) return '—'
  return map[value] ?? value
}

/** 通用标签色映射查表：未知值降级为 default（中性色）。 */
export function statusTagType(
  map: Record<string, TagProps['type']>,
  value: string,
): TagProps['type'] {
  return map[value] ?? 'default'
}

/** 是否为 Run 的终端状态（succeeded/failed/cancelled 不再推进）。 */
export function isRunTerminalStatus(status: ActionRunStatusValue | null | undefined): boolean {
  return status === 'succeeded' || status === 'failed' || status === 'cancelled'
}

/**
 * Run 恢复按钮条件（集中派生，禁止散落在模板）。
 *
 * 同时满足才允许展示恢复入口：
 *  1. 当前用户是 admin；
 *  2. 最近一次决定响应明确 resume_required=true，或 Run/执行信息表明
 *     错误可重试（run.last_error_retryable / execution.error_retryable）；
 *  3. Run 不是「明显不可恢复」的终态（succeeded / cancelled / 不可重试的 failed）。
 *
 * @param run            Run 完整状态（可能尚未加载）
 * @param isAdmin        当前企业角色是否为 admin（仅界面展示，后端仍会校验）
 * @param resumeRequired 最近一次 DecisionResponse 的 resume_required 标记
 */
export function canResumeRun(
  run: RunStatusResponse | null,
  isAdmin: boolean,
  resumeRequired: boolean,
): boolean {
  if (!run || !isAdmin) return false
  const { status } = run.run
  const retryableError =
    run.run.last_error_retryable || (run.execution?.error_retryable ?? false)
  if (!resumeRequired && !retryableError) return false
  // 明显不可恢复的终态：已成功 / 已取消 / 不可重试失败
  if (status === 'succeeded' || status === 'cancelled') return false
  if (status === 'failed' && !retryableError) return false
  return true
}
