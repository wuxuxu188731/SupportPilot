<script setup lang="ts">
/*
 * 审批决定表单（仅 admin + 待审批 + 尚无决定时展示）：
 * 批准 / 修改后批准 / 拒绝 三种操作，提交前必须经过不可逆确认。
 *
 * 约束与事实（与后端契约对齐）：
 *  - 直接批准与拒绝：changes 必须为 null，绝不携带修改内容；
 *  - 修改后批准：以请求版本为初始值，只提交真正变化的字段；
 *    至少一个字段发生变化才可提交；金额以「元」输入、安全转「分」；
 *    refund_scope 仅退款提案可发送，补偿提案禁止；
 *  - 备注去首尾空白后最长 1000 字符；前端校验不能替代后端校验，
 *    后端 422 字段错误由父级映射回本表单（serverFieldErrors）；
 *  - 确认对话框展示决定/金额/币种/修改字段/备注与模拟执行说明，
 *    确认前不发起任何请求；提交期间按钮 loading、禁止重复点击；
 *  - 决定一旦落库不可修改或删除，界面必须如实说明。
 */
import { computed, ref, watch } from 'vue'
import { NAlert, NButton, NModal } from 'naive-ui'

import type {
  ActionTypeValue,
  ApprovalDecisionTypeValue,
  DecisionChangesPayload,
  RefundScopeValue,
  VersionView,
} from '@/api/actionTypes'
import {
  DECISION_TYPE_TEXT,
  REFUND_SCOPE_TEXT,
  REFUND_REASON_TEXT,
  COMPENSATION_REASON_TEXT,
  displayText,
  reasonCodeOptions,
} from '@/utils/actionDisplay'
import { validateApprovedWithChanges, validateComment } from '@/utils/actionForm'
import { formatAmountCents, formatCentsToYuanInput } from '@/utils/money'

/** 决定类型选项（三种操作）。 */
const DECISION_MODES: { value: ApprovalDecisionTypeValue; label: string; description: string }[] = [
  { value: 'approved', label: '批准', description: '按请求版本执行退款/补偿' },
  { value: 'approved_with_changes', label: '修改后批准', description: '修改金额/原因/范围后执行' },
  { value: 'rejected', label: '拒绝', description: '终止该提案，不产生任何执行' },
]

const props = defineProps<{
  /** 请求审批的版本（修改后批准表单的初始值） */
  requestedVersion: VersionView
  /** 动作类型：决定 refund_scope 是否允许与原因码枚举 */
  actionType: ActionTypeValue
  /** 决定提交中（父级来自 store.deciding，按钮显示 loading 并防重复） */
  submitting: boolean
  /** 后端 422 字段错误映射（字段名 → 提示），提交后由父级回传 */
  serverFieldErrors: Record<string, string>
  /** 后端稳定错误消息（如 APPROVAL_INVALID_CHANGES 的 message） */
  serverMessage: string | null
}>()

const emit = defineEmits<{
  /** 用户已在确认对话框确认：抛出最终请求载荷（由父级调用 store.decide） */
  submit: [payload: { decision: ApprovalDecisionTypeValue; changes: DecisionChangesPayload | null; comment: string | null }]
}>()

/** 当前选中的决定类型。 */
const mode = ref<ApprovalDecisionTypeValue>('approved')

/** 金额输入（元，字符串）：以请求版本为初始值。 */
const amountText = ref(formatCentsToYuanInput(props.requestedVersion.amount_cents))
/** 原因码（英文枚举）：以请求版本为初始值。 */
const reasonCode = ref(props.requestedVersion.reason_code)
/** 原因说明：以请求版本为初始值。 */
const reasonText = ref(props.requestedVersion.reason_text)
/** 退款范围：以请求版本为初始值（补偿提案不展示该字段）。 */
const refundScopeChecked = ref<RefundScopeValue>(props.requestedVersion.refund_scope ?? 'partial')
/** 审批备注（三种决定共用）。 */
const comment = ref('')

/** 客户端校验产生的字段错误（服务端错误另行合并展示）。 */
const localErrors = ref<Record<string, string>>({})
/** 校验/提交产生的表单级错误。 */
const formError = ref<string | null>(null)
/** 确认对话框显隐。 */
const showConfirm = ref(false)
/** 待确认的请求载荷（用户确认前不发起任何请求）。 */
const pendingPayload = ref<{ decision: ApprovalDecisionTypeValue; changes: DecisionChangesPayload | null; comment: string | null } | null>(null)

