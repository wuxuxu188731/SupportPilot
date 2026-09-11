<script setup lang="ts">
/*
 * 提案版本时间线：按版本号升序展示全部历史版本，并标出相邻版本的变化。
 *
 * 规则：
 *  - 版本 1 为初始版本（提案人创建），不做「修改」比较；
 *  - 从版本 2 起，与上一版本比较金额/原因码/原因说明/退款范围，
 *    发生变化时展示「前 → 后」，未变化的字段不标「发生修改」；
 *  - 当前生效版本以主色边框标记；
 *  - 未知原因码安全降级为原始值（displayText 兜底）。
 */
import { NTag, NText } from 'naive-ui'

import type { ActionTypeValue, VersionView } from '@/api/actionTypes'
import {
  COMPENSATION_REASON_TEXT,
  REFUND_REASON_TEXT,
  REFUND_SCOPE_TEXT,
  displayText,
} from '@/utils/actionDisplay'
import { formatAmountCents } from '@/utils/money'
import { formatDateTime } from '@/utils/time'

const props = defineProps<{
  /** 全部历史版本（按版本号升序） */
  versions: VersionView[]
  /** 当前生效版本 id（用于高亮标记） */
  currentVersionId: string | null
  /** 动作类型：决定原因码/退款范围的展示映射 */
  actionType: ActionTypeValue
}>()

/** 原因码中文文案（按动作类型选择映射）。 */
function reasonLabel(code: string): string {
  const map = props.actionType === 'refund' ? REFUND_REASON_TEXT : COMPENSATION_REASON_TEXT
  return displayText(map, code)
}

/** 金额文本（前 → 后 均在模板内用 formatAmountCents 展示）。 */
function amountText(version: VersionView): string {
  return formatAmountCents(version.amount_cents, version.currency)
}

/** 版本差异列表：与上一版本比较实际变化的字段（无变化则不标「发生修改」）。 */
function diffWithPrevious(index: number): { label: string; before: string; after: string }[] {
  const version = props.versions[index]
  const previous = props.versions[index - 1]
  if (!previous) return []
  const diff: { label: string; before: string; after: string }[] = []
  if (version.amount_cents !== previous.amount_cents) {
    diff.push({ label: '金额', before: amountText(previous), after: amountText(version) })
  }
  if (version.reason_code !== previous.reason_code) {
    diff.push({ label: '原因码', before: reasonLabel(previous.reason_code), after: reasonLabel(version.reason_code) })
  }
  if (version.reason_text !== previous.reason_text) {
    diff.push({ label: '原因说明', before: previous.reason_text, after: version.reason_text })
  }
  if (props.actionType === 'refund' && version.refund_scope !== previous.refund_scope) {
    diff.push({
      label: '退款范围',
      before: displayText(REFUND_SCOPE_TEXT, previous.refund_scope ?? null),
      after: displayText(REFUND_SCOPE_TEXT, version.refund_scope ?? null),
    })
  }
  return diff
}

/** 是否当前生效版本。 */
function isCurrent(version: VersionView): boolean {
  return props.currentVersionId !== null && version.version_id === props.currentVersionId
}
</script>

<template>
  <ol class="version-timeline" data-test="version-timeline">
    <li
      v-for="(version, index) in versions"
      :key="version.version_id"
      class="timeline-item"
      :class="{ 'is-current': isCurrent(version) }"
      :data-test="`version-item-${version.version_no}`"
    >
      <div class="item-head">
        <span class="version-no">V{{ version.version_no }}</span>
        <n-tag v-if="isCurrent(version)" size="small" type="primary" round>当前生效</n-tag>
        <n-tag v-else size="small" round type="default">历史版本</n-tag>
        <span class="item-time">{{ formatDateTime(version.created_at) }}</span>
      </div>

      <div class="item-body">
        <p class="version-amount">
          {{ amountText(version) }}
          <n-text depth="3" class="version-currency">{{ version.currency }}</n-text>
        </p>
        <dl class="version-meta">
          <div class="meta-row">
            <dt>原因</dt>
            <dd>{{ reasonLabel(version.reason_code) }}（{{ version.reason_code }}）</dd>
          </div>
          <div class="meta-row">
            <dt>说明</dt>
            <dd class="reason-text">{{ version.reason_text || '—' }}</dd>
          </div>
          <div v-if="actionType === 'refund'" class="meta-row">
            <dt>退款范围</dt>
            <dd>{{ displayText(REFUND_SCOPE_TEXT, version.refund_scope ?? null) }}</dd>
          </div>
          <div v-if="actionType === 'compensation'" class="meta-row">
            <dt>券有效期</dt>
            <dd>{{ version.coupon_valid_days !== null ? `${version.coupon_valid_days} 天` : '—' }}</dd>
          </div>
          <div class="meta-row">
            <dt>创建人</dt>
            <dd>{{ version.created_by_user_id || '—' }}</dd>
          </div>
        </dl>
      </div>

      <!-- 与上一版本的差异（版本 1 为初始版本，不做比较） -->
      <div v-if="diffWithPrevious(index).length > 0" class="version-diff" data-test="version-diff">
        <p class="diff-title">相对上一版本的修改</p>
        <div v-for="item in diffWithPrevious(index)" :key="item.label" class="diff-row">
          <span class="diff-label">{{ item.label }}</span>
          <span class="diff-change">{{ item.before }} → {{ item.after }}</span>
        </div>
      </div>
      <p v-else class="version-initial">
        {{ index === 0 ? '初始版本（提案创建）' : '与上一版本内容一致' }}
      </p>
    </li>
  </ol>
</template>

<style scoped>
.version-timeline {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-3);
}

.timeline-item {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-2);
  padding: var(--sp-space-3) var(--sp-space-4);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-md);
  background: var(--sp-color-bg-card);
}

.timeline-item.is-current {
  border-color: var(--sp-color-primary);
  background: var(--sp-color-primary-weak);
}

.item-head {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  flex-wrap: wrap;
}

.version-no {
  font-weight: 600;
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-1);
}

.item-time {
  margin-left: auto;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.item-body {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-space-4);
  flex-wrap: wrap;
}

.version-amount {
  margin: 0;
  font-size: var(--sp-font-size-lg);
  font-weight: 700;
  color: var(--sp-color-text-1);
}

.version-currency {
  font-size: var(--sp-font-size-sm);
  font-weight: 400;
  margin-left: var(--sp-space-1);
}

.version-meta {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-1);
  font-size: var(--sp-font-size-sm);
  flex: 1;
  min-width: 220px;
}

.meta-row {
  display: flex;
  gap: var(--sp-space-2);
}

.meta-row dt {
  color: var(--sp-color-text-3);
  flex-shrink: 0;
  min-width: 60px;
}

.meta-row dd {
  margin: 0;
  word-break: break-word;
}

.reason-text {
  white-space: pre-line;
}

.version-diff {
  padding-top: var(--sp-space-2);
  border-top: 1px dashed var(--sp-color-border);
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-1);
  font-size: var(--sp-font-size-sm);
}

.diff-title {
  margin: 0;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-warning);
}

.diff-row {
  display: flex;
  gap: var(--sp-space-2);
  flex-wrap: wrap;
}

.diff-label {
  color: var(--sp-color-text-3);
  flex-shrink: 0;
  min-width: 60px;
}

.diff-change {
  word-break: break-word;
}

.version-initial {
  margin: 0;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}
</style>
