/*
 * 审批决定表单的纯函数校验与变更构造。
 *
 * 事实与约束（与后端 `app/actions/service.py`、`app/schemas/action.py` 对齐）：
 *  - 「修改后批准」以请求版本为初始值，只提交**真正变化的字段**；
 *  - 至少有一个字段实际变化才能提交（后端 422 APPROVAL_INVALID_CHANGES）；
 *  - amount_cents 必须是严格正整数（分）：用户界面按「元」输入，
 *    金额字符串安全转换为分（见 utils/money.ts，禁止浮点乘法）；
 *  - reason_text 去首尾空白后 1–2000 字符；comment 去首尾空白后 ≤1000；
 *  - refund_scope 只允许退款提案使用，补偿提案不得发送；
 *  - 前端验证不能替代后端验证：后端 422 字段错误仍会映射回表单项。
 */

import type {
  ActionTypeValue,
  DecisionChangesPayload,
  RefundScopeValue,
  VersionView,
} from '@/api/actionTypes'
import { reasonCodeOptions } from '@/utils/actionDisplay'
import { parseYuanToCents } from '@/utils/money'

/** 「修改后批准」表单草稿（金额为「元」字符串输入，提交前安全转换）。 */
export interface ApprovedChangesDraft {
  /** 金额输入（元，字符串；允许最多两位小数） */
  amountText: string
  /** 原因码（英文枚举值） */
  reasonCode: string
  /** 原因说明 */
  reasonText: string
  /** 退款范围（仅退款提案展示与提交） */
  refundScope: RefundScopeValue
}

/** 校验结果：ok=true 时只含真实变化的字段；ok=false 时给出字段级错误。 */
export interface ChangedFieldsValidation {
  /** 是否通过校验（至少一项真实变化 + 字段合法） */
  ok: boolean
  /** 只包含真正变化的字段（未变化字段不发送，与后端「沿用请求版本」一致） */
  changes: DecisionChangesPayload | null
  /** 字段级错误：字段名（amount_cents/reason_code/reason_text/refund_scope）→ 提示 */
  errors: Record<string, string>
  /** 表单级错误（如「没有任何字段发生变化」）；无则为 null */
  formMessage: string | null
}

/**
 * 校验并构造「修改后批准」的 changes 载荷。
 *
 * @param requested  请求版本（表单初始值来源）
 * @param draft      表单草稿
 * @param actionType 动作类型（决定 refund_scope 是否允许发送）
 */
export function validateApprovedWithChanges(
  requested: VersionView,
  draft: ApprovedChangesDraft,
  actionType: ActionTypeValue,
): ChangedFieldsValidation {
  const errors: Record<string, string> = {}
  const changes: DecisionChangesPayload = {}

  // 金额：元字符串 → 分；非法或非正给字段错误；与请求版本相同则不发送
  const parsedAmount = parseYuanToCents(draft.amountText)
  if (parsedAmount === null) {
    errors.amount_cents = '金额必须为大于 0 的数字，且最多两位小数'
  } else if (parsedAmount !== requested.amount_cents) {
    changes.amount_cents = parsedAmount
  }

  // 原因码：必须属于该动作类型的固定枚举；与请求版本相同则不发送
  const allowedCodes = reasonCodeOptions(actionType).map((option) => option.value)
  if (!allowedCodes.includes(draft.reasonCode)) {
    errors.reason_code = '原因码不在该动作允许的枚举中'
  } else if (draft.reasonCode !== requested.reason_code) {
    changes.reason_code = draft.reasonCode
  }

  // 原因说明：去首尾空白后 1–2000 字符；与请求版本相同则不发送
  const trimmedReasonText = draft.reasonText.trim()
  if (trimmedReasonText.length === 0) {
    errors.reason_text = '原因说明不能为空；如需保持不变请沿用原值'
  } else if (trimmedReasonText.length > 2000) {
    errors.reason_text = '原因说明最长 2000 个字符'
  } else if (trimmedReasonText !== requested.reason_text) {
    changes.reason_text = trimmedReasonText
  }

  // 退款范围：仅退款提案允许；补偿提案永远不发送该字段
  if (actionType === 'refund' && draft.refundScope !== requested.refund_scope) {
    changes.refund_scope = draft.refundScope
  }

  const fieldErrorCount = Object.keys(errors).length
  const changedCount = Object.keys(changes).length
  if (fieldErrorCount > 0) {
    return { ok: false, changes: null, errors, formMessage: null }
  }
  if (changedCount === 0) {
    return {
      ok: false,
      changes: null,
      errors,
      formMessage: '修改内容未发生变化：金额、原因码、原因说明或退款范围至少修改一项',
    }
  }
  return { ok: true, changes, errors, formMessage: null }
}

/** 校验审批备注（三种决定共用）：去首尾空白后最长 1000 字符。 */
export function validateComment(comment: string): string | null {
  const trimmed = comment.trim()
  if (trimmed.length > 1000) {
    return '备注最长 1000 个字符'
  }
  return null
}
