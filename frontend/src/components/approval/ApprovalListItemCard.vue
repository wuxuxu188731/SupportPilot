<script setup lang="ts">
/*
 * 审批列表项卡片：展示动作类型、金额、审批/Run 状态、订单与版本摘要。
 *
 * 事实与范围说明：
 *  - 只使用后端 ApprovalListItemResponse 结构化字段，不伪造用户名/订单名；
 *  - 列表接口不返回总数，本组件不展示总数；
 *  - 整卡可点击（键盘可达）进入审批详情；「查看详情」是明确入口。
 */
import { NButton, NTag, NText } from 'naive-ui'

import type { ApprovalListItem } from '@/api/actionTypes'
import {
  ACTION_TYPE_TEXT,
  APPROVAL_STATUS_TAG,
  APPROVAL_STATUS_TEXT,
  DECISION_TYPE_TEXT,
  displayText,
  RUN_STATUS_TAG,
  RUN_STATUS_TEXT,
  statusTagType,
} from '@/utils/actionDisplay'
import { formatAmountCents } from '@/utils/money'
import { formatMessageTime } from '@/utils/time'

const props = defineProps<{
  /** 审批列表项（结构化数据） */
  item: ApprovalListItem
}>()

const emit = defineEmits<{
  /** 点击卡片或「查看详情」：跳转审批详情路由 */
  open: [approvalId: string]
}>()

/** 动作类型中文标签。 */
const actionLabel = () => displayText(ACTION_TYPE_TEXT, props.item.action_type)

/** 审批状态中文标签。 */
const approvalStatusLabel = () => displayText(APPROVAL_STATUS_TEXT, props.item.approval_status)

/** 审批状态标签色。 */
const approvalStatusType = () => statusTagType(APPROVAL_STATUS_TAG, props.item.approval_status)

/** Run 状态中文标签。 */
const runStatusLabel = () => displayText(RUN_STATUS_TEXT, props.item.run_status)

/** Run 状态标签色。 */
const runStatusType = () => statusTagType(RUN_STATUS_TAG, props.item.run_status)

/** 金额文本：当前版本金额按分格式化；尚无版本时显示 —。 */
const amountText = () =>
  props.item.current_version
    ? formatAmountCents(props.item.current_version.amount_cents, props.item.current_version.currency)
    : '—'

/** 币种：当前版本币种；无版本时为 —。 */
const currencyText = () => props.item.current_version?.currency ?? '—'

/** 当前版本号文本：无版本时为 —。 */
const versionNoText = () => (props.item.current_version ? `V${props.item.current_version.version_no}` : '—')

/** 已有决定摘要：决定类型 + 备注；未决定时不展示。 */
function decisionSummary(): string | null {
  const decision = props.item.decision
  if (!decision) return null
  const label = displayText(DECISION_TYPE_TEXT, decision.decision)
  return decision.comment ? `${label}：${decision.comment}` : label
}

/** 审批创建时间文本。 */
const createdText = () => formatMessageTime(props.item.created_at)

/** 打开审批详情（卡片点击与键盘触发共用）。 */
function openDetail(): void {
  emit('open', props.item.approval_id)
}
</script>

<template>
  <article
    class="approval-item"
    role="link"
    tabindex="0"
    data-test="approval-item"
    :aria-label="`查看审批 ${item.approval_id} 详情`"
    @click="openDetail"
    @keydown.enter="openDetail"
    @keydown.space.prevent="openDetail"
  >
    <div class="item-head">
      <div class="item-tags">
        <n-tag size="small" round :type="item.action_type === 'refund' ? 'warning' : 'info'">
          {{ actionLabel() }}
        </n-tag>
        <n-tag size="small" round :type="approvalStatusType()">
          {{ approvalStatusLabel() }}
        </n-tag>
        <n-tag size="small" round :type="runStatusType()">
          {{ runStatusLabel() }}
        </n-tag>
      </div>
      <span class="item-time" data-test="approval-created-at">{{ createdText() }}</span>
    </div>

    <div class="item-body">
      <p class="item-amount" data-test="approval-amount">
        {{ amountText() }}
        <n-text depth="3" class="item-currency">{{ currencyText() }}</n-text>
      </p>
      <dl class="item-meta">
        <div class="meta-row">
          <dt>订单</dt>
          <dd><code class="meta-code" :title="item.order_id">{{ item.order_id }}</code></dd>
        </div>
        <div class="meta-row">
          <dt>审批 ID</dt>
          <dd><code class="meta-code" :title="item.approval_id">{{ item.approval_id }}</code></dd>
        </div>
        <div class="meta-row">
          <dt>版本</dt>
          <dd data-test="approval-version">{{ versionNoText() }} · 共 {{ item.version_count }} 个版本</dd>
        </div>
      </dl>
    </div>

    <div class="item-foot">
      <n-text v-if="decisionSummary()" depth="2" class="item-decision" data-test="approval-decision">
        {{ decisionSummary() }}
      </n-text>
      <n-text v-else depth="3" class="item-decision">等待管理员作出决定</n-text>
      <n-button size="small" secondary type="primary" data-test="approval-detail-link" @click.stop="openDetail">
        查看详情
      </n-button>
    </div>
  </article>
</template>

<style scoped>
.approval-item {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-3);
  padding: var(--sp-space-4);
  background: var(--sp-color-bg-card);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-lg);
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.approval-item:hover,
.approval-item:focus-visible {
  border-color: var(--sp-color-primary);
  box-shadow: var(--sp-shadow-card);
}

.item-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-2);
  flex-wrap: wrap;
}

.item-tags {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  flex-wrap: wrap;
}

.item-time {
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.item-body {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-space-4);
  flex-wrap: wrap;
}

.item-amount {
  margin: 0;
  font-size: var(--sp-font-size-xl);
  font-weight: 700;
  color: var(--sp-color-text-1);
}

.item-currency {
  font-size: var(--sp-font-size-sm);
  font-weight: 400;
  margin-left: var(--sp-space-1);
}

.item-meta {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-1);
  font-size: var(--sp-font-size-sm);
}

.meta-row {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
}

.meta-row dt {
  color: var(--sp-color-text-3);
  flex-shrink: 0;
  min-width: 52px;
}

.meta-row dd {
  margin: 0;
  display: flex;
  align-items: center;
  min-width: 0;
}

.meta-code {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 240px;
  background: var(--sp-color-bg-hover);
  border-radius: var(--sp-radius-sm);
  padding: 1px var(--sp-space-1);
  font-size: var(--sp-font-size-xs);
}

.item-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-3);
  flex-wrap: wrap;
  padding-top: var(--sp-space-2);
  border-top: 1px dashed var(--sp-color-border);
}

.item-decision {
  font-size: var(--sp-font-size-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
