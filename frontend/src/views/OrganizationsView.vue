<script setup lang="ts">
/*
 * 企业选择/创建页（/organizations）：
 *  - 展示当前用户可加入的企业与各自角色，点击进入主界面；
 *  - 无企业时展示空状态并引导创建；
 *  - 支持加载中、加载失败（错误 + 重试）、退出登录；
 *  - 页面加载时会恢复本地保存的企业选择（若仍有效，由守卫直接放行主界面）；
 *  - 若从受保护页面被引导至此，query.redirect 记录原目标，选择后返回。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAlert, NButton, NCard, NEmpty, NSpin, useMessage } from 'naive-ui'

import RoleTag from '@/components/common/RoleTag.vue'
import OrganizationCreateDialog from '@/components/organization/OrganizationCreateDialog.vue'
import { useAuthStore } from '@/stores/auth'
import { useOrganizationStore } from '@/stores/organization'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const authStore = useAuthStore()
const organizationStore = useOrganizationStore()

/** 是否显示创建企业对话框 */
const showCreateDialog = ref(false)

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
          role="button"
          tabindex="0"
          :aria-label="`进入企业 ${item.name}`"
          @click="handleEnter(item.organization_id)"
          @keydown.enter.prevent="handleEnter(item.organization_id)"
        >
          <div class="org-card-body">
            <div class="org-card-main">
              <h3 class="org-card-name">{{ item.name }}</h3>
              <p class="org-card-role">我在该企业的角色：{{ item.role === 'admin' ? '管理员' : '客服' }}</p>
            </div>
            <div class="org-card-side">
              <RoleTag :role="item.role" />
              <span class="org-card-enter" aria-hidden="true">进入 →</span>
            </div>
          </div>
        </n-card>

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
  cursor: pointer;
  box-shadow: var(--sp-shadow-card);
  transition: border-color 0.15s ease;
  border: 1px solid transparent;
}

.org-card:hover,
.org-card:focus-visible {
  border-color: var(--sp-color-primary);
  background: var(--sp-color-bg-hover);
}

.org-card-body {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-4);
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

.org-create-entry {
  margin-top: var(--sp-space-2);
}

@media (max-width: 560px) {
  .org-main {
    padding-top: var(--sp-space-5);
  }
}
</style>
