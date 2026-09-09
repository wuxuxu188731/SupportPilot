<script setup lang="ts">
/*
 * 知识库列表页（/app/knowledge）。
 *
 * 事实与范围说明：
 *  - 后端一次返回当前企业全部文档（无分页/搜索）：搜索、状态筛选、
 *    来源类型筛选与排序全部为**前端本地处理**，不向后端发送参数；
 *  - 默认排序「最近更新」；界面在筛选区明确标注「本地筛选与排序」；
 *  - admin 才显示「上传文档」与文档卡片上的停用/启用操作，agent 显示
 *    只读说明；前端角色仅用于界面展示，后端在每个写操作上实时校验；
 *  - 停用/启用必须先确认（确认前不发送请求），成功后 store 刷新列表；
 *  - 列表不写入 localStorage，切换企业由 tenantReset 清理。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NAlert, NButton, NEmpty, NInput, NModal, NSelect, NSpin } from 'naive-ui'

import MainLayout from '@/layouts/MainLayout.vue'
import KnowledgeDocumentCard from '@/components/knowledge/KnowledgeDocumentCard.vue'
import DocumentUploadDialog from '@/components/knowledge/DocumentUploadDialog.vue'
import RoleTag from '@/components/common/RoleTag.vue'
import { useKnowledgeStore, type KnowledgeSortBy, type KnowledgeSourceFilter, type KnowledgeStatusFilter } from '@/stores/knowledge'
import { useOrganizationStore } from '@/stores/organization'
import { DOCUMENT_SOURCE_TYPE_TEXT, DOCUMENT_STATUS_TEXT } from '@/utils/knowledgeDisplay'

const router = useRouter()
const knowledgeStore = useKnowledgeStore()
const organizationStore = useOrganizationStore()

/** 状态筛选选项（本地）。 */
const STATUS_OPTIONS: { label: string; value: KnowledgeStatusFilter }[] = [
  { label: '全部状态', value: 'all' },
  { label: DOCUMENT_STATUS_TEXT.processing, value: 'processing' },
  { label: DOCUMENT_STATUS_TEXT.active, value: 'active' },
  { label: DOCUMENT_STATUS_TEXT.disabled, value: 'disabled' },
  { label: DOCUMENT_STATUS_TEXT.failed, value: 'failed' },
]

/** 来源类型筛选选项（本地）。 */
const SOURCE_OPTIONS: { label: string; value: KnowledgeSourceFilter }[] = [
  { label: '全部类型', value: 'all' },
  { label: 'Markdown', value: 'markdown' },
  { label: DOCUMENT_SOURCE_TYPE_TEXT.text, value: 'text' },
  { label: DOCUMENT_SOURCE_TYPE_TEXT.word, value: 'word' },
]

/** 排序选项（本地）。 */
const SORT_OPTIONS: { label: string; value: KnowledgeSortBy }[] = [
  { label: '最近更新', value: 'updated-desc' },
  { label: '最早创建', value: 'created-asc' },
  { label: '最近创建', value: 'created-desc' },
  { label: '标题', value: 'title-asc' },
]

/** 当前企业名称。 */
const organizationName = computed(
  () => organizationStore.currentOrganization?.name ?? '当前企业',
)

/** 文档总数（全部，不随筛选变化）。 */
const totalCount = computed(() => knowledgeStore.documents.length)

/** 筛选后的数量。 */
const filteredCount = computed(() => knowledgeStore.filteredDocuments.length)

/** 是否显示「上传文档」对话框。 */
const showUploadDialog = ref(false)

/** 打开文档详情。 */
function onOpenDocument(documentId: string): void {
  void router.push({ name: 'knowledge-detail', params: { documentId } })
}

/** 上传成功（succeeded）：进入新文档详情（其它回执留在列表查看）。 */
function onUploaded(): void {
  const receipt = knowledgeStore.uploadReceipt
  if (receipt && receipt.status === 'succeeded' && !receipt.deduplicated) {
    void router.push({ name: 'knowledge-detail', params: { documentId: receipt.document_id } })
  }
}