/** 是否退款提案（决定 refund_scope 是否可编辑）。 */
const isRefund = computed(() => props.actionType === 'refund')

/** 原因码下拉选项（英文值 + 中文文案）。 */
const reasonOptions = computed(() => reasonCodeOptions(props.actionType))

/** 展示用原因码文案。 */
function reasonCodeLabel(code: string): string {
  const map = props.actionType === 'refund' ? REFUND_REASON_TEXT : COMPENSATION_REASON_TEXT
  return displayText(map, code)
}

/** 字段级错误（本地校验 + 服务端 422 合并；服务端错误不清空用户输入）。 */
const fieldErrors = computed(() => ({ ...props.serverFieldErrors, ...localErrors.value }))

/** 金额字段实际值（分）：用于确认摘要展示。 */
function effectiveAmountCents(): number {
  if (mode.value !== 'approved_with_changes' || !pendingPayload.value?.changes?.amount_cents) {
    return props.requestedVersion.amount_cents
  }
  return pendingPayload.value.changes.amount_cents
}

/** 确认摘要：修改过的字段文案（金额/原因码/原因说明/退款范围）。 */
function changedFieldSummary(): string[] {
  const changes = pendingPayload.value?.changes ?? {}
  const lines: string[] = []
  if (changes.amount_cents !== undefined) {
    lines.push(`金额：${formatAmountCents(props.requestedVersion.amount_cents, props.requestedVersion.currency)} → ${formatAmountCents(changes.amount_cents, props.requestedVersion.currency)}`)
  }
  if (changes.reason_code !== undefined) {
    lines.push(`原因码：${reasonCodeLabel(props.requestedVersion.reason_code)} → ${reasonCodeLabel(changes.reason_code)}`)
  }
  if (changes.reason_text !== undefined) {
    lines.push('原因说明已修改')
  }
  if (changes.refund_scope !== undefined && isRefund.value) {
    lines.push(
      `退款范围：${displayText(REFUND_SCOPE_TEXT, props.requestedVersion.refund_scope ?? null)} → ${displayText(REFUND_SCOPE_TEXT, changes.refund_scope ?? null)}`,
    )
  }
  return lines
}

/** 按当前模式构造请求载荷并做客户端校验；通过后打开确认对话框。 */
function requestSubmit(): void {
  localErrors.value = {}
  formError.value = null
  const commentError = validateComment(comment.value)
  if (commentError) {
    localErrors.value = { comment: commentError }
    formError.value = commentError
    return
  }
  const trimmedComment = comment.value.trim() || null

  if (mode.value === 'approved_with_changes') {
    const validation = validateApprovedWithChanges(
      props.requestedVersion,
      {
        amountText: amountText.value,
        reasonCode: reasonCode.value,
        reasonText: reasonText.value,
        refundScope: refundScopeChecked.value,
      },
      props.actionType,
    )
    if (!validation.ok) {
      localErrors.value = validation.errors
      formError.value = validation.formMessage
      return
    }
    pendingPayload.value = {
      decision: 'approved_with_changes',
      changes: validation.changes,
      comment: trimmedComment,
    }
  } else {
    // 直接批准/拒绝：changes 必须为 null，不携带任何修改内容
    pendingPayload.value = {
      decision: mode.value,
      changes: null,
      comment: trimmedComment,
    }
  }
  showConfirm.value = true
}

/** 用户在确认对话框点击「确认提交」：只在此刻发出请求。 */
function confirmSubmit(): void {
  if (props.submitting || !pendingPayload.value) return // 防重复提交兜底
  emit('submit', pendingPayload.value)
}

/** 请求结束后（submitting 由 true 变 false）自动关闭确认框。 */
watch(
  () => props.submitting,
  (submitting, was) => {
    if (was && !submitting) {
      showConfirm.value = false
    }
  },
)

/** 决定类型切换：清空该次校验错误（不改变已输入数据）。 */
watch(mode, () => {
  localErrors.value = {}
  formError.value = null
})
</script>

