<script setup lang="ts">
/*
 * 审批详情页（/app/approvals/:approvalId）。
 *
 * 事实与范围说明：
 *  - 成员（agent/admin）均可查看：基本信息、请求版本、版本时间线、
 *    Run 完整状态；决定与恢复仅 admin 可见（后端始终最终校验）；
 *  - 详情加载后按 detail.run.run_id 调用 Run 查询接口展示完整状态；
 *  - 404（跨企业/不存在/已不可访问）时返回审批列表并提示；
 *  - 决定提交按真实 HTTP 状态码（200/201/202）分流；409 立即刷新服务端
 *    状态；422 字段错误映射回表单且不清空用户输入；403 刷新角色；
 *  - Run 恢复：仅 queued/running 时短周期轮询，提交决定/恢复后立即主动刷新。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAlert, NButton, NCard, NEmpty, NSpin, NTag } from 'naive-ui'

import MainLayout from '@/layouts/MainLayout.vue'
import DecisionForm from '@/components/approval/DecisionForm.vue'
import RunStatusPanel from '@/components/approval/RunStatusPanel.vue'
import ApprovalVersionTimeline from '@/components/approval/ApprovalVersionTimeline.vue'
import RoleTag from '@/components/common/RoleTag.vue'
import { useApprovalStore } from '@/stores/approvals'
import { useOrganizationStore } from '@/stores/organization'
import type { ApprovalDecisionTypeValue, DecisionChangesPayload } from '@/api/actionTypes'
import {
  ACTION_TYPE_TEXT,
  APPROVAL_STATUS_TAG,
  APPROVAL_STATUS_TEXT,
  DECISION_TYPE_TEXT,
  REFUND_SCOPE_TEXT,
  displayText,
  statusTagType,
} from '@/utils/actionDisplay'
import { formatAmountCents } from '@/utils/money'

const route = useRoute()
const router = useRouter()
const approvalStore = useApprovalStore()
const organizationStore = useOrganizationStore()

/** 路由参数中的审批 id。 */
const routeApprovalId = computed(() => {
  const value = route.params.approvalId
  return typeof value === 'string' && value.length > 0 ? value : null
})

/** 详情是否处于加载中（供骨架/空态切换）。 */
const detailLoading = computed(() => approvalStore.detailLoading && !approvalStore.detail)

/** 构建基本信息的只读源（详情加载后非空）。 */
const info = computed(() => approvalStore.detail)

/** 复制反馈（复制成功后短暂显示「已复制」）。 */
const copiedKey = ref<'approval' | 'proposal' | null>(null)

/** 后端 422 字段错误（提交后回传表单，不清空用户输入）。 */
const formServerErrors = ref<Record<string, string>>({})
/** 后端稳定错误消息（如 APPROVAL_INVALID_CHANGES）。 */
const formServerMessage = ref<string | null>(null)

/** 复制文本到剪贴板：优先 Clipboard API，失败时提示（浏览器限制场景）。 */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 继续走降级路径
  }
  try {
    const helper = document.createElement('textarea')
    helper.value = text
    helper.setAttribute('readonly', '')
    helper.style.position = 'fixed'
    helper.style.opacity = '0'
    document.body.appendChild(helper)
    helper.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(helper)
    return ok
  } catch {
    return false
  }
}

/** 复制审批 ID。 */
async function copyApprovalId(): Promise<void> {
  if (!info.value) return
  if (await copyText(info.value.approval_id)) {
    copiedKey.value = 'approval'
    window.setTimeout(() => {
      copiedKey.value = null
    }, 1600)
  }
}

/** 复制提案 ID。 */
async function copyProposalId(): Promise<void> {
  if (!info.value) return
  if (await copyText(info.value.proposal_id)) {
    copiedKey.value = 'proposal'
    window.setTimeout(() => {
      copiedKey.value = null
    }, 1600)
  }
}

/**
 * 打开审批详情：404 时回列表页（提示由 store.notice 承接显示），
 * 成功后按 run.run_id 加载 Run 完整状态并启动短周期轮询。
 */
async function openDetail(): Promise<void> {
  const approvalId = routeApprovalId.value
  if (!approvalId) {
    await router.replace({ name: 'approvals' })
    return
  }
  const result = await approvalStore.loadApprovalDetail(approvalId)
  if (result === 'not-found') {
    await router.replace({ name: 'approvals' })
    return
  }
  const detailRun = approvalStore.detail?.run
  if (result === 'ok' && detailRun?.run_id) {
    await approvalStore.loadRunStatus(detailRun.run_id)
    approvalStore.startRunAutoRefresh(detailRun.run_id)
  }
}

