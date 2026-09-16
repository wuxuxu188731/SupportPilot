<script setup lang="ts">
/*
 * 知识库文档详情页（/app/knowledge/:documentId）。
 *
 * 事实与范围说明：
 *  - 展示：基本信息（标题/ID/类型/状态/有效版本/时间）、全部版本历史
 *    （version_no 升序，当前有效版本高亮）、有效版本最近入库任务；
 *  - **正文查看**：详情/版本响应仍不含 raw_text，正文只从 4.4.8 的正文接口按版本读取
 *    （复用 DocumentContentViewer）；**下载入口依然不提供**（无原文下载接口）；
 *  - 支持深链参数：?versionId=&start=&end=&heading= —— 对话页窄屏降级跳转过来时，
 *    用它们打开指定版本并定位到引用区间；参数缺失时读当前有效版本；
 *  - admin 操作按状态显示：active→停用；disabled→启用；
 *    active/disabled 且类型为 markdown/text→上传新版本；
 *    processing/failed→只展示状态说明；word→不提供上传新版本；
 *    停用/启用必须确认后执行（确认前不发送请求）；不提供删除；
 *  - 404（不存在或不属于当前企业）返回知识库列表并提示
 *    「文档不存在或已不可访问」；
 *  - 详情不写入 localStorage，企业切换由 tenantReset 清理；
 *  - 页面卸载时停止任务轮询（store 的企业切换路径也会停止）。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAlert, NButton, NCard, NModal, NSpin, NTag } from 'naive-ui'

import MainLayout from '@/layouts/MainLayout.vue'
import DocumentContentViewer from '@/components/knowledge/DocumentContentViewer.vue'
import VersionHistoryPanel from '@/components/knowledge/VersionHistoryPanel.vue'
import IngestionJobPanel from '@/components/knowledge/IngestionJobPanel.vue'
import VersionUploadDialog from '@/components/knowledge/VersionUploadDialog.vue'
import type { CitationTarget } from '@/api/types'
import { useKnowledgeStore } from '@/stores/knowledge'
import {
  DOCUMENT_SOURCE_TYPE_TEXT,
  DOCUMENT_STATUS_TEXT,
  knowledgeDisplayText,
} from '@/utils/knowledgeDisplay'
import { formatDateTime } from '@/utils/time'

const route = useRoute()
const router = useRouter()
const knowledgeStore = useKnowledgeStore()

/** 当前路由中的文档 id。 */
const documentId = computed(() => String(route.params.documentId ?? ''))

/** 当前文档详情（未加载/已失效为 null）。 */
const detail = computed(() => knowledgeStore.documentDetail)

/** 当前有效版本号（展示用）。 */
const activeVersionNo = computed(() => {
  const id = detail.value?.active_version_id
  const active = detail.value?.versions.find((version) => version.version_id === id)
  return active ? `v${active.version_no}` : '—'
})

/** 是否允许上传新版本：admin + 有效状态（active/disabled）。
 *  文档类型不再限制——word/pdf 由解析服务处理，与文本类型同样支持版本覆盖。 */
const canUploadVersion = computed(
  () =>
    knowledgeStore.isAdmin &&
    detail.value !== null &&
    (detail.value.status === 'active' || detail.value.status === 'disabled'),
)

/** 停用/启用确认对话框。 */
const showStatusConfirm = ref(false)
/** 待确认的动作：disable / enable。 */
const confirmKind = ref<'disable' | 'enable'>('disable')

/** 版本上传对话框。 */
const showVersionUpload = ref(false)

/** 请求停用。 */
function requestDisable(): void {
  confirmKind.value = 'disable'
  showStatusConfirm.value = true
}

/** 请求启用。 */
function requestEnable(): void {
  confirmKind.value = 'enable'
  showStatusConfirm.value = true
}

/** 执行已确认的停用/启用（成功后 store 已刷新列表与详情）。 */
async function onConfirmStatusAction(): Promise<void> {
  if (!documentId.value) return
  try {
    if (confirmKind.value === 'disable') {
      await knowledgeStore.disableDocument(documentId.value)
    } else {
      await knowledgeStore.enableDocument(documentId.value)
    }
  } finally {
    showStatusConfirm.value = false
  }
}

/** 加载详情：404 时返回列表（store 已设置提示）。 */
async function loadDetail() {
  if (!documentId.value || !detail.value || detail.value.document_id !== documentId.value) {
    const result = await knowledgeStore.loadDocumentDetail(documentId.value)
    if (result === 'not-found') {
      void router.replace({ name: 'knowledge' })
    }
  }
}

