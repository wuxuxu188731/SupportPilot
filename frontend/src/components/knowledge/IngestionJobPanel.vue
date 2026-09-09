<script setup lang="ts">
/*
 * 最近入库任务面板：展示有效版本最近一次入库任务的状态详情。
 *
 * 事实说明：
 *  - 当前后端没有任务列表接口，这里只展示详情接口提供的
 *    latest_job（有效版本最近一次任务）；
 *  - 没有任务时显示明确空状态，不伪造任务历史；
 *  - 所有可空字段显示「—」，不显示 null/undefined；
 *  - 错误只展示服务端安全 error_code / error_message，不展示堆栈。
 */
import { computed } from 'vue'
import { NTag } from 'naive-ui'

import type { IngestionJobInfo } from '@/api/knowledgeTypes'
import {
  INGESTION_STATUS_TEXT,
  INGESTION_STATUS_TAG,
  knowledgeDisplayText,
  knowledgeStatusTag,
} from '@/utils/knowledgeDisplay'
import { formatDateTime } from '@/utils/time'

const props = defineProps<{
  /** 最近入库任务；null 表示尚无任务记录 */
  job: IngestionJobInfo | null
  /** 任务查询中（轮询/手动） */
  loading: boolean
}>()

/** 任务状态中文文案。 */
const statusText = computed(() => knowledgeDisplayText(INGESTION_STATUS_TEXT, props.job?.status))
/** 任务状态标签色。 */
const statusTag = computed(() => knowledgeStatusTag(INGESTION_STATUS_TAG, props.job?.status ?? ''))

/** 是否展示错误区块（failed/存在错误码）。 */
const hasError = computed(() => Boolean(props.job?.error_code || props.job?.error_message))

/** 展示辅助：空值 → 「—」。 */
function display(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return String(value)
}
</script>

<template>
  <section class="job-panel" data-test="ingestion-job-panel">
    <h3 class="panel-title">最近入库任务</h3>

    <p v-if="loading" class="panel-note" data-test="job-loading">正在查询任务状态…</p>

    <div v-else-if="!job" class="panel-note" data-test="job-empty">
      暂无入库任务记录。只有有效版本最近一次任务会展示（后端没有任务列表接口）。
    </div>

    <dl v-else class="job-meta" data-test="job-meta">
      <div class="meta-item meta-wide">
        <dt>任务 ID</dt>
        <dd>{{ display(job.job_id) }}</dd>
      </div>
      <div class="meta-item">
        <dt>文档 ID</dt>
        <dd>{{ display(job.document_id) }}</dd>
      </div>
      <div class="meta-item">
        <dt>版本 ID</dt>
        <dd>{{ display(job.version_id) }}</dd>
      </div>
      <div class="meta-item">
        <dt>状态</dt>
        <dd>
          <n-tag :type="statusTag" size="small" data-test="job-status-tag">{{ statusText }}</n-tag>
        </dd>
      </div>
      <div class="meta-item">
        <dt>尝试次数</dt>
        <dd>{{ display(job.attempt_count) }}</dd>
      </div>
      <div class="meta-item">
        <dt>创建时间</dt>
        <dd>{{ formatDateTime(job.created_at) }}</dd>
      </div>
      <div class="meta-item">
        <dt>开始时间</dt>
        <dd>{{ formatDateTime(job.started_at) }}</dd>
      </div>
      <div class="meta-item">
        <dt>结束时间</dt>
        <dd>{{ formatDateTime(job.finished_at) }}</dd>
      </div>
      <div v-if="hasError" class="meta-item meta-wide">
        <dt>错误信息（服务端安全错误码）</dt>
        <dd>
          <code class="error-code" data-test="job-error">{{ display(job.error_code) }}</code>
          <span class="error-message">{{ display(job.error_message) }}</span>
        </dd>
      </div>
    </dl>
  </section>
</template>

<style scoped>
.job-panel {
  background: var(--sp-color-bg-card);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-md);
  padding: var(--sp-space-4);
}

.panel-title {
  margin: 0 0 var(--sp-space-3);
  font-size: var(--sp-font-size-md);
  font-weight: 600;
}

.panel-note {
  margin: 0;
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.job-meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--sp-space-2) var(--sp-space-4);
  margin: 0;
}

.meta-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.meta-wide {
  grid-column: span 2;
}

@media (max-width: 640px) {
  .meta-wide {
    grid-column: span 1;
  }
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

.error-code {
  display: inline-block;
  padding: 1px 6px;
  border-radius: var(--sp-radius-sm);
  background: var(--sp-color-bg-active);
  color: var(--sp-color-danger);
  font-size: var(--sp-font-size-xs);
  margin-right: var(--sp-space-2);
}

.error-message {
  display: inline-block;
}
</style>
