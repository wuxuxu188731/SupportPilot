<script setup lang="ts">
/*
 * 主界面布局：深色品牌侧栏、企业与用户顶栏、业务内容区。
 *
 * 说明：
 *  - 工作台、客服对话、审批中心、知识库与成员管理模块均已开放并接入路由，
 *    不使用假数据；
 *  - 菜单高亮跟随当前路由（工作台 /app、客服对话 /app/chat 前缀）；
 *  - 窄屏（<960px）自动隐藏左侧导航，仅保留顶栏核心操作，避免横向溢出。
 */
import { computed, h } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NMenu, useDialog, useMessage } from 'naive-ui'
import type { MenuOption } from 'naive-ui'

import AppIcon from '@/components/common/AppIcon.vue'
import RoleTag from '@/components/common/RoleTag.vue'
import OrganizationSwitcher from '@/components/organization/OrganizationSwitcher.vue'
import { useAuthStore } from '@/stores/auth'
import { useOrganizationStore } from '@/stores/organization'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const authStore = useAuthStore()
const organizationStore = useOrganizationStore()

/** 当前用户显示名。 */
const displayName = computed(() => authStore.currentUser?.username ?? '')

/** 侧栏功能导航：工作台、客服对话、审批中心、知识库与成员管理均已开放。 */
const menuOptions: MenuOption[] = [
  {
    label: '工作台',
    key: 'home',
    icon: () => h(AppIcon, { name: 'home' }),
  },
  {
    label: '客服对话',
    key: 'chat',
    icon: () => h(AppIcon, { name: 'chat' }),
  },
  {
    label: '审批中心',
    key: 'approvals',
    icon: () => h(AppIcon, { name: 'approvals' }),
  },
  {
    label: '知识库',
    key: 'knowledge',
    icon: () => h(AppIcon, { name: 'knowledge' }),
  },
  {
    label: '成员管理',
    key: 'members',
    icon: () => h(AppIcon, { name: 'members' }),
  },
]

/** 当前路由对应的高亮菜单 key（与路由 name 映射）。 */
const activeMenuKey = computed(() => {
  if (route.name === 'app') return 'home'
  if (route.name === 'chat' || route.name === 'chat-detail') return 'chat'
  if (route.name === 'approvals' || route.name === 'approval-detail') return 'approvals'
  if (route.name === 'knowledge' || route.name === 'knowledge-detail') return 'knowledge'
  if (route.name === 'members') return 'members'
  return ''
})

/** 顶栏面包屑与当前导航保持一致。 */
const activePageLabel = computed(() => menuOptions.find((item) => item.key === activeMenuKey.value)?.label ?? '工作台')

/** 菜单点击：在各功能模块之间导航。 */
function handleMenuSelect(key: string): void {
  if (key === 'home') {
    void router.push({ name: 'app' })
  } else if (key === 'chat') {
    void router.push({ name: 'chat' })
  } else if (key === 'approvals') {
    void router.push({ name: 'approvals' })
  } else if (key === 'knowledge') {
    void router.push({ name: 'knowledge' })
  } else if (key === 'members') {
    void router.push({ name: 'members' })
  }
}

/** 退出登录：先弹确认框，确认后清理本地认证与企业状态并返回登录页。 */
function handleLogout(): void {
  dialog.warning({
    title: '退出登录',
    content: '退出后需要重新登录才能继续使用当前工作空间。确定退出吗？',
    positiveText: '退出登录',
    negativeText: '取消',
    onPositiveClick: async () => {
      await authStore.logout()
      message.success('已退出登录')
      await router.replace({ name: 'login' })
    },
  })
}
</script>

<template>
  <div class="app-frame">
    <aside class="app-sider">
      <router-link :to="{ name: 'app' }" class="topbar-brand" aria-label="SupportPilot 客服工作台">
        <span class="brand-mark"><AppIcon name="spark" :size="25" /></span>
        <span class="brand-name">SupportPilot<small>智能客服工作台</small></span>
      </router-link>
      <div class="sider-caption">工作空间 <span>WORKSPACE</span></div>
      <nav aria-label="功能导航">
        <n-menu :options="menuOptions" :value="activeMenuKey" :indent="16" :icon-size="19" @update:value="handleMenuSelect" />
      </nav>
      <div class="sider-bottom">
        <div class="sider-tip"><AppIcon name="spark" :size="22" /><strong>每一次服务，都更进一步</strong><p>让 AI 连接知识与协作，<br />让团队专注有温度的沟通。</p><router-link :to="{ name: 'chat' }">开启一段对话 <AppIcon name="arrow" :size="15" /></router-link></div>
        <div class="sider-footer"><span class="footer-dot" /> SupportPilot <span>服务，有章可循</span></div>
      </div>
    </aside>
    <div class="app-body">
      <header class="app-topbar">
        <div class="topbar-breadcrumb"><span>工作空间</span><span class="breadcrumb-slash">/</span><strong>{{ activePageLabel }}</strong></div>
        <div class="topbar-actions">
          <OrganizationSwitcher />
          <n-button quaternary aria-label="选择企业" class="topbar-link-button" @click="router.push({ name: 'organizations' })">全部企业</n-button>
          <span class="topbar-divider" aria-hidden="true" />
          <div class="topbar-user">
            <span class="user-avatar" aria-hidden="true">{{ displayName.slice(0, 1).toUpperCase() }}</span>
            <span class="topbar-username" :title="displayName">{{ displayName }}</span>
            <RoleTag :role="organizationStore.currentRole ?? 'agent'" />
          </div>
          <n-button quaternary aria-label="退出登录" title="退出登录" class="logout-button" @click="handleLogout"><AppIcon name="logout" :size="18" /></n-button>
        </div>
      </header>
      <main class="app-content"><slot /></main>
    </div>
  </div>