/** 返回知识库列表。 */
function onBack(): void {
  void router.push({ name: 'knowledge' })
}

/** 正文查看器弹窗显隐。 */
const showContentViewer = ref(false)

/**
 * 正文查看目标：文档 + 版本 + 可选偏移。
 *
 * 为什么带 versionId：知识块偏移相对某一个版本的正文，必须按版本读取；
 * 深链（来自对话页窄屏降级跳转）会显式给出版本，否则用当前有效版本。
 */
const contentTarget = computed<CitationTarget | null>(() => {
  const current = detail.value
  if (current === null) return null
  const versionId = readQueryString('versionId') ?? current.active_version_id
  // 没有有效版本（processing / failed）时无法读正文，入口禁用并说明原因
  if (versionId === null || versionId.length === 0) return null
  return {
    documentId: current.document_id,
    versionId,
    startOffset: readQueryNumber('start'),
    endOffset: readQueryNumber('end'),
    headingPath: readQueryString('heading'),
    title: current.title,
  }
})

/** 是否提供「查看正文」入口（有有效版本或深链指定版本时可用）。 */
const canOpenContent = computed(() => contentTarget.value !== null)

/** 无有效版本时禁用入口的原因文案。 */
const contentUnavailableReason = computed(() => {
  if (detail.value?.status === 'processing') return '文档尚未完成入库，暂时无法查看正文。'
  if (detail.value?.status === 'failed') return '文档入库失败，没有可查看的正文。'
  return '当前没有可读取的有效版本。'
})

/** 读取 query 中的字符串参数（缺失/非法返回 null）。 */
function readQueryString(key: string): string | null {
  const value = route.query[key]
  return typeof value === 'string' && value.length > 0 ? value : null
}

/** 读取 query 中的非负整数偏移（缺失/非法返回 null）。 */
function readQueryNumber(key: string): number | null {
  const raw = readQueryString(key)
  if (raw === null) return null
  const parsed = Number.parseInt(raw, 10)
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null
}

/** 打开正文查看器。 */
function onOpenContent(): void {
  if (!canOpenContent.value) return
  showContentViewer.value = true
}

// 深链直达（?versionId=&start=&end=）：详情加载完成后自动打开查看器并定位
watch(
  () => [detail.value?.document_id, route.query.versionId] as const,
  () => {
    if (!canOpenContent.value) return
    if (readQueryString('versionId') === null) return
    if (readQueryNumber('start') === null) return
    showContentViewer.value = true
  },
)

// 路由参数变化（详情 → 详情）或首次进入
watch(
  () => documentId.value,
  () => {
    void loadDetail()
  },
  { immediate: true },
)