/** 决定提交处理器：成功/202/200 由 store 刷新并提示；错误映射回表单。 */
async function onDecideSubmit(payload: {
  decision: ApprovalDecisionTypeValue
  changes: DecisionChangesPayload | null
  comment: string | null
}): Promise<void> {
  const approvalId = routeApprovalId.value
  if (!approvalId) return
  const outcome = await approvalStore.decide(approvalId, payload)
  formServerErrors.value = {}
  formServerMessage.value = null
  if (outcome.result === 'error') {
    formServerErrors.value = outcome.error?.fieldErrors ?? {}
    formServerMessage.value = outcome.error?.message ?? '提交失败，请稍后重试'
    return
  }
  if (outcome.result === 'not-found') {
    await router.replace({ name: 'approvals' })
    return
  }
  if (outcome.result === 'conflict' || outcome.result === 'forbidden') {
    // store 已刷新服务端状态/角色；提示在 notice 中展示
    return
  }
  // created / pending / idempotent：store 已重载详情、Run 与列表缓存
}

/** Run 恢复处理器：store 按 200/409/503/403/404 分别处理。 */
async function onResume(): Promise<void> {
  const runId = approvalStore.run?.run.run_id
  if (!runId) return
  const outcome = await approvalStore.resumeRun(runId)
  if (outcome.result === 'not-found') {
    await router.replace({ name: 'approvals' })
  }
}

/** 关闭一次性提示。 */
function onCloseNotice(): void {
  approvalStore.clearNotice()
}

/** 页面可见性：隐藏时停止 Run 轮询，恢复可见时重新开始。 */
function onVisibilityChange(): void {
  const runId = approvalStore.run?.run.run_id
  if (document.hidden) {
    approvalStore.stopRunAutoRefresh()
  } else if (runId) {
    approvalStore.startRunAutoRefresh(runId)
  }
}

// 路由变化：审批 id 出现/切换都要重新加载
watch(routeApprovalId, (approvalId, previous) => {
  if (approvalId && approvalId !== previous) {
    void openDetail()
  } else if (!approvalId) {
    void router.replace({ name: 'approvals' })
  }
})

// 企业切换：审批状态已由 tenantReset 清理，这里重新加载（通常 404 回列表）
watch(
  () => organizationStore.currentOrganizationId,
  (organizationId, previous) => {
    if (organizationId && organizationId !== previous) {
      stopRunPolling()
      void openDetail()
    }
  },
)

/** 停止 Run 轮询（卸载/切页时调用）。 */
function stopRunPolling(): void {
  approvalStore.stopRunAutoRefresh()
}

onMounted(() => {
  document.addEventListener('visibilitychange', onVisibilityChange)
  void openDetail()
})

