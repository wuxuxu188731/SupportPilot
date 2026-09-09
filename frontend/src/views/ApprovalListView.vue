<script setup lang="ts">
/*
 * 审批中心列表页（/app/approvals）。
 *
 * 事实与范围说明：
 *  - 默认展示「待审批」筛选；支持全部/已批准/修改后批准/已拒绝筛选；
 *  - 后端不返回总数：分页是否到底按「最近一页是否拉满」推断，不伪造总数；
 *  - 页面可见时每 30 秒低频刷新（自动刷新失败静默，不覆盖用户看到的错误），
 *    页面隐藏/卸载/切换企业即停止；
 *  - 审批列表不写入 localStorage，切换企业由 tenantReset 清理。
 */
import { computed, onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NAlert, NButton, NEmpty, NSpin, NTabs, NTabPane } from 'naive-ui'

import MainLayout from '@/layouts/MainLayout.vue'
import ApprovalListItemCard from '@/components/approval/ApprovalListItemCard.vue'
import RoleTag from '@/components/common/RoleTag.vue'
import { useApprovalStore, type ApprovalFilter } from '@/stores/approvals'
import { useOrganizationStore } from '@/stores/organization'

const router = useRouter()
const approvalStore = useApprovalStore()
const organizationStore = useOrganizationStore()

/** 筛选 Tab 选项：值即后端审批状态字段（all 表示不传 status）。 */
const FILTER_TABS: { key: ApprovalFilter; label: string }[] = [
  { key: 'pending', label: '待审批' },
  { key: 'all', label: '全部' },
  { key: 'approved', label: '已批准' },
  { key: 'approved_with_changes', label: '修改后批准' },
  { key: 'rejected', label: '已拒绝' },
]

/** 各筛选的空态文案（不伪造审批数量）。 */
const EMPTY_TEXT: Record<ApprovalFilter, string> = {
  pending: '暂无待审批提案',
  all: '暂无审批记录',
  approved: '暂无已批准审批',
  approved_with_changes: '暂无修改后批准记录',
  rejected: '暂无已拒绝审批',
}

/** 当前企业名称（头栏提示）。 */
const organizationName = computed(
  () => organizationStore.currentOrganization?.name ?? '当前企业',
)

/** 当前筛选对应的空态文案。 */
const emptyText = computed(() => EMPTY_TEXT[approvalStore.filterStatus])

/** 筛选切换：由 Tab 驱动，切换后从 offset=0 重新加载。 */
function onFilterChange(value: string): void {
  void approvalStore.setFilter(value as ApprovalFilter)
}

/** 手动刷新：重新加载当前筛选第一页（失败会展示错误与重试）。 */
function onRefresh(): void {
  void approvalStore.loadApprovals(true)
}

/** 进入审批详情（列表项点击）。 */
function onOpenApproval(approvalId: string): void {
  void router.push({ name: 'approval-detail', params: { approvalId } })
}

/** 关闭一次性提示（如「审批不存在或已不可访问」）。 */
function onCloseNotice(): void {
  approvalStore.clearNotice()
}

// 页面可见性：隐藏时停止低频刷新，恢复可见时重新开始
function onVisibilityChange(): void {
  if (document.hidden) {
    approvalStore.stopListAutoRefresh()
  } else {
    approvalStore.startListAutoRefresh()
  }
}

// 企业切换：审批状态已由 tenantReset 清理，这里重新加载当前筛选列表
watch(
  () => organizationStore.currentOrganizationId,
  (organizationId, previous) => {
    if (organizationId && organizationId !== previous) {
      void approvalStore.loadApprovals()
    }
  },
)

onMounted(() => {
  approvalStore.startListAutoRefresh()
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  approvalStore.stopListAutoRefresh()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

// 进入页面时加载列表（守卫已保证企业上下文存在）
void approvalStore.loadApprovals()
</script>

<template>
  <MainLayout>
    <div class="approvals-page" data-test="approvals-page">
      <!-- 页面标题与当前企业 -->
      <header class="page-head">
        <div>
          <h2 class="page-title">审批中心</h2>
          <p class="page-sub">
            当前企业：<strong>{{ organizationName }}</strong>
            <RoleTag :role="organizationStore.currentRole ?? 'agent'" />
          </p>
        </div>
        <n-button data-test="refresh-approvals" :loading="approvalStore.listLoading" @click="onRefresh">
          刷新
        </n-button>
      </header>

      <!-- 一次性提示（详情页 404 回退等） -->
      <n-alert
        v-if="approvalStore.notice"
        type="warning"
        closable
        class="page-notice"
        @close="onCloseNotice"
      >
        {{ approvalStore.notice }}
      </n-alert>

      <!-- 状态筛选 -->
      <n-tabs
        class="filter-tabs"
        :value="approvalStore.filterStatus"
        type="line"
        @update:value="onFilterChange"
      >
        <n-tab-pane v-for="tab in FILTER_TABS" :key="tab.key" :name="tab.key">
          <template #tab>{{ tab.label }}</template>
        </n-tab-pane>
      </n-tabs>

      <!-- 初始加载中 -->
      <div v-if="approvalStore.listLoading && approvalStore.approvals.length === 0" class="list-state" aria-live="polite">
        <n-spin size="small" />
        <span>正在加载审批列表…</span>
      </div>

      <!-- 加载失败 -->
      <div v-else-if="approvalStore.listError" class="list-state" role="alert">
        <n-alert type="error" :show-icon="true">
          <template #header>审批列表加载失败</template>
          <div class="error-body">
            <span>{{ approvalStore.listError }}</span>
            <n-button size="small" data-test="retry-approvals" @click="onRefresh">重试</n-button>
          </div>
        </n-alert>
      </div>

      <!-- 空态 -->
      <div v-else-if="approvalStore.approvals.length === 0" class="list-state">
        <n-empty :description="emptyText" size="small" data-test="approval-empty" />
      </div>

      <!-- 列表 -->
      <div v-else class="approval-list" data-test="approval-list">
        <ApprovalListItemCard
          v-for="item in approvalStore.approvals"
          :key="item.approval_id"
          :item="item"
          @open="onOpenApproval"
        />
        <div v-if="approvalStore.listHasMore" class="load-more">
          <n-button text data-test="load-more-approvals" :loading="approvalStore.listLoading" @click="approvalStore.loadMoreApprovals()">
            加载更多
          </n-button>
        </div>
      </div>
    </div>
  </MainLayout>
</template>

<style scoped>
.approvals-page {
  max-width: 900px;
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
}

.page-notice {
  max-width: 720px;
}

.filter-tabs {
  background: var(--sp-color-bg-card);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-lg);
  padding: 0 var(--sp-space-3);
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

.approval-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-3);
}

.load-more {
  display: flex;
  justify-content: center;
}
</style>
