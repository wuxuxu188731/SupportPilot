/*
 * 路由配置与守卫。
 *
 * 路由约定：
 *  - /login、/register：仅未登录用户可访问（guestOnly）；
 *  - /organizations：需要登录（企业选择/创建页）；
 *  - /app：需要登录且已选择有效企业（主界面）。
 *
 * 守卫要点：
 *  - 每次导航先等待认证初始化完成（ensureInitialized），页面刷新期间不会
 *    因登录态未就绪而误跳转；
 *  - 未登录访问受保护页面时记录原始地址到 redirect query，登录并选择
 *    企业后尽量返回原目标；
 *  - 已登录但无有效当前企业时不允许进入 /app。
 */

import { createRouter, createWebHistory, type Router } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { useOrganizationStore } from '@/stores/organization'

const routes = [
  {
    path: '/',
    name: 'root',
    // 根路径默认进入主界面；未登录/无企业时由守卫改道
    redirect: { name: 'app' },
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { guestOnly: true, title: '登录' },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/RegisterView.vue'),
    meta: { guestOnly: true, title: '注册' },
  },
  {
    path: '/organizations',
    name: 'organizations',
    component: () => import('@/views/OrganizationsView.vue'),
    meta: { requiresAuth: true, title: '选择企业' },
  },
  {
    path: '/app',
    name: 'app',
    component: () => import('@/views/AppHomeView.vue'),
    meta: { requiresAuth: true, requiresOrganization: true, title: '工作台' },
  },
  {
    // 未匹配路径统一回根路径，由守卫决定去向
    path: '/:pathMatch(.*)*',
    redirect: { name: 'root' },
  },
]

/** 创建应用路由（测试中可改用内存历史）。 */
export function createAppRouter(history = createWebHistory()): Router {
  return createRouter({ history, routes })
}

/** 组装跳转参数：把原目标地址写入 query.redirect（仅站内路径）。 */
function withRedirectQuery(fullPath: string): { name: string; query: { redirect: string } } {
  return { name: 'login', query: { redirect: fullPath } }
}

/**
 * 注册全局守卫（需在 pinia 激活后调用一次）。
 * 守卫内通过 store 判断登录态与企业上下文，全部为异步等待式，避免闪烁。
 */
export function registerGuards(router: Router): void {
  router.beforeEach(async (to) => {
    const authStore = useAuthStore()
    const organizationStore = useOrganizationStore()

    // 1. 等待认证初始化完成：页面刷新期间避免误判未登录
    await authStore.ensureInitialized()

    // 2. 受保护页面：未登录 → 登录页并记录原始目标
    if (to.meta.requiresAuth && !authStore.isLoggedIn) {
      return withRedirectQuery(to.fullPath)
    }

    // 3. 登录/注册页：已登录用户合理改道（有企业回主界面，无企业去选择页）
    if (to.meta.guestOnly && authStore.isLoggedIn) {
      await organizationStore.ensureLoaded()
      if (organizationStore.hasCurrentOrganization) {
        return { name: 'app' }
      }
      return { name: 'organizations' }
    }

    // 4. 主界面：已登录但没有有效企业时先去选择页（保留原目标）
    if (to.meta.requiresOrganization && authStore.isLoggedIn) {
      await organizationStore.ensureLoaded()
      if (!organizationStore.hasCurrentOrganization) {
        return { name: 'organizations', query: { redirect: to.fullPath } }
      }
    }

    // 其余情况放行
    return true
  })
}