onUnmounted(() => {
  stopRunPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <MainLayout>
    <div class="approval-detail" data-test="approval-detail">
      <!-- 一次性提示（202/409/503/404 等语义） -->
      <n-alert
        v-if="approvalStore.notice"
        type="warning"
        closable
        class="page-notice"
        @close="onCloseNotice"
      >
        {{ approvalStore.notice }}
      </n-alert>

      <!-- 详情加载中 -->
      <div v-if="detailLoading" class="page-state" aria-live="polite">
        <n-spin size="small" />
        <span>正在加载审批详情…</span>
      </div>

      <!-- 详情加载失败 -->
      <div v-else-if="approvalStore.detailError && !info" class="page-state" role="alert">
        <n-empty
          :description="approvalStore.detailError ?? '审批详情加载失败'"
          size="small"
        >
          <template #extra>
            <n-button data-test="retry-detail" @click="openDetail">重试</n-button>
          </template>
        </n-empty>
      </div>

      <div v-else-if="info" class="detail-body">
        <!-- 标题区 -->
        <header class="detail-head">
          <div class="head-text">
            <div class="head-tags">
              <n-tag size="small" round :type="info.action_type === 'refund' ? 'warning' : 'info'">
                {{ displayText(ACTION_TYPE_TEXT, info.action_type) }}
              </n-tag>
              <n-tag size="small" round :type="statusTagType(APPROVAL_STATUS_TAG, info.approval_status)">
                {{ displayText(APPROVAL_STATUS_TEXT, info.approval_status) }}
              </n-tag>
              <n-tag v-if="info.self_approved" size="small" round type="warning">自审</n-tag>
            </div>
            <h2 class="head-title">
              {{ displayText(ACTION_TYPE_TEXT, info.action_type) }}审批
              <span class="head-id"><code>{{ info.approval_id }}</code></span>
            </h2>
            <p class="head-sub">
              当前企业：<strong>{{ organizationStore.currentOrganization?.name }}</strong>
              <RoleTag :role="organizationStore.currentRole ?? 'agent'" />
            </p>
          </div>
        </header>

        <!-- agent 只读说明（不隐藏按钮，而是如实说明角色边界） -->
        <n-alert v-if="!approvalStore.isAdmin" type="info" :show-icon="true" class="readonly-alert">
          你当前是客服角色，可以查看审批状态，但只有管理员可以作出决定或恢复执行。
        </n-alert>

        <div class="detail-grid">
          <!-- 基本信息 -->
          <n-card class="detail-card" :bordered="false" data-test="basic-info-card">
            <h3 class="card-title">基本信息</h3>
            <dl class="info-grid">
              <div class="info-row">
                <dt>审批 ID</dt>
                <dd>
                  <code class="id-code" :title="info.approval_id">{{ info.approval_id }}</code>
                  <n-button size="tiny" quaternary :aria-label="'复制审批 ID'" @click="copyApprovalId">
                    {{ copiedKey === 'approval' ? '已复制' : '复制' }}
                  </n-button>
                </dd>
              </div>
              <div class="info-row">
                <dt>提案 ID</dt>
                <dd>
                  <code class="id-code" :title="info.proposal_id">{{ info.proposal_id }}</code>
                  <n-button size="tiny" quaternary :aria-label="'复制提案 ID'" @click="copyProposalId">
                    {{ copiedKey === 'proposal' ? '已复制' : '复制' }}
                  </n-button>
                </dd>
              </div>
              <div class="info-row">
                <dt>订单</dt>
                <dd><code class="id-code" :title="info.order_id">{{ info.order_id }}</code></dd>
              </div>
              <div class="info-row">
                <dt>创建时间</dt>
                <dd data-test="detail-created-at">{{ info.created_at }}</dd>
              </div>
              <div class="info-row">
                <dt>请求人</dt>
                <dd data-test="detail-requested-by">{{ info.run.created_by_user_id || '—' }}</dd>
              </div>
              <div class="info-row">
                <dt>决定人</dt>
                <dd>{{ info.decision?.decided_by_user_id ?? '—' }}</dd>
              </div>
            </dl>
            <p class="id-note">
              请求人与决定人均以用户标识展示（接口未提供用户名）；自审标记表示提案人与决定人为同一用户。
            </p>
          </n-card>

          <!-- 请求版本 -->
          <n-card class="detail-card" :bordered="false" data-test="requested-version-card">
            <h3 class="card-title">请求版本（V{{ info.requested_version.version_no }}）</h3>
            <p class="version-amount">
              {{ formatAmountCents(info.requested_version.amount_cents, info.requested_version.currency) }}
              <span class="version-currency">{{ info.requested_version.currency }}</span>
            </p>
            <dl class="info-grid">
              <div class="info-row">
                <dt>原因</dt>
                <dd>
                  {{ info.requested_version.reason_code }}
                  <span class="muted">（详见版本时间线）</span>
                </dd>
              </div>
              <div class="info-row">
                <dt>说明</dt>
                <dd class="wrap-text">{{ info.requested_version.reason_text || '—' }}</dd>
              </div>
              <div v-if="info.action_type === 'refund'" class="info-row">
                <dt>退款范围</dt>
                <dd>{{ displayText(REFUND_SCOPE_TEXT, info.requested_version.refund_scope ?? null) }}</dd>
              </div>
              <div v-if="info.action_type === 'compensation'" class="info-row">
                <dt>券有效期</dt>
                <dd>{{ info.requested_version.coupon_valid_days !== null ? `${info.requested_version.coupon_valid_days} 天` : '—' }}</dd>
              </div>
              <div class="info-row">
                <dt>创建人</dt>
                <dd>{{ info.requested_version.created_by_user_id || '—' }}</dd>
              </div>
              <div class="info-row">
                <dt>创建时间</dt>
                <dd>{{ info.requested_version.created_at }}</dd>
              </div>
            </dl>
            <p class="id-note">金额与原因来自客服提案；执行结果均为模拟业务记录，不代表真实到账或真实发券。</p>
          </n-card>

          <!-- 当前版本与版本历史 -->
          <n-card class="detail-card" :bordered="false" data-test="versions-card">
            <h3 class="card-title">版本历史</h3>
            <p v-if="info.current_version" class="current-version-line" data-test="current-version">
              当前生效：V{{ info.current_version.version_no }} ·
              {{ formatAmountCents(info.current_version.amount_cents, info.current_version.currency) }}
            </p>
            <p v-else class="current-version-line muted">当前尚无生效版本</p>
            <ApprovalVersionTimeline
              :versions="info.versions"
              :current-version-id="info.current_version?.version_id ?? null"
              :action-type="info.action_type"
            />
          </n-card>

          <!-- 决定摘要（已决定） -->
          <n-card v-if="info.decision" class="detail-card" :bordered="false" data-test="decision-summary">
            <h3 class="card-title">决定摘要</h3>
            <dl class="info-grid">
              <div class="info-row">
                <dt>决定</dt>
                <dd>{{ displayText(DECISION_TYPE_TEXT, info.decision.decision) }}</dd>
              </div>
              <div class="info-row">
                <dt>采用版本</dt>
                <dd>{{ info.decision.decided_version_id }}</dd>
              </div>
              <div class="info-row">
                <dt>决定人</dt>
                <dd>{{ info.decision.decided_by_user_id || '—' }}</dd>
              </div>
              <div class="info-row">
                <dt>备注</dt>
                <dd class="wrap-text">{{ info.decision.comment || '—' }}</dd>
              </div>
              <div class="info-row">
                <dt>决定时间</dt>
                <dd>{{ info.decision.created_at }}</dd>
              </div>
              <div class="info-row">
                <dt>自审</dt>
                <dd>{{ info.self_approved ? '是（提案人与决定人为同一用户）' : '否' }}</dd>
              </div>
            </dl>
          </n-card>

          <!-- 决定表单：admin + 待审批 + 尚无决定 -->
          <n-card v-if="approvalStore.canSubmitDecision" class="detail-card" :bordered="false" data-test="decision-card">
            <DecisionForm
              :key="info.approval_id"
              :requested-version="info.requested_version"
              :action-type="info.action_type"
              :submitting="approvalStore.deciding"
              :server-field-errors="formServerErrors"
              :server-message="formServerMessage"
              @submit="onDecideSubmit"
            />
          </n-card>

          <!-- Run 状态 -->
          <n-card class="detail-card" :bordered="false" data-test="run-card">
            <h3 class="card-title">Run 状态</h3>
            <RunStatusPanel
              :run="approvalStore.run"
              :loading="approvalStore.runLoading"
              :error="approvalStore.runError"
              :resuming="approvalStore.resuming"
              :can-resume="approvalStore.canResume"
              :resume-required="approvalStore.lastDecision?.data.resume_required ?? false"
              :resume-error-code="approvalStore.lastDecision?.data.resume_error_code ?? null"
              @resume="onResume"
            />
          </n-card>
        </div>
      </div>
    </div>
  </MainLayout>
</template>

<style scoped>
.approval-detail {
  max-width: 980px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-4);
}

.page-notice {
  max-width: 760px;
}

.page-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-space-3);
  min-height: 260px;
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.detail-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-4);
}