</template>

<style scoped>
.app-frame { display: flex; min-height: 100vh; }
.app-sider { position: sticky; top: 0; display: flex; flex-direction: column; width: 232px; height: 100vh; flex-shrink: 0; padding: 30px 16px 20px; color: #b9cbbf; background: #203e34; }
.topbar-brand { display: flex; align-items: center; gap: 11px; margin: 0 11px 45px; color: #f4f7ed; text-decoration: none; }
.brand-mark { display: grid; place-items: center; width: 37px; height: 37px; flex-shrink: 0; border-radius: 10px; color: #dce9b0; background: #b9d6a018; border: 1px solid #aec99740; }
.brand-name { font-size: 21px; font-weight: 600; letter-spacing: -.6px; }
.brand-name small { display: block; margin-top: 2px; font-size: 9px; color: #9fb6a6; font-weight: 400; letter-spacing: 2px; }
.sider-caption { display: flex; justify-content: space-between; padding: 0 16px 9px; font-size: 10px; letter-spacing: 1px; color: #a4b9a9; }.sider-caption span { font-size: 8px; letter-spacing: 1.2px; opacity: .7; }
.app-sider :deep(.n-menu-item) { height: 46px; margin-top: 7px; }
.app-sider :deep(.n-menu-item-content) { height: 46px; }
.app-sider :deep(.n-menu-item-content--selected)::after { content: ''; position: absolute; right: 13px; width: 5px; height: 5px; background: #d7e8a5; border-radius: 50%; }
.sider-bottom { margin-top: auto; padding-top: 44px; }.sider-tip { padding: 19px 15px; border: 1px solid #90b49828; border-radius: 11px; background: linear-gradient(135deg, #54725136, #5472510c); }.sider-tip > .app-icon { color: #d5e4a2; margin-bottom: 10px; }.sider-tip strong { display: block; font-size: 12px; font-weight: 500; color: #e1ead9; }.sider-tip p { margin: 9px 0 17px; font-size: 11px; line-height: 1.9; color: #aac0b0; }.sider-tip a { display: flex; align-items: center; justify-content: space-between; font-size: 11px; color: #dce8bc; text-decoration: none; }.sider-footer { display: flex; align-items: center; gap: 6px; margin: 24px 3px 0; font-size: 10px; color: #b2c5b6; }.sider-footer > span:last-child { margin-left: auto; font-size: 9px; color: #92aa9b; }.footer-dot { width: 5px; height: 5px; border-radius: 50%; background: #b6c992; }
.app-body { display: flex; flex-direction: column; flex: 1; min-width: 0; }
.app-topbar { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; justify-content: space-between; gap: 20px; min-height: 72px; padding: 12px 34px; background: #ffffffed; border-bottom: 1px solid var(--sp-color-border); backdrop-filter: blur(12px); }
.topbar-breadcrumb { display: flex; align-items: center; gap: 13px; flex-shrink: 0; font-size: 12px; color: var(--sp-color-text-3); }.breadcrumb-slash { color: #bcc4b6; }.topbar-breadcrumb strong { color: var(--sp-color-text-1); font-weight: 500; }
.topbar-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; min-width: 0; }.topbar-link-button { font-size: 12px; }.topbar-divider { height: 24px; width: 1px; margin: 0 10px; background: var(--sp-color-border); }
.topbar-user { display: flex; align-items: center; gap: 9px; }.user-avatar { display: grid; place-items: center; width: 31px; height: 31px; border: 1px solid #dde5ce; border-radius: 50%; font-size: 12px; font-weight: 600; background: #edf1df; color: #5f7244; }.topbar-username { max-width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }.logout-button { color: var(--sp-color-text-3); }
.app-content { flex: 1; min-width: 0; padding: 30px 34px; }
@media (min-width: 1600px) { .app-sider { width: 252px; }.app-content { padding: 38px 48px; }.app-topbar { padding-inline: 48px; } }
@media (max-width: 1100px) { .app-sider { width: 210px; padding-inline: 12px; }.brand-name { font-size: 18px; }.topbar-breadcrumb > span:first-child, .breadcrumb-slash { display: none; }.app-topbar { padding-inline: 22px; }.app-content { padding: 26px 22px; } }
@media (max-width: 959px) { .app-sider { display: none; }.app-topbar { flex-wrap: wrap; }.topbar-actions { flex-wrap: wrap; }.app-content { padding: 20px; } }
</style>
