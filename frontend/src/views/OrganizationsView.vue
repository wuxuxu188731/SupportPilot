<script setup lang="ts">
/*
 * 企业选择/创建页（/organizations）：
 *  - 展示当前用户可加入的企业与各自角色，点击进入主界面；
 *  - 无企业时展示空状态并引导创建；
 *  - 管理员可就地为某个企业生成演示业务数据（面试现场一键初始化）；
 *  - 支持加载中、加载失败（错误 + 重试）、退出登录；
 *  - 页面加载时会恢复本地保存的企业选择（若仍有效，由守卫直接放行主界面）；
 *  - 若从受保护页面被引导至此，query.redirect 记录原目标，选择后返回。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAlert, NButton, NCard, NEmpty, NSpin, useMessage } from 'naive-ui'

import RoleTag from '@/components/common/RoleTag.vue'
import AppIcon from '@/components/common/AppIcon.vue'
import OrganizationCreateDialog from '@/components/organization/OrganizationCreateDialog.vue'
import { generateDemoData } from '@/api/demoData'
import { toApiErrorFromThrowable } from '@/api/errors'
import type { DemoSeedCounts } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useOrganizationStore } from '@/stores/organization'
import { formatDateTime } from '@/utils/time'

/** 演示数据新增行数的中文标签（与后端 counts 固定键一一对应，顺序固定）。 */
const DEMO_COUNT_LABELS: Array<{ key: keyof DemoSeedCounts; label: string }> = [
  { key: 'customers', label: '客户' },
  { key: 'orders', label: '订单' },
  { key: 'shipments', label: '物流' },
  { key: 'tickets', label: '工单' },
  { key: 'ticket_comments', label: '备注' },
]

/** 演示数据与已有业务记录冲突时的补充引导（后端 409）。 */
const DEMO_CONFLICT_SUFFIX = '，请新建一个企业后重新生成'

/** 生成演示数据的失败提示：冲突（409）追加可操作引导，其余原样透出。 */
function describeGenerateError(error: unknown): string {
  const apiError = toApiErrorFromThrowable(error)
  return apiError.status === 409
    ? apiError.message + DEMO_CONFLICT_SUFFIX
    : apiError.message
}

const route = useRoute()
const router = useRouter()
const message = useMessage()
const authStore = useAuthStore()
const organizationStore = useOrganizationStore()

/** 是否显示创建企业对话框 */
const showCreateDialog = ref(false)

/** 正在生成演示数据的企业 id；null 表示当前没有进行中的生成 */
const generatingOrganizationId = ref<string | null>(null)

/** 最近一次生成结果的展示文案（含企业名、新增明细与数据基准时间） */
const lastSeedSummary = ref<string | null>(null)

/** 选择企业后的落地地址：优先回到被引导前记录的原始目标。 */
const destination = computed(() => {
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : ''
  return redirect.startsWith('/') && !redirect.startsWith('//') ? redirect : '/app'
})

/** 进入页面时加载企业列表（守卫已确保登录态与初始化完成）。 */
onMounted(() => {
  void organizationStore.load()
})

/** 点击企业：选择并进入主界面（或原目标页面）。 */
async function handleEnter(organizationId: string): Promise<void> {
  const ok = await organizationStore.select(organizationId)
  if (!ok) {
    // 选择的目标不在最新列表中（已被移出等）：刷新后提示
    message.warning('企业信息已变化，请刷新后重试')
    await organizationStore.load(true)
    return
  }
  await router.push(destination.value)
}

/** 创建成功：store 已刷新列表并自动选中新企业，直接进入主界面。 */
function handleCreated(): void {
  message.success('企业创建成功')
  void router.push(destination.value)
}

/**
 * 就地为某个企业生成演示业务数据（仅该企业管理员可见该入口）。
 *
 * 这里显式传企业 id：用户可能还没进入任何企业（本地没有「当前企业」），
 * 而接口需要 X-Organization-ID；生成完成后停在当前页，由用户自行决定进入。
 */
