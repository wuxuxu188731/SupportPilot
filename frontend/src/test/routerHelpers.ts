/*
 * 路由相关测试助手：
 *  - 预加载全部懒加载路由组件：懒组件首次被导航时才会动态 import，
 *    其加载可能跨事件循环，导致单次 flushPromises 后导航仍未完成；
 *    提前预加载后，测试内的程序化跳转可在微任务内完成；
 *  - flushNavigation：冲刷多次微任务与宏任务，等待导航落定。
 */

import { flushPromises } from '@vue/test-utils'

/** 预加载所有懒加载的路由页面组件（不渲染，只让模块进入 Vite 缓存）。 */
export async function preloadRouteComponents(): Promise<void> {
  await Promise.all([
    import('@/views/LoginView.vue'),
    import('@/views/RegisterView.vue'),
    import('@/views/OrganizationsView.vue'),
    import('@/views/AppHomeView.vue'),
    import('@/views/ApprovalListView.vue'),
    import('@/views/ApprovalDetailView.vue'),
  ])
}

/** 冲刷导航：交替刷新微任务与宏任务队列，直到导航彻底落定。 */
export async function flushNavigation(): Promise<void> {
  await preloadRouteComponents()
  for (let i = 0; i < 5; i++) {
    await flushPromises()
    await new Promise<void>((resolve) => setTimeout(resolve, 0))
  }
}