onMounted(() => {
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  // 页面卸载停止任务轮询（企业切换由 tenantReset 兜底）
  knowledgeStore.stopJobPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

/** 页面可见性：隐藏暂停轮询、恢复可见继续（tick 内也各自校验）。 */
function onVisibilityChange(): void {
  const job = knowledgeStore.ingestionJob
  if (!job) return
  if (document.visibilityState === 'visible') {
    if (job.status === 'queued' || job.status === 'running') {
      knowledgeStore.startJobPolling(job.job_id)
    }
  } else {
    knowledgeStore.stopJobPolling()
  }
}
</script>

<template>
  <MainLayout>
    <div class="detail-page" data-test="knowledge-detail-page">
      <header class="page-head">
        <n-button quaternary data-test="back-to-knowledge" @click="onBack">
          ← 返回知识库
        </n-button>
      </header>

      <!-- 一次性提示（如 409/403） -->
      <n-alert
        v-if="knowledgeStore.notice"
        type="warning"
        closable
        class="page-notice"
        @close="knowledgeStore.clearNotice()"
      >
        {{ knowledgeStore.notice }}
      </n-alert>

      <!-- 加载中 -->
      <div v-if="knowledgeStore.detailLoading && !detail" class="page-state" aria-live="polite">
        <n-spin size="small" />
        <span>正在加载文档详情…</span>
      </div>

      <!-- 详情加载失败 -->
      <div v-else-if="knowledgeStore.detailError" class="page-state" role="alert">
        <n-alert type="error" :show-icon="true">
          <template #header>文档详情加载失败</template>
          <div class="error-body">
            <span>{{ knowledgeStore.detailError }}</span>
            <n-button size="small" data-test="retry-detail" @click="loadDetail()">
              重试
            </n-button>
          </div>
        </n-alert>
      </div>

      <template v-else-if="detail">
        <!-- 基本信息 -->
        <n-card class="info-card" :bordered="false" data-test="document-info-card">
          <div class="info-head">
            <h2 class="doc-title">{{ detail.title }}</h2>
            <div class="info-tags">
              <n-tag
                data-test="detail-status-tag"
                :type="detail.status === 'active' ? 'success' : detail.status === 'failed' ? 'error' : detail.status === 'processing' ? 'warning' : 'default'"
                size="small"
              >
                {{ knowledgeDisplayText(DOCUMENT_STATUS_TEXT, detail.status) }}
              </n-tag>
              <n-tag size="small" data-test="detail-source-tag">
                {{ knowledgeDisplayText(DOCUMENT_SOURCE_TYPE_TEXT, detail.source_type) }}
              </n-tag>
            </div>
          </div>

          <dl class="info-meta">
            <div class="meta-item">
              <dt>文档 ID</dt>
              <dd class="mono" data-test="detail-document-id">{{ detail.document_id }}</dd>
            </div>
            <div class="meta-item">
              <dt>有效版本</dt>
              <dd class="mono">{{ detail.active_version_id ?? '—' }}</dd>
            </div>
            <div class="meta-item">
              <dt>当前版本号</dt>
              <dd>{{ activeVersionNo }}</dd>
            </div>
            <div class="meta-item">
              <dt>创建时间</dt>
              <dd>{{ formatDateTime(detail.created_at) }}</dd>
            </div>
            <div class="meta-item">
              <dt>更新时间</dt>
              <dd>{{ formatDateTime(detail.updated_at) }}</dd>
            </div>
          </dl>

          <!-- admin 操作区 -->
          <div v-if="knowledgeStore.isAdmin" class="admin-actions" data-test="admin-actions">
            <!-- active：停用 -->
            <n-button
              v-if="detail.status === 'active'"
              type="warning"
              secondary
              :loading="knowledgeStore.statusUpdatingDocumentIds.includes(detail.document_id)"
              data-test="detail-disable"
              @click="requestDisable"
            >
              停用文档
            </n-button>
            <!-- disabled：启用 -->
            <n-button
              v-else-if="detail.status === 'disabled'"
              type="primary"
              secondary
              :loading="knowledgeStore.statusUpdatingDocumentIds.includes(detail.document_id)"
              data-test="detail-enable"
              @click="requestEnable"
            >
              启用文档
            </n-button>
            <!-- processing：只展示状态，不允许启用/停用 -->
            <p v-else-if="detail.status === 'processing'" class="state-note">
              文档正在处理中，完成后即可对其进行管理。
            </p>
            <!-- failed：展示失败，不允许直接启用 -->
            <p v-else-if="detail.status === 'failed'" class="state-note">
              文档入库失败，不可用于检索；不会自动重试，如需重新入库请上传相同内容的新版本。
            </p>

            <!-- 上传新版本（active/disabled + markdown/text） -->
            <n-button
              v-if="canUploadVersion"
              type="primary"
              data-test="open-version-upload"
              @click="showVersionUpload = true"
            >
              上传新版本
            </n-button>
          </div>
          <p v-else class="readonly-note" data-test="detail-readonly-note">
            你当前是客服角色，可以查看文档详情与版本历史；只有管理员可以上传新版本、
            停用或启用文档。
          </p>

          <!-- 正文查看入口：详情/版本响应仍不含 raw_text，正文按版本单独读取；
               下载入口依然不提供（无原文下载接口） -->
          <div class="content-entry" data-test="content-entry">
            <n-button
              type="primary"
              secondary
              :disabled="!canOpenContent"
              data-test="open-content"
              @click="onOpenContent"
            >
              查看正文
            </n-button>
            <p v-if="!canOpenContent" class="state-note" data-test="content-unavailable">
              {{ contentUnavailableReason }}
            </p>
            <p v-else class="feature-note" data-test="content-note">
              正文为入库时解析/转换后的 Markdown（PDF/Word 也已转换），与原文排版可能不一致；
              不提供原文下载。
            </p>
          </div>
        </n-card>

        <!-- 正文查看器弹窗：复用对话页同一个查看器组件 -->
        <n-modal
          v-model:show="showContentViewer"
          preset="card"
          class="content-modal"
          :title="detail.title"
          :bordered="false"
          style="width: min(960px, 92vw)"
        >
          <div v-if="contentTarget" class="content-modal-body" data-test="content-viewer-body">
            <DocumentContentViewer
              :document-id="contentTarget.documentId"
              :version-id="contentTarget.versionId"
              :start-offset="contentTarget.startOffset"
              :end-offset="contentTarget.endOffset"
              :heading-path="contentTarget.headingPath"
            />
          </div>
        </n-modal>

        <!-- 版本历史 -->
        <VersionHistoryPanel
          :versions="detail.versions"
          :active-version-id="detail.active_version_id"
        />

        <!-- 最近入库任务 -->
        <IngestionJobPanel
          :job="knowledgeStore.ingestionJob"
          :loading="knowledgeStore.jobLoading"
        />
      </template>

      <!-- 上传新版本对话框 -->
      <VersionUploadDialog
        v-if="detail"
        v-model:show="showVersionUpload"
        :document="detail"
      />

      <!-- 停用/启用确认对话框 -->
      <n-modal
        :show="showStatusConfirm"
        :mask-closable="false"
        :close-on-esc="false"
        preset="card"
        class="confirm-modal"
        data-test="detail-status-confirm"
        @update:show="showStatusConfirm = false"
      >
        <p class="confirm-text">
          <template v-if="confirmKind === 'disable'">
            停用 <strong>{{ detail?.title }}</strong> 后，该文档将不再参与客服 Agent 的知识检索，
            但版本记录不会删除。确定停用吗？
          </template>
          <template v-else>
            启用 <strong>{{ detail?.title }}</strong> 后，当前有效版本将重新参与知识检索。
            确定启用吗？
          </template>
        </p>
        <template #footer>
          <div class="confirm-actions">
            <n-button @click="showStatusConfirm = false">取消</n-button>
            <n-button
              type="warning"
              :loading="knowledgeStore.statusUpdatingDocumentIds.includes(detail?.document_id ?? '')"
              data-test="confirm-detail-status"
              @click="onConfirmStatusAction"
            >
              确定
            </n-button>
          </div>
        </template>
      </n-modal>
    </div>
  </MainLayout>
</template>

<style scoped>
.detail-page {
  max-width: 900px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-4);
}

.page-head {
  display: flex;
  align-items: center;
  gap: var(--sp-space-3);
}

.page-notice {
  max-width: 720px;
}

.page-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-space-3);
  min-height: 220px;
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.error-body {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-3);
}