async function handleGenerate(organizationId: string, organizationName: string): Promise<void> {
  if (generatingOrganizationId.value) return
  generatingOrganizationId.value = organizationId
  try {
    const result = await generateDemoData(organizationId)
    const detail = DEMO_COUNT_LABELS
      .map(({ key, label }) => `${label} ${result.counts[key]}`)
      .join('、')
    const created = DEMO_COUNT_LABELS.reduce((sum, { key }) => sum + result.counts[key], 0)
    const prefix = created > 0 ? `「${organizationName}」已生成演示数据：` : `「${organizationName}」演示数据已存在：`
    lastSeedSummary.value = `${prefix}${detail}（新增 ${created} 条，数据基准时间 ${formatDateTime(result.reference_at)}）`
    message.success(lastSeedSummary.value)
  } catch (error) {
    const text = describeGenerateError(error)
    lastSeedSummary.value = null
    message.error(text)
  } finally {
    generatingOrganizationId.value = null
  }
}

/** 退出登录：本地清理后返回登录页（后端无服务端登出接口）。 */
async function handleLogout(): Promise<void> {
  await authStore.logout()
  message.success('已退出登录')
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="org-page">
    <!-- 顶栏：当前登录用户与退出登录 -->
    <header class="org-topbar">
      <div class="org-brand"><AppIcon name="spark" :size="26" /> <strong>SupportPilot</strong><span>企业工作空间</span></div>
      <div class="org-user">
        <span class="org-username">当前用户：{{ authStore.currentUser?.username }}</span>
        <n-button quaternary size="small" aria-label="退出登录" @click="handleLogout">
          退出登录
        </n-button>
      </div>
    </header>

    <main class="org-main">
      <div class="org-heading">
        <h1 class="org-title">选择企业</h1>
        <p class="org-subtitle">选择要进入的企业，或在企业内以对应角色开展工作</p>
      </div>

      <!-- 加载中 -->
      <div v-if="organizationStore.loading && !organizationStore.loaded" class="org-loading">
        <n-spin size="large" description="正在加载企业列表…" />
      </div>

      <!-- 加载失败：给出错误与重试入口 -->
      <n-alert
        v-else-if="organizationStore.error"
        type="error"
        :show-icon="true"
        class="org-error"
        role="alert"
      >
        <div class="error-row">
          <span>{{ organizationStore.error }}</span>
          <n-button size="small" :loading="organizationStore.loading" @click="organizationStore.load(true)">
            重试
          </n-button>
        </div>
      </n-alert>

      <!-- 空状态：新用户引导创建企业 -->
      <n-empty
        v-else-if="organizationStore.isEmpty"
        class="org-empty"
        description="你还没有加入任何企业"
      >
        <template #extra>
          <div class="empty-guide">
            <p class="empty-guide-text">创建你的第一个企业，你将自动成为该企业的管理员。</p>
            <n-button type="primary" @click="showCreateDialog = true">创建企业</n-button>
          </div>
        </template>
      </n-empty>

      <!-- 企业列表 -->
      <div v-else class="org-list">
        <n-card
          v-for="item in organizationStore.organizations"
          :key="item.organization_id"
          class="org-card"
          :bordered="false"
        >
          <!-- 卡片主体是「进入企业」的可点击区域；生成按钮放在主体之外，
               避免按钮嵌在 role="button" 里造成可访问性冲突 -->
          <div
            class="org-card-body"
            role="button"
            tabindex="0"
            :aria-label="`进入企业 ${item.name}`"
            @click="handleEnter(item.organization_id)"
            @keydown.enter.prevent="handleEnter(item.organization_id)"
          >
            <div class="org-card-main">
              <h3 class="org-card-name">{{ item.name }}</h3>
              <p class="org-card-role">我在该企业的角色：{{ item.role === 'admin' ? '管理员' : '客服' }}</p>
            </div>
            <div class="org-card-side">
              <RoleTag :role="item.role" />
              <span class="org-card-enter" aria-hidden="true">进入 →</span>
            </div>
          </div>

          <!-- 演示数据入口：仅该企业管理员可见（后端仍会强制校验角色） -->
          <div v-if="item.role === 'admin'" class="org-card-actions">
            <n-button
              size="small"
              secondary
              data-test="generate-demo-data"
              :aria-label="`为 ${item.name} 生成测试数据`"
              :loading="generatingOrganizationId === item.organization_id"
              :disabled="generatingOrganizationId !== null"
              @click="handleGenerate(item.organization_id, item.name)"
            >
              生成测试数据
            </n-button>
            <span class="org-card-actions-hint">
              为该企业生成订单、物流与工单等演示数据（可重复点击，日期会刷新到当前）
            </span>
          </div>
        </n-card>

        <!-- 生成结果：保留一行文本，刷新页面即消失（不做持久化） -->
        <p v-if="lastSeedSummary" class="org-demo-summary" data-test="demo-data-summary">
          {{ lastSeedSummary }}
        </p>

        <n-button
          class="org-create-entry"
          secondary
          block
          aria-label="创建新企业"
          @click="showCreateDialog = true"
        >
          ＋ 创建新企业
        </n-button>
      </div>
    </main>

    <OrganizationCreateDialog v-model:show="showCreateDialog" @created="handleCreated" />
  </div>
</template>

<style scoped>
.org-brand { display: flex; align-items: center; gap: 12px; color: var(--sp-color-primary); }
.org-brand strong { font-size: 21px; letter-spacing: -.5px; }
.org-brand > span { margin-left: 10px; padding-left: 20px; border-left: 1px solid var(--sp-color-border); font-size: 12px; color: var(--sp-color-text-3); }
.org-page {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

/* 简洁顶栏 */
.org-topbar {
  display: flex;
  justify-content: flex-end;
  padding: var(--sp-space-3) var(--sp-space-6);
  border-bottom: 1px solid var(--sp-color-border);
  background: var(--sp-color-bg-card);
}

.org-user {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
}

.org-username {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-2);
}

/* 主体内容 */
.org-main {
  width: 100%;
  max-width: 720px;
  margin: 0 auto;
  padding: var(--sp-space-8) var(--sp-space-5) var(--sp-space-10);
}

.org-heading {
  margin-bottom: var(--sp-space-6);
}

.org-title {
  margin: 0 0 var(--sp-space-1);
  font-size: var(--sp-font-size-xxl);
  font-weight: 600;
}

.org-subtitle {
  margin: 0;
  color: var(--sp-color-text-3);
}

.org-loading {
  display: flex;
  justify-content: center;
  padding: var(--sp-space-10) 0;
}

.org-error {
  margin-top: var(--sp-space-4);
}

.error-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-3);
}

