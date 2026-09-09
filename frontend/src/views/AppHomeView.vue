<script setup lang="ts">
/*
 * 主界面（/app）：认证与租户闭环后的工作台首页。
 * 只展示真实存在的上下文（用户、企业、角色）与模块开放说明，
 * 不展示任何虚构的业务统计数据。
 */
import { useRouter } from 'vue-router'
import { NAlert, NButton, NCard } from 'naive-ui'

import MainLayout from '@/layouts/MainLayout.vue'
import { useAuthStore } from '@/stores/auth'
import { useOrganizationStore } from '@/stores/organization'

const router = useRouter()
const authStore = useAuthStore()
const organizationStore = useOrganizationStore()
</script>

<template>
  <MainLayout>
    <section class="home-page">
      <n-card class="welcome-card" :bordered="false">
        <h2 class="welcome-title">你好，{{ authStore.currentUser?.username }}</h2>
        <p class="welcome-sub">
          当前企业：
          <strong>{{ organizationStore.currentOrganization?.name }}</strong>
          ，你的角色由后端在每个操作上实时校验，界面仅作展示。
        </p>
        <p class="welcome-sub muted">
          账号创建于 {{ authStore.currentUser?.created_at }}（UTC），用户 ID：
          {{ authStore.currentUser?.user_id }}
        </p>
        <n-button type="primary" data-test="go-chat" @click="router.push({ name: 'chat' })">
          进入客服对话
        </n-button>
      </n-card>

      <n-alert type="info" :show-icon="true" class="module-alert">
        客服对话已开放（会话列表、多轮问答、知识引用与待审批提案展示）；知识库、
        审批中心与成员管理模块将在后续阶段逐步接入。此处仅展示当前登录与企业
        上下文，不包含任何模拟数据。
      </n-alert>
    </section>
  </MainLayout>
</template>

<style scoped>
.home-page {
  max-width: 980px;
  margin: 0 auto;
}

.welcome-card {
  background: var(--sp-color-bg-card);
  box-shadow: var(--sp-shadow-card);
  margin-bottom: var(--sp-space-5);
}

.welcome-title {
  margin: 0 0 var(--sp-space-2);
  font-size: var(--sp-font-size-xl);
  font-weight: 600;
}

.welcome-sub {
  margin: 0 0 var(--sp-space-2);
  color: var(--sp-color-text-2);
}

.welcome-sub.muted {
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.welcome-card :deep(.n-card__content) {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--sp-space-3);
}

.module-alert {
  margin-bottom: var(--sp-space-5);
}
</style>
