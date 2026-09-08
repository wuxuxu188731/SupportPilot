/*
 * Vitest 全局测试环境准备（setupFiles）：
 *  - 为 jsdom 补齐浏览器 API（ResizeObserver / matchMedia），供 Naive UI
 *    组件在测试中正常挂载；
 *  - 每个测试用例前清空 localStorage，保证测试相互独立。
 */

import { afterEach, beforeEach, vi } from 'vitest'
import { enableAutoUnmount } from '@vue/test-utils'

/** jsdom 缺失 ResizeObserver（Naive UI 下拉/弹层组件依赖），提供空实现桩。 */
class ResizeObserverStub {
  observe(): void {
    /* 空实现：测试环境无需真实观察尺寸 */
  }
  unobserve(): void {
    /* 空实现 */
  }
  disconnect(): void {
    /* 空实现 */
  }
}

/** matchMedia 桩：Naive UI 与响应式逻辑在测试中按「不匹配媒体查询」处理。 */
function createMatchMediaStub(query: string): MediaQueryList {
  return {
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {
      /* 空实现（旧 API 兼容桩） */
    },
    removeListener: () => {
      /* 空实现 */
    },
    addEventListener: () => {
      /* 空实现 */
    },
    removeEventListener: () => {
      /* 空实现 */
    },
    dispatchEvent: () => false,
  } as MediaQueryList
}

// 仅在环境缺失时注入桩，避免覆盖 jsdom 自带实现
if (typeof globalThis.ResizeObserver === 'undefined') {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
}
if (typeof window.matchMedia !== 'function') {
  vi.stubGlobal('matchMedia', createMatchMediaStub)
}
if (typeof globalThis.history === 'undefined' && typeof window !== 'undefined') {
  // vue-router 在浏览器模式直接引用全局 history（window.history）；
  // jsdom 环境下需显式暴露，否则路由初始导航会抛 ReferenceError
  vi.stubGlobal('history', window.history)
}

// 每个测试结束后自动卸载挂载的组件树，避免 naive-ui 消息等异步任务
// 在测试环境销毁后继续访问 document
enableAutoUnmount(afterEach)

beforeEach(() => {
  // 用例间隔离：清空全部本地存储，避免登录态/企业选择相互污染
  window.localStorage.clear()
})