.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-space-3);
  flex-wrap: wrap;
}

.head-tags {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  flex-wrap: wrap;
  margin-bottom: var(--sp-space-2);
}

.head-title {
  margin: 0 0 var(--sp-space-1);
  font-size: var(--sp-font-size-xl);
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  flex-wrap: wrap;
}

.head-id {
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.head-id code {
  background: var(--sp-color-bg-hover);
  border-radius: var(--sp-radius-sm);
  padding: 2px var(--sp-space-2);
  font-size: var(--sp-font-size-xs);
}

.head-sub {
  margin: 0;
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  color: var(--sp-color-text-2);
  font-size: var(--sp-font-size-sm);
}

.readonly-alert {
  max-width: 760px;
}

.detail-grid {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-4);
}

.detail-card {
  background: var(--sp-color-bg-card);
  box-shadow: var(--sp-shadow-card);
}

.card-title {
  margin: 0 0 var(--sp-space-3);
  font-size: var(--sp-font-size-base);
  font-weight: 600;
}

.info-grid {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--sp-space-2) var(--sp-space-5);
  font-size: var(--sp-font-size-sm);
}

.info-row {
  display: flex;
  align-items: baseline;
  gap: var(--sp-space-2);
  min-width: 0;
}

.info-row dt {
  color: var(--sp-color-text-3);
  flex-shrink: 0;
  min-width: 64px;
  font-size: var(--sp-font-size-xs);
}

.info-row dd {
  margin: 0;
  display: flex;
  align-items: center;
  gap: var(--sp-space-1);
  min-width: 0;
  word-break: break-all;
}

.id-code {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 260px;
  background: var(--sp-color-bg-hover);
  border-radius: var(--sp-radius-sm);
  padding: 1px var(--sp-space-1);
  font-size: var(--sp-font-size-xs);
}

.muted {
  color: var(--sp-color-text-3);
}

.wrap-text {
  white-space: pre-line;
}

.id-note,
.current-version-line {
  margin: var(--sp-space-3) 0 0;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.current-version-line {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-primary);
}

.version-amount {
  margin: 0 0 var(--sp-space-2);
  font-size: var(--sp-font-size-xl);
  font-weight: 700;
}

.version-currency {
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
  font-weight: 400;
  margin-left: var(--sp-space-1);
}
</style>