.info-card {
  background: var(--sp-color-bg-card);
  box-shadow: var(--sp-shadow-card);
}

.info-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-space-3);
  flex-wrap: wrap;
}

.doc-title {
  margin: 0;
  font-size: var(--sp-font-size-lg);
  font-weight: 600;
  overflow-wrap: anywhere;
}

.info-tags {
  display: flex;
  gap: var(--sp-space-2);
  flex-shrink: 0;
}

.info-meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--sp-space-2) var(--sp-space-4);
  margin: var(--sp-space-4) 0 0;
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

.mono {
  font-family: monospace;
}

.admin-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-space-3);
  flex-wrap: wrap;
  margin-top: var(--sp-space-4);
}

.state-note {
  margin: var(--sp-space-4) 0 0;
  padding: var(--sp-space-2) var(--sp-space-3);
  border-radius: var(--sp-radius-sm);
  background: var(--sp-color-bg-active);
  color: var(--sp-color-text-2);
  font-size: var(--sp-font-size-sm);
}

.readonly-note {
  margin: var(--sp-space-4) 0 0;
  color: var(--sp-color-text-2);
  font-size: var(--sp-font-size-sm);
}

.feature-note {
  margin: var(--sp-space-3) 0 0;
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-xs);
}

/* 正文查看入口：按钮与其说明同块，宽屏并排、窄屏自动换行 */
.content-entry {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-space-3);
  margin-top: var(--sp-space-4);
  padding-top: var(--sp-space-3);
  border-top: 1px solid var(--sp-color-border);
}

.content-entry .state-note,
.content-entry .feature-note {
  margin: 0;
}

/* 正文弹窗：给查看器一个固定高度，正文区域内部滚动。
 * 高度在视口允许范围内尽量放大（原先 min(70vh,640px) 一屏只能看几行，
 * 正文要频繁滚动）；上限 860px 并留出弹窗标题与页面边距，不会超出视口。 */
.content-modal-body {
  height: min(80vh, 860px);
  display: flex;
}

.content-modal-body > * {
  flex: 1;
  min-width: 0;
}

.confirm-modal {
  max-width: 480px;
}

.confirm-text {
  margin: 0;
  line-height: 1.7;
  color: var(--sp-color-text-2);
}

.confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-space-2);
}
</style>
