<script setup lang="ts">
/*
 * Agent 处理过程时间线：把 events 渲染成默认折叠的「处理过程」区域。
 *
 * 安全说明：
 *  - 只展示适合用户理解的内容：事件类型、工具名、执行耗时与安全错误；
 *  - 默认不渲染工具原始参数（tool_call_arguments）与原始结果（result）
 *    JSON，避免把内部或业务敏感数据直接铺在界面上。
 */
import { computed, ref } from 'vue'

import type { AgentEvent } from '@/api/types'

const props = defineProps<{
  /** 本轮工具调用事件列表 */
  events: AgentEvent[]
}>()

/** 折叠区域是否展开（默认折叠，展开状态由用户切换）。 */
const expanded = ref(false)

/** 事件类型 → 中文展示文案。 */
const EVENT_TYPE_LABELS: Record<AgentEvent['type'], string> = {
  'tool_call.requested': '工具请求',
  'tool_call.started': '工具执行中',
  'tool_call.completed': '工具完成',
  'tool_call.failed': '工具失败',
  'citation.invalid': '引用校验失败',
}

/** 已知工具名 → 中文展示名（未知工具原样展示，不臆造）。 */
const TOOL_LABELS: Record<string, string> = {
  get_order: '查询订单',
  get_logistics: '查询物流',
  create_ticket: '创建工单',
  add_ticket_note: '添加备注',
  search_knowledge: '知识库检索',
  propose_refund: '提出退款提案',
  propose_compensation: '提出补偿提案',
  get_action_status: '查询动作状态',
  citation_validation: '引用校验',
}

/** 事件行展示信息：类型文案、工具名、是否失败/校验失败。 */
function describeEvent(event: AgentEvent): {
  label: string
  toolName: string
  isFailure: boolean
  summary: string
} {
  const label = EVENT_TYPE_LABELS[event.type] ?? event.type
  const toolName = TOOL_LABELS[event.tool_call_name] ?? event.tool_call_name
  const isFailure = event.type === 'tool_call.failed' || event.type === 'citation.invalid'
  const duration =
    typeof event.duration_ms === 'number' ? `${Math.round(event.duration_ms)} ms` : ''
  if (event.type === 'tool_call.completed') {
    return { label, toolName, isFailure, summary: duration ? `耗时 ${duration}` : '执行成功' }
  }
  if (event.type === 'tool_call.failed') {
    return { label, toolName, isFailure, summary: event.error ?? '执行失败' }
  }
  if (event.type === 'citation.invalid') {
    return { label, toolName, isFailure, summary: '引用验证未通过，回答被标记为可能不完整' }
  }
  return { label, toolName, isFailure, summary: '' }
}

/** 时间线展示数量（避免超长列表撑爆折叠区）。 */
const visibleEvents = computed(() => props.events.slice(0, 50))
const truncated = computed(() => props.events.length > 50)

/** 切换展开状态。 */
function toggleExpanded(): void {
  expanded.value = !expanded.value
}
</script>

<template>
  <div class="agent-timeline" data-test="agent-timeline">
    <div class="timeline-head">
      <button
        type="button"
        class="timeline-toggle"
        :aria-expanded="expanded"
        data-test="events-toggle"
        @click="toggleExpanded"
      >
        <span class="toggle-arrow" aria-hidden="true">{{ expanded ? '▾' : '▸' }}</span>
        处理过程（{{ events.length }} 个事件）
      </button>
    </div>

    <div v-if="expanded" class="timeline-body">
      <ol class="event-list">
        <li
          v-for="(item, index) in visibleEvents"
          :key="`${item.tool_call_id}-${index}`"
          class="event-item"
          :class="{ failure: describeEvent(item).isFailure }"
        >
          <span class="event-marker" aria-hidden="true">
            {{ describeEvent(item).isFailure ? '⚠' : '·' }}
          </span>
          <span class="event-main">
            <span class="event-label">{{ describeEvent(item).label }}</span>
            <span class="event-tool">{{ describeEvent(item).toolName }}</span>
            <span v-if="describeEvent(item).summary" class="event-summary">
              {{ describeEvent(item).summary }}
            </span>
          </span>
        </li>
      </ol>
      <p v-if="truncated" class="truncate-note">（仅展示前 50 个事件，其余已省略）</p>
      <p class="raw-note">
        工具参数与原始结果属于内部数据，此处不展示；如需排查失败原因请让管理员查看服务端记录。
      </p>
    </div>
  </div>
</template>

<style scoped>
.agent-timeline {
  margin-top: var(--sp-space-3);
}

.timeline-head {
  display: flex;
  align-items: center;
}

.timeline-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-space-1);
  padding: var(--sp-space-1) var(--sp-space-2);
  border: none;
  border-radius: var(--sp-radius-sm);
  background: transparent;
  color: var(--sp-color-text-2);
  font: inherit;
  font-size: var(--sp-font-size-sm);
  cursor: pointer;
}

.timeline-toggle:hover {
  background: var(--sp-color-bg-hover);
}

.timeline-toggle:focus-visible {
  outline: 2px solid var(--sp-color-primary);
  outline-offset: 1px;
}

.toggle-arrow {
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.timeline-body {
  padding: var(--sp-space-2) var(--sp-space-2) var(--sp-space-1);
}

.event-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 240px;
  overflow-y: auto;
}

.event-item {
  display: flex;
  align-items: baseline;
  gap: var(--sp-space-2);
  padding: 3px 0;
  font-size: var(--sp-font-size-sm);
}

.event-marker {
  color: var(--sp-color-text-3);
  flex-shrink: 0;
}

.event-item.failure .event-marker {
  color: var(--sp-color-error);
}

.event-main {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-space-2);
  min-width: 0;
}

.event-label {
  color: var(--sp-color-text-2);
}

.event-tool {
  font-weight: 600;
  color: var(--sp-color-text-1);
}

.event-item.failure .event-tool,
.event-item.failure .event-summary {
  color: var(--sp-color-error);
}

.event-summary {
  color: var(--sp-color-text-3);
  word-break: break-word;
}

.truncate-note {
  margin: var(--sp-space-2) 0 0;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.raw-note {
  margin: var(--sp-space-2) 0 0;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}
</style>
