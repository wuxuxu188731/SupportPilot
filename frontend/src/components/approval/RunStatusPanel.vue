<script setup lang="ts">
/*
 * Run 状态面板：审批详情里的完整执行状态 + 恢复入口。
 *
 * 事实与范围说明：
 *  - 数据来自 GET /action-runs/{run_id}/（成员可读），字段全部结构化展示；
 *  - 可空字段一律显示「—」或明确空态，不显示字符串 "null"/"undefined"；
 *  - 恢复按钮条件集中为 canResumeRun 派生（store.canResume），模板不散落逻辑；
 *  - 恢复失败（503）时保留决定/版本/审批事实，按钮重新可用允许稍后重试。
 */
import { NAlert, NButton, NSpin, NTag, NText } from 'naive-ui'

import type { RunStatusResponse } from '@/api/actionTypes'
import {
  EXECUTION_STATUS_TEXT,
  RUN_STATUS_TAG,
  RUN_STATUS_TEXT,
  WORKFLOW_TYPE_TEXT,
  displayText,
  statusTagType,
} from '@/utils/actionDisplay'

const props = defineProps<{
  /** Run 完整状态（GET /action-runs/{run_id}/ 响应） */
  run: RunStatusResponse | null
  /** Run 状态是否加载中 */
  loading: boolean
  /** Run 状态加载错误消息；null 表示无错误 */
  error: string | null
  /** 恢复请求进行中（防重复与按钮 loading） */
  resuming: boolean
  /** 是否展示恢复按钮（store.canResume 派生） */
  canResume: boolean
  /** 最近一次决定是否明确要求恢复（202 语义） */
  resumeRequired: boolean
  /** 决定后自动恢复失败的稳定错误码 */
  resumeErrorCode: string | null
}>()

const emit = defineEmits<{
  /** 点击「恢复执行」：由父级调用 store.resumeRun */
  resume: []
}>()

/** 空态文本：字段无数据时不显示字符串 "null"。 */
const EMPTY = '—'

/** 是否可重试（Run 或执行层任一可重试即显示提示）。 */
const retryableState = () =>
  props.run ? props.run.run.last_error_retryable || (props.run.execution?.error_retryable ?? false) : false
</script>

