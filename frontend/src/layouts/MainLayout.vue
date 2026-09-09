<script setup lang="ts">
/*
 * 主界面布局：顶栏（品牌、企业切换、用户、退出）+ 左侧导航 + 内容区。
 *
 * 说明：
 *  - 客服对话模块已开放并接入路由；知识库、审批、成员管理为后续阶段
 *    模块，目前以「待实现」占位且禁用，不使用假数据；
 *  - 菜单高亮跟随当前路由（工作台 /app、客服对话 /app/chat 前缀）；
 *  - 窄屏（<960px）自动隐藏左侧导航，仅保留顶栏核心操作，避免横向溢出。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NMenu, useDialog, useMessage } from 'naive-ui'
import type { MenuOption } from 'naive-ui'

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

/** 侧栏功能导航：客服对话已开放，其余模块仍标注「待实现」并禁用。 */
const menuOptions: MenuOption[] = [
  {
    label: '工作台',
    key: 'home',
  },
  {
    label: '客服对话',
    key: 'chat',
  },
  {
    label: '知识库（待实现）',
    key: 'knowledge',
    disabled: true,
  },
  {
    label: '审批中心（待实现）',
    key: 'approvals',
    disabled: true,
  },
  {
    label: '成员管理（待实现）',
    key: 'members',
    disabled: true,
  },
]

/** 当前路由对应的高亮菜单 key（与路由 name 映射）。 */
const activeMenuKey = computed(() => {
  if (route.name === 'app') return 'home'
  if (route.name === 'chat' || route.name === 'chat-detail') return 'chat'
  return ''
})

/** 菜单点击：仅已开放的模块会触发导航，待实现项在选项中已禁用。 */
function handleMenuSelect(key: string): void {
  if (key === 'home') {
    void router.push({ name: 'app' })
  } else if (key === 'chat') {
    void router.push({ name: 'chat' })
  }
}

/** 退出登录：先弹确认框，确认后清理本地认证与企业状态并返回登录页。 */
function handleLogout(): void {
  dialog.warning({
    title: '退出登录',
    content: '退出后将清除本地登录状态与当前企业选择；令牌只能等待自然过期。确定退出吗？',
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
    <header class="app-topbar">
      <div class="topbar-brand" aria-label="SupportPilot 客服工作台">
        <span class="brand-mark" aria-hidden="true">SP</span>
        <span class="brand-name">SupportPilot</span>
      </div>

      <div class="topbar-actions">
        <OrganizationSwitcher />
        <n-button
          quaternary
          aria-label="选择企业"
          class="topbar-link-button"
          @click="router.push({ name: 'organizations' })"
        >
          全部企业
        </n-button>
        <span class="topbar-divider" aria-hidden="true" />
        <div class="topbar-user">
          <RoleTag :role="organizationStore.currentRole ?? 'agent'" />
          <span class="topbar-username" :title="displayName">{{ displayName }}</span>
        </div>
        <n-button quaternary aria-label="退出登录" class="topbar-link-button" @click="handleLogout">
          退出登录
        </n-button>
      </div>
    </header>

    <div class="app-body">
      <aside class="app-sider">
        <div class="sider-caption">功能模块</div>
        <n-menu
          :options="menuOptions"
          :value="activeMenuKey"
          @update:value="handleMenuSelect"
        />
        <p class="sider-note">知识库、审批与成员管理将在后续阶段逐步开放。</p>
      </aside>

      <main class="app-content">
        <slot />
      </main>
    </div>
  </div>
</template>

<style scoped>
.app-frame {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

/* —— 顶栏 —— */
.app-topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-4);
  flex-wrap: wrap;
  padding: 0 var(--sp-space-6);
  height: 60px;
  background: var(--sp-color-bg-card);
  border-bottom: 1px solid var(--sp-color-border);
}

.topbar-brand {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  min-width: 0;
}

.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--sp-radius-sm);
  background: var(--sp-color-primary);
  color: #ffffff;
  font-size: var(--sp-font-size-xs);
  font-weight: 600;
}

.brand-name {
  font-size: var(--sp-font-size-lg);
  font-weight: 600;
  color: var(--sp-color-text-1);
}

.topbar-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  min-width: 0;
  flex-wrap: wrap;
}

.topbar-link-button {
  font-size: var(--sp-font-size-sm);
}

.topbar-divider {
  width: 1px;
  height: 18px;
  background: var(--sp-color-border);
  margin: 0 var(--sp-space-2);
}

.topbar-user {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
}

.topbar-username {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-2);
}

/* —— 主体：左导航 + 内容 —— */
.app-body {
  display: flex;
  flex: 1;
  min-height: 0;
}

.app-sider {
  width: 220px;
  flex-shrink: 0;
  padding: var(--sp-space-5) var(--sp-space-4);
  background: var(--sp-color-bg-card);
  border-right: 1px solid var(--sp-color-border);
}

.sider-caption {
  padding: 0 var(--sp-space-3) var(--sp-space-2);
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
  letter-spacing: 1px;
}

.sider-note {
  margin: var(--sp-space-4) var(--sp-space-3) 0;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
  line-height: 1.6;
}

.app-content {
  flex: 1;
  min-width: 0;
  padding: var(--sp-space-6);
}

/* —— 窄屏（常见移动端宽度）：隐藏侧栏，内容占满 —— */
@media (max-width: 959px) {
  .app-topbar {
    padding: 0 var(--sp-space-4);
    height: auto;
    padding-top: var(--sp-space-2);
    padding-bottom: var(--sp-space-2);
  }

  .app-sider {
    display: none;
  }

  .app-content {
    padding: var(--sp-space-4);
  }
}
</style>