<template>
  <div class="decision-form" data-test="decision-form">
    <div class="form-head">
      <h3 class="form-title">作出审批决定</h3>
      <n-alert type="warning" :show-icon="true" class="form-warning">
        决定一旦提交不可修改或删除；退款/补偿均为模拟业务记录，不会产生真实资金到账或真实发券。
      </n-alert>
      <n-alert v-if="formError" type="error" :show-icon="true" class="form-error" data-test="decision-form-error">
        {{ formError }}
      </n-alert>
    </div>

    <!-- 三种决定方式（原生 radio：受控、键盘可达、可测试） -->
    <div class="mode-group" role="radiogroup" aria-label="决定类型">
      <label
        v-for="option in DECISION_MODES"
        :key="option.value"
        class="mode-option"
      >
        <input
          v-model="mode"
          type="radio"
          name="decision-mode"
          class="mode-radio"
          :value="option.value"
          :data-test="`mode-${option.value}`"
        >
        <span class="mode-text">
          <span class="mode-label">{{ option.label }}</span>
          <span class="mode-description">{{ option.description }}</span>
        </span>
      </label>
    </div>

    <!-- 修改后批准：以请求版本为初始值的编辑区 -->
    <fieldset v-if="mode === 'approved_with_changes'" class="changes-fields" data-test="changes-fields">
      <legend class="fieldset-legend">修改内容（至少修改一项，未修改的字段沿用请求版本）</legend>

      <div class="field-row">
        <label class="field-label" for="decision-amount">金额（元）</label>
        <input
          id="decision-amount"
          v-model="amountText"
          class="field-input"
          type="text"
          inputmode="decimal"
          data-test="amount-input"
        >
        <span class="field-hint">最大两位小数；当前请求版本 {{ formatAmountCents(requestedVersion.amount_cents, requestedVersion.currency) }}</span>
        <p v-if="fieldErrors.amount_cents" class="field-error" data-test="amount-error">{{ fieldErrors.amount_cents }}</p>
      </div>

      <div class="field-row">
        <label class="field-label" for="decision-reason-code">原因码</label>
        <select
          id="decision-reason-code"
          v-model="reasonCode"
          class="field-input"
          data-test="reason-code-select"
        >
          <option v-for="option in reasonOptions" :key="option.value" :value="option.value">
            {{ option.label }}（{{ option.value }}）
          </option>
        </select>
        <p v-if="fieldErrors.reason_code" class="field-error" data-test="reason-code-error">{{ fieldErrors.reason_code }}</p>
      </div>

      <div class="field-row">
        <label class="field-label" for="decision-reason-text">原因说明</label>
        <textarea
          id="decision-reason-text"
          v-model="reasonText"
          class="field-input field-textarea"
          rows="3"
          data-test="reason-text-input"
        />
        <p v-if="fieldErrors.reason_text" class="field-error" data-test="reason-text-error">{{ fieldErrors.reason_text }}</p>
      </div>

      <div v-if="isRefund" class="field-row">
        <span class="field-label">退款范围</span>
        <div class="scope-group" role="radiogroup" aria-label="退款范围" data-test="refund-scope-group">
          <label v-for="scope in (['full', 'partial'] as const)" :key="scope" class="scope-option">
            <input v-model="refundScopeChecked" type="radio" name="refund-scope" class="mode-radio" :value="scope">
            <span>{{ REFUND_SCOPE_TEXT[scope] }}（{{ scope === 'full' ? '全额退款' : '部分退款' }}）</span>
          </label>
        </div>
        <p v-if="fieldErrors.refund_scope" class="field-error">{{ fieldErrors.refund_scope }}</p>
      </div>
    </fieldset>

    <div class="field-row comment-row">
      <label class="field-label" for="decision-comment">备注</label>
      <textarea
        id="decision-comment"
        v-model="comment"
        class="field-input field-textarea"
        rows="2"
        placeholder="可选，最长 1000 字符"
        data-test="comment-input"
      />
      <p v-if="fieldErrors.comment" class="field-error" data-test="comment-error">{{ fieldErrors.comment }}</p>
    </div>

    <div class="form-actions">
      <n-button
        type="primary"
        :loading="submitting"
        :disabled="submitting"
        data-test="submit-decision"
        @click="requestSubmit"
      >
        提交决定
      </n-button>
    </div>

    <!-- 不可逆确认对话框：确认前不发任何请求 -->
    <n-modal
      v-model:show="showConfirm"
      preset="card"
      :title="`确认${pendingPayload ? DECISION_TYPE_TEXT[pendingPayload.decision] : ''}（不可逆）`"
      :mask-closable="!submitting"
      :closable="!submitting"
      data-test="decision-confirm-modal"
    >
      <div class="confirm-body" data-test="decision-confirm-content">
        <dl class="confirm-summary">
          <div class="summary-row">
            <dt>决定</dt>
            <dd>{{ pendingPayload ? DECISION_TYPE_TEXT[pendingPayload.decision] : '' }}</dd>
          </div>
          <div class="summary-row">
            <dt>金额</dt>
            <dd>
              {{ formatAmountCents(effectiveAmountCents(), requestedVersion.currency) }}
              <span class="confirm-currency">{{ requestedVersion.currency }}</span>
            </dd>
          </div>
          <div v-if="mode === 'approved_with_changes'" class="summary-row">
            <dt>修改字段</dt>
            <dd>
              <ul class="confirm-changes" data-test="confirm-changes">
                <li v-for="line in changedFieldSummary()" :key="line">{{ line }}</li>
              </ul>
            </dd>
          </div>
          <div v-if="pendingPayload?.comment" class="summary-row">
            <dt>备注</dt>
            <dd>{{ pendingPayload.comment }}</dd>
          </div>
        </dl>
        <n-alert type="warning" :show-icon="true">
          提交后将执行模拟退款或优惠券补偿（业务记录写入数据库，不代表真实资金到账或真实发券）；
          决定落库后不可修改或删除。
        </n-alert>
      </div>
      <template #footer>
        <div class="confirm-actions">
          <n-button :disabled="submitting" data-test="cancel-decision" @click="showConfirm = false">
            取消
          </n-button>
          <n-button
            type="primary"
            :loading="submitting"
            :disabled="submitting"
            data-test="confirm-decision"
            @click="confirmSubmit"
          >
            确认提交
          </n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.decision-form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-4);
}