<template>
  <div class="run-panel" data-test="run-panel">
    <!-- 加载中 -->
    <div v-if="loading && !run" class="panel-state" aria-live="polite">
      <n-spin size="small" />
      <span>正在加载执行状态…</span>
    </div>

    <!-- 加载失败 -->
    <div v-else-if="error" class="panel-state" role="alert">
      <NText type="error">{{ error }}</NText>
    </div>

    <!-- 尚无数据 -->
    <div v-else-if="!run" class="panel-state">
      <NText depth="3">暂无执行状态</NText>
    </div>

    <template v-else>
      <!-- 决定已保存但恢复未完成（202）提示 -->
      <n-alert v-if="resumeRequired" type="warning" :show-icon="true" class="resume-alert">
        工作流尚未恢复完成（{{ resumeErrorCode ?? '未知错误' }}），可以稍后恢复执行。
      </n-alert>

      <div class="run-head">
        <h4 class="panel-title">执行状态</h4>
        <span class="run-status">
          <n-tag size="small" round :type="statusTagType(RUN_STATUS_TAG, run.run.status)">
            {{ displayText(RUN_STATUS_TEXT, run.run.status) }}
          </n-tag>
        </span>
      </div>

      <dl class="run-meta">
        <div class="meta-row">
          <dt>工作流类型</dt>
          <dd>{{ displayText(WORKFLOW_TYPE_TEXT, run.run.workflow_type) }}</dd>
        </div>
        <div class="meta-row">
          <dt>发起人</dt>
          <dd data-test="run-created-by">{{ run.run.created_by_user_id || EMPTY }}</dd>
        </div>
        <div class="meta-row">
          <dt>创建时间</dt>
          <dd>{{ run.run.created_at || EMPTY }}</dd>
        </div>
        <div class="meta-row">
          <dt>更新时间</dt>
          <dd>{{ run.run.updated_at || EMPTY }}</dd>
        </div>
        <div class="meta-row">
          <dt>完成时间</dt>
          <dd>{{ run.run.completed_at || EMPTY }}</dd>
        </div>
        <div class="meta-row">
          <dt>最近错误</dt>
          <dd data-test="run-last-error">{{ run.run.last_error_code || EMPTY }}</dd>
        </div>
        <div class="meta-row">
          <dt>可重试</dt>
          <dd>{{ retryableState() ? '是（可稍后恢复）' : '否' }}</dd>
        </div>
      </dl>

      <!-- 执行记录（决定版本） -->
      <div v-if="run.execution" class="execution-section">
        <h5 class="sub-title">执行记录</h5>
        <dl class="run-meta">
          <div class="meta-row">
            <dt>执行状态</dt>
            <dd>
              {{ displayText(EXECUTION_STATUS_TEXT, run.execution.status) }}
            </dd>
          </div>
          <div class="meta-row">
            <dt>尝试次数</dt>
            <dd data-test="execution-attempts">{{ run.execution.attempt_count }}</dd>
          </div>
          <div class="meta-row">
            <dt>错误码</dt>
            <dd>{{ run.execution.error_code || EMPTY }}</dd>
          </div>
          <div class="meta-row">
            <dt>可重试</dt>
            <dd>{{ run.execution.error_retryable ? '是' : '否' }}</dd>
          </div>
        </dl>
      </div>
      <p v-else class="no-execution">
        <n-text depth="3">尚未根据决定版本发起执行{{ displayText(RUN_STATUS_TEXT, run.run.status) === '等待审批' ? '（等待审批决定）' : '' }}</n-text>
      </p>

      <!-- 业务结果（模拟记录） -->
      <div v-if="run.result" class="result-section">
        <h5 class="sub-title">业务结果（模拟记录）</h5>
        <dl class="run-meta">
          <div class="meta-row">
            <dt>记录 ID</dt>
            <dd data-test="result-record-id">{{ run.result.business_record_id || EMPTY }}</dd>
          </div>
          <div class="meta-row">
            <dt>订单标记</dt>
            <dd data-test="result-marked-refunded">
              {{ run.result.order_marked_refunded ? '已标记订单为退款状态（模拟）' : '未标记订单' }}
            </dd>
          </div>
        </dl>
        <p class="disclaimer">退款/补偿结果为模拟业务记录，不代表真实资金到账或真实发券。</p>
      </div>

      <!-- 恢复入口：条件集中由 canResume 派生（admin + 可重试/要求恢复 + 非不可恢复终态） -->
      <div v-if="canResume" class="recover-row" data-test="resume-row">
        <span class="recover-hint">执行未完成或遇到可重试失败；恢复不会重复产生业务副作用。</span>
        <n-button
          type="primary"
          :loading="resuming"
          :disabled="resuming"
          data-test="resume-run"
          @click="emit('resume')"
        >
          恢复执行
        </n-button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.run-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-3);
}

.panel-state {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
  min-height: 60px;
}

.resume-alert {
  max-width: 720px;
}

.run-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-2);
}

.panel-title {
  margin: 0;
  font-size: var(--sp-font-size-base);
  font-weight: 600;
}

.run-status {
  display: inline-flex;
}

.run-meta {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: var(--sp-space-2) var(--sp-space-5);
  font-size: var(--sp-font-size-sm);
}

.meta-row {
  display: flex;
  align-items: baseline;
  gap: var(--sp-space-2);
  min-width: 0;
}

.meta-row dt {
  color: var(--sp-color-text-3);
  flex-shrink: 0;
  min-width: 64px;
  font-size: var(--sp-font-size-xs);
}

.meta-row dd {
  margin: 0;
  word-break: break-all;
}

.execution-section,
.result-section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-2);
  padding-top: var(--sp-space-3);
  border-top: 1px dashed var(--sp-color-border);
}

.sub-title {
  margin: 0;
  font-size: var(--sp-font-size-sm);
  font-weight: 600;
  color: var(--sp-color-text-2);
}

.no-execution {
  margin: 0;
  padding-top: var(--sp-space-2);
  border-top: 1px dashed var(--sp-color-border);
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-3);
}

.disclaimer {
  margin: 0;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.recover-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-3);
  flex-wrap: wrap;
  padding: var(--sp-space-3);
  border: 1px solid var(--sp-color-warning);
  border-radius: var(--sp-radius-md);
  background: var(--sp-color-bg-page);
}

.recover-hint {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-2);
  max-width: 480px;
}
</style>
