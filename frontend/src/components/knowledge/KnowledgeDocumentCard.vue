<script setup lang="ts">
/*
 * 知识库文档卡片（列表项）：标题、状态、来源类型、当前有效版本与时间，
 * admin 的停用/启用入口由父级控制显隐，点击进入详情。
 *
 * 事实说明：
 *  - 仅展示接口真实字段，不伪造正文/下载/统计；
 *  - 停用/启用按钮在父级（store）确认后再执行，本组件只转发意图；
 *  - 长 ID 与哈希支持截断 + title 完整展示 + 点击复制。
 */
import { computed } from 'vue'
import { NButton, NTag, useMessage } from 'naive-ui'

import type { KnowledgeDocumentSummary } from '@/api/knowledgeTypes'
import {
  DOCUMENT_SOURCE_TYPE_TEXT,
  DOCUMENT_SOURCE_TYPE_TAG,
  DOCUMENT_STATUS_TEXT,
  DOCUMENT_STATUS_TAG,
  knowledgeDisplayText,
  knowledgeStatusTag,
} from '@/utils/knowledgeDisplay'
import { copyToClipboard } from '@/utils/clipboard'
import { formatDateTime } from '@/utils/time'

const props = defineProps<{
  /** 文档摘要 */
  item: KnowledgeDocumentSummary
  /** 当前用户是否 admin（仅界面展示；后端仍实时校验） */
  isAdmin: boolean
  /** 该文档是否正在执行停用/启用（防重复点击） */
  statusUpdating: boolean
}>()

const emit = defineEmits<{
  /** 用户点击进入详情 */
  open: [documentId: string]
  /** 用户点击「停用」（父级需先确认再调用 store） */
  disable: [documentId: string]
  /** 用户点击「启用」（父级需先确认再调用 store） */
  enable: [documentId: string]
}>()

const message = useMessage()

/** 状态中文文案（未知值安全降级）。 */
const statusText = computed(() => knowledgeDisplayText(DOCUMENT_STATUS_TEXT, props.item.status))
/** 状态标签色（未知值降级中性色）。 */
const statusTag = computed(() => knowledgeStatusTag(DOCUMENT_STATUS_TAG, props.item.status))
/** 来源类型中文文案。 */
const sourceTypeText = computed(() =>
  knowledgeDisplayText(DOCUMENT_SOURCE_TYPE_TEXT, props.item.source_type),
)
/** 来源类型标签色。 */
const sourceTypeTag = computed(() =>
  knowledgeStatusTag(DOCUMENT_SOURCE_TYPE_TAG, props.item.source_type),
)
/** 是否展示「停用」：admin + 已启用文档。 */
const canDisable = computed(() => props.isAdmin && props.item.status === 'active')
/** 是否展示「启用」：admin + 已停用文档（processing/failed 不提供误导性入口）。 */
const canEnable = computed(() => props.isAdmin && props.item.status === 'disabled')

/** 长 ID 截断展示（中间省略，保留首尾）。 */
function shortenId(id: string | null): string {
  if (!id) return '—'
  if (id.length <= 14) return id
  return `${id.slice(0, 8)}…${id.slice(-4)}`
}

/** 点击复制（成功提示，失败静默）。 */
async function onCopy(value: string | null): Promise<void> {
  if (!value) return
  const ok = await copyToClipboard(value)
  if (ok) message.success('已复制')
}
</script>

<template>
  <article class="doc-card" data-test="document-card" @click="emit('open', item.document_id)">
    <div class="doc-main">
      <div class="doc-title-row">
        <span class="doc-title" :title="item.title">{{ item.title }}</span>
        <n-tag :type="statusTag" size="small" class="doc-tag" data-test="doc-status-tag">
          {{ statusText }}
        </n-tag>
        <n-tag :type="sourceTypeTag" size="small" class="doc-tag" data-test="doc-source-tag">
          {{ sourceTypeText }}
        </n-tag>
      </div>

      <dl class="doc-meta">
        <div class="meta-item">
          <dt>文档 ID</dt>
          <dd>
            <button type="button" class="id-link" :title="item.document_id" data-test="doc-id" @click.stop="onCopy(item.document_id)">
              {{ shortenId(item.document_id) }}
            </button>
          </dd>
        </div>
        <div class="meta-item">
          <dt>有效版本</dt>
          <dd>
            <button type="button" class="id-link" :title="item.active_version_id ?? ''" @click.stop="onCopy(item.active_version_id)">
              {{ shortenId(item.active_version_id) }}
            </button>
          </dd>
        </div>
        <div class="meta-item">
          <dt>创建时间</dt>
          <dd>{{ formatDateTime(item.created_at) }}</dd>
        </div>
        <div class="meta-item">
          <dt>更新时间</dt>
          <dd>{{ formatDateTime(item.updated_at) }}</dd>
        </div>
      </dl>
    </div>

    <div class="doc-actions" @click.stop>
      <n-button
        v-if="canDisable"
        size="small"
        quaternary
        type="warning"
        :loading="statusUpdating"
        data-test="disable-document"
        @click="emit('disable', item.document_id)"
      >
        停用
      </n-button>
      <n-button
        v-if="canEnable"
        size="small"
        quaternary
        type="primary"
        :loading="statusUpdating"
        data-test="enable-document"
        @click="emit('enable', item.document_id)"
      >
        启用
      </n-button>
    </div>
  </article>
</template>

<style scoped>
.doc-card {
  display: flex;
  align-items: stretch;
  justify-content: space-between;
  gap: var(--sp-space-3);
  padding: var(--sp-space-4);
  background: var(--sp-color-bg-card);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-md);
  cursor: pointer;
  transition: border-color 0.15s ease;
}

.doc-card:hover {
  border-color: var(--sp-color-primary);
}

.doc-main {
  min-width: 0;
  flex: 1;
}

.doc-title-row {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  flex-wrap: wrap;
}

.doc-title {
  font-size: var(--sp-font-size-md);
  font-weight: 600;
  color: var(--sp-color-text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

.doc-tag {
  flex-shrink: 0;
}

.doc-meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: var(--sp-space-2) var(--sp-space-4);
  margin: var(--sp-space-3) 0 0;
}

.meta-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.meta-item dt {
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.meta-item dd {
  margin: 0;
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-2);
  min-width: 0;
  overflow-wrap: anywhere;
}

.id-link {
  border: none;
  background: none;
  padding: 0;
  font: inherit;
  color: var(--sp-color-text-2);
  cursor: pointer;
  text-decoration: underline;
  text-decoration-style: dotted;
  text-underline-offset: 2px;
}

.id-link:hover {
  color: var(--sp-color-primary);
}

.doc-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  flex-shrink: 0;
}
</style>