.form-head {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-2);
}

.form-title {
  margin: 0;
  font-size: var(--sp-font-size-lg);
  font-weight: 600;
}

.form-warning {
  max-width: 720px;
}

.form-error {
  max-width: 720px;
}

.mode-group {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--sp-space-3);
}

.mode-option {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-space-2);
  padding: var(--sp-space-3);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-md);
  cursor: pointer;
  background: var(--sp-color-bg-page);
}

.mode-option:has(.mode-radio:checked) {
  border-color: var(--sp-color-primary);
  background: var(--sp-color-primary-weak);
}

.mode-radio {
  accent-color: var(--sp-color-primary);
  margin: 3px 0 0;
  flex-shrink: 0;
}

.mode-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.mode-label {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-1);
}

.mode-description {
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.scope-group {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-2);
  font-size: var(--sp-font-size-sm);
}

.scope-option {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  cursor: pointer;
}

.scope-option .mode-radio {
  margin: 0;
}

.changes-fields {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-3);
  padding: var(--sp-space-4);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-lg);
  background: var(--sp-color-bg-page);
}

.fieldset-legend {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-2);
  padding: 0 var(--sp-space-2);
}

.field-row {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-1);
}

.field-label {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-2);
}

.field-input {
  padding: var(--sp-space-2) var(--sp-space-3);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-sm);
  background: var(--sp-color-bg-card);
  color: var(--sp-color-text-1);
  font-size: var(--sp-font-size-base);
  max-width: 480px;
  font-family: inherit;
}

.field-input:focus-visible {
  outline: 2px solid var(--sp-color-primary);
  outline-offset: 1px;
}

.field-textarea {
  width: 100%;
  resize: vertical;
}

.field-hint {
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.field-error {
  margin: 0;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-error);
}

.comment-row {
  max-width: 720px;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
}

.confirm-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-3);
}

.confirm-summary {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-2);
  font-size: var(--sp-font-size-sm);
}

.summary-row {
  display: flex;
  gap: var(--sp-space-3);
}

.summary-row dt {
  color: var(--sp-color-text-3);
  flex-shrink: 0;
  min-width: 64px;
}

.summary-row dd {
  margin: 0;
}

.confirm-currency {
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
  margin-left: var(--sp-space-1);
}

.confirm-changes {
  margin: 0;
  padding-left: var(--sp-space-4);
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-1);
}

.confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-space-3);
}
</style>