.org-empty {
  margin-top: var(--sp-space-8);
}

.empty-guide {
  text-align: center;
}

.empty-guide-text {
  margin: 0 0 var(--sp-space-4);
  color: var(--sp-color-text-2);
}

/* 企业卡片 */
.org-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-3);
}

.org-card {
  box-shadow: var(--sp-shadow-card);
  transition: border-color 0.15s ease;
  border: 1px solid transparent;
}

.org-card:has(.org-card-body:hover) {
  border-color: var(--sp-color-primary);
  background: var(--sp-color-bg-hover);
}

.org-card-body {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-4);
  cursor: pointer;
}

.org-card-body:focus-visible {
  outline: 2px solid var(--sp-color-primary);
  outline-offset: 2px;
  border-radius: var(--sp-radius-sm);
}

.org-card-main {
  min-width: 0;
}

.org-card-name {
  margin: 0 0 var(--sp-space-1);
  font-size: var(--sp-font-size-lg);
  font-weight: 600;
}

.org-card-role {
  margin: 0;
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-3);
}

.org-card-side {
  display: flex;
  align-items: center;
  gap: var(--sp-space-3);
  flex-shrink: 0;
}

.org-card-enter {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-primary);
  font-weight: 500;
}

/* 演示数据入口：与「进入企业」的可点击区域分开，避免误触 */
.org-card-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-space-3);
  margin-top: var(--sp-space-3);
  padding-top: var(--sp-space-3);
  border-top: 1px dashed var(--sp-color-border);
}

.org-card-actions-hint {
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.org-demo-summary {
  margin: 0;
  padding: var(--sp-space-2) var(--sp-space-3);
  border-radius: var(--sp-radius-sm);
  background: var(--sp-color-bg-hover);
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-2);
}

.org-create-entry {
  margin-top: var(--sp-space-2);
}

@media (max-width: 560px) {
  .org-main {
    padding-top: var(--sp-space-5);
  }

  .org-card-body {
    flex-direction: column;
    align-items: flex-start;
  }

  .org-card-side {
    flex-wrap: wrap;
  }
}
</style>
