/*
 * 应用入口：组装 Pinia、Router 与全局 401 处理。
 *
 * 时序说明：
 *  - 先安装 Pinia（成为 active），再注册路由守卫；
 *  - 等待 router.isReady()：首次导航会完整执行守卫（内部等待认证初始化），
 *    因此应用挂载时不会出现「先渲染登录页再跳走」的闪烁；
 *  - setUnauthorizedHandler 注册 401 全局回调：清除认证/企业状态后跳登录页，
 *    并携带 reason=expired 供登录页提示「登录已过期」。
 */

import { createPinia } from 'pinia'
import { createApp } from 'vue'

import { setUnauthorizedHandler } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { createAppRouter, registerGuards } from '@/router'

import App from './App.vue'
import './styles/tokens.css'

/** 应用引导：统一入口避免顶层 await（浏览器构建目标不支持）。 */
async function bootstrap(): Promise<void> {
  const app = createApp(App)
  const pinia = createPinia()
  app.use(pinia)

  const router = createAppRouter()
  registerGuards(router)
  app.use(router)

  // 注册 401 统一处理：除登录接口自身外，任何接口返回 401 都会触发
  // （清理本地认证与企业状态，并跳转登录页提示「登录已过期」）。
  const authStore = useAuthStore(pinia)
  setUnauthorizedHandler(() => {
    void (async () => {
      await authStore.handleSessionExpired()
      const current = router.currentRoute.value
      const alreadyOnAuthPage = current.name === 'login' || current.name === 'register'
      if (!alreadyOnAuthPage) {
        await router.replace({ name: 'login', query: { reason: 'expired' } })
      }
    })()
  })

  // 等待首个导航（含守卫中的认证初始化）完成后再挂载，避免刷新闪烁
  await router.isReady()
  app.mount('#app')
}

void bootstrap()