/** 停用确认：确认前不发送请求。 */
const confirmAction = ref<{ kind: 'disable' | 'enable'; documentId: string } | null>(null)

/** 请求确认停用。 */
function requestDisable(documentId: string): void {
  confirmAction.value = { kind: 'disable', documentId }
}

/** 请求确认启用。 */
function requestEnable(documentId: string): void {
  confirmAction.value = { kind: 'enable', documentId }
}

/** 确认对话框中的目标文档标题。 */
const confirmDocumentTitle = computed(() => {
  const target = knowledgeStore.documents.find(
    (item) => item.document_id === confirmAction.value?.documentId,
  )
  return target?.title ?? '该文档'
})

/** 执行已确认的停用/启用。 */
async function onConfirmStatusAction(): Promise<void> {
  const action = confirmAction.value
  if (!action) return
  try {
    if (action.kind === 'disable') {
      await knowledgeStore.disableDocument(action.documentId)
    } else {
      await knowledgeStore.enableDocument(action.documentId)
    }
  } finally {
    confirmAction.value = null
  }
}

onMounted(() => {
  // 进入页面加载列表（守卫已保证企业上下文存在）
  void knowledgeStore.loadDocuments()
})
</script>

<template>
  <MainLayout>
    <div class="knowledge-page" data-test="knowledge-page">
      <!-- 页面标题与当前企业 -->
      <header class="page-head">
        <div>
          <h2 class="page-title">知识库</h2>
          <p class="page-sub">
            当前企业：<strong>{{ organizationName }}</strong>
            <RoleTag :role="organizationStore.currentRole ?? 'agent'" />
            <span class="count-note">共 {{ totalCount }} 篇文档</span>
          </p>
        </div>
        <div class="head-actions">
          <n-button
            data-test="refresh-documents"
            :loading="knowledgeStore.documentsLoading"
            @click="knowledgeStore.refreshDocuments()"
          >
            刷新
          </n-button>
          <n-button
            v-if="knowledgeStore.isAdmin"
            type="primary"
            data-test="open-upload-dialog"
            @click="showUploadDialog = true"
          >
            上传文档
          </n-button>
        </div>
      </header>

      <!-- 一次性提示（如 404/409/403 后回到列表） -->
      <n-alert
        v-if="knowledgeStore.notice"
        type="warning"
        closable
        class="page-notice"
        @close="knowledgeStore.clearNotice()"
      >
        {{ knowledgeStore.notice }}
      </n-alert>

      <!-- agent 只读说明 -->
      <n-alert
        v-if="!knowledgeStore.isAdmin"
        type="default"
        :show-icon="true"
        class="readonly-note"
        data-test="readonly-note"
      >
        你当前是客服角色，可以查看知识库文档列表与详情；只有管理员可以上传文档、
        上传新版本、停用或启用文档。
      </n-alert>

      <!-- 本地搜索/筛选/排序（明确标注为本地处理） -->
      <div class="filters" data-test="knowledge-filters">
        <n-input
          v-model:value="knowledgeStore.searchText"
          class="filter-search"
          clearable
          placeholder="搜索标题或文档 ID（本地搜索）"
          data-test="knowledge-search"
        />
        <n-select
          v-model:value="knowledgeStore.statusFilter"
          class="filter-select"
          :options="STATUS_OPTIONS"
          data-test="status-filter"
        />
        <n-select
          v-model:value="knowledgeStore.sourceTypeFilter"
          class="filter-select"
          :options="SOURCE_OPTIONS"
          data-test="source-filter"
        />
        <n-select
          v-model:value="knowledgeStore.sortBy"
          class="filter-select"
          :options="SORT_OPTIONS"
          data-test="sort-by"
        />
      </div>
      <p class="filters-note">搜索、筛选与排序均为本地处理（服务端不支持这些参数）。</p>

      <!-- 初始加载中 -->
      <div v-if="knowledgeStore.documentsLoading && knowledgeStore.documents.length === 0" class="list-state" aria-live="polite">
        <n-spin size="small" />
        <span>正在加载知识库文档…</span>
      </div>

      <!-- 加载失败 -->
      <div v-else-if="knowledgeStore.documentsError" class="list-state" role="alert">
        <n-alert type="error" :show-icon="true">
          <template #header>知识库文档加载失败</template>
          <div class="error-body">
            <span>{{ knowledgeStore.documentsError }}</span>
            <n-button size="small" data-test="retry-documents" @click="knowledgeStore.refreshDocuments()">
              重试
            </n-button>
          </div>
        </n-alert>
      </div>

      <!-- 空态 -->
      <div v-else-if="knowledgeStore.documents.length === 0" class="list-state">
        <n-empty
          :description="knowledgeStore.isAdmin ? '还没有任何文档，点击「上传文档」开始建立知识库' : '当前企业还没有任何文档'"
          size="small"
          data-test="knowledge-empty"
        />
      </div>

      <!-- 筛选后为空 -->
      <div v-else-if="filteredCount === 0" class="list-state">
        <n-empty description="没有符合当前搜索/筛选条件的文档" size="small" data-test="filtered-empty" />
      </div>

      <!-- 文档列表 -->
      <div v-else class="document-list" data-test="document-list">
        <KnowledgeDocumentCard
          v-for="item in knowledgeStore.filteredDocuments"
          :key="item.document_id"
          :item="item"
          :is-admin="knowledgeStore.isAdmin"
          :status-updating="knowledgeStore.statusUpdatingDocumentIds.includes(item.document_id)"
          data-test="document-card-item"
          @open="onOpenDocument"
          @disable="requestDisable"
          @enable="requestEnable"
        />
      </div>

      <!-- 上传新文档对话框 -->
      <DocumentUploadDialog v-model:show="showUploadDialog" @uploaded="onUploaded" />

      <!-- 停用/启用确认对话框（确认前不发送请求） -->
      <n-modal
        :show="confirmAction !== null"
        :mask-closable="false"
        :close-on-esc="false"
        preset="card"
        class="confirm-modal"
        data-test="status-confirm-dialog"
        @update:show="confirmAction = null"
      >
        <p class="confirm-text">
          <template v-if="confirmAction?.kind === 'disable'">
            停用 <strong>{{ confirmDocumentTitle }}</strong> 后，该文档将不再参与客服
            Agent 的知识检索，但版本记录不会删除。确定停用吗？
          </template>
          <template v-else>
            启用 <strong>{{ confirmDocumentTitle }}</strong> 后，当前有效版本将重新
            参与知识检索。确定启用吗？
          </template>
        </p>
        <template #footer>
          <div class="confirm-actions">
            <n-button data-test="cancel-status-action" @click="confirmAction = null">
              取消
            </n-button>
            <n-button
              type="warning"
              :loading="knowledgeStore.statusUpdatingDocumentIds.includes(confirmAction?.documentId ?? '')"
              data-test="confirm-status-action"
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
.knowledge-page {
  max-width: 980px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-4);
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-space-3);
  flex-wrap: wrap;
}

.page-title {
  margin: 0 0 var(--sp-space-1);
  font-size: var(--sp-font-size-xl);
  font-weight: 600;
}

.page-sub {
  margin: 0;
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  color: var(--sp-color-text-2);
  font-size: var(--sp-font-size-sm);
  flex-wrap: wrap;
}

.count-note {
  color: var(--sp-color-text-3);
}

.head-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
}

.page-notice {
  max-width: 720px;
}

.readonly-note {
  max-width: 720px;
}

.filters {
  display: grid;
  grid-template-columns: 2fr 1fr 1fr 1fr;
  gap: var(--sp-space-2);
  align-items: center;
}

@media (max-width: 900px) {
  .filters {
    grid-template-columns: 1fr 1fr;
  }
}

@media (max-width: 560px) {
  .filters {
    grid-template-columns: 1fr;
  }
}

.filter-search {
  min-width: 0;
}

.filters-note {
  margin: 0;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.list-state {
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

.document-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-3);
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
