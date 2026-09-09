/*
 * 路由守卫测试：登录态与企业上下文对导航的约束。
 * 场景覆盖：未登录访问受保护页面、无企业不能进主界面、已登录访问
 * 登录/注册页的改道、原始目标地址（redirect）记录。
 */

import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter, registerGuards } from '@/router'
import { AUTH_STORAGE_KEY, ORGANIZATION_STORAGE_KEY } from '@/stores/persistence'

// mock API 模块：守卫内部的 store 恢复/加载不触达网络
vi.mock('@/api/auth', () => ({
  login: vi.fn(),
  register: vi.fn(),
  me: vi.fn(),
}))
vi.mock('@/api/organization', () => ({
  listOrganizations: vi.fn(),
  createOrganization: vi.fn(),
}))

import * as authApi from '@/api/auth'
import * as organizationApi from '@/api/organization'

/** 模拟当前用户。 */
const USER = { user_id: 'u-1', username: 'alice', created_at: '2026-09-01 00:00:00' }
/** 模拟企业列表。 */
const ORG_LIST = [{ organization_id: 'org-1', name: '示例企业', role: 'admin' as const }]

/** 构造一套已登录（令牌有效 + /auth/me 成功）的默认环境。 */
async function setupLoggedInRouter(options: { withOrganization?: boolean; organizationId?: string } = {}) {
  window.localStorage.setItem(
    AUTH_STORAGE_KEY,
    JSON.stringify({ accessToken: 'token-1', tokenType: 'bearer', expiresIn: 1800, savedAt: Date.now() }),
  )
  vi.mocked(authApi.me).mockResolvedValue(USER)
  if (options.withOrganization) {
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue(ORG_LIST)
    window.localStorage.setItem(
      ORGANIZATION_STORAGE_KEY,
      JSON.stringify({ organizationId: options.organizationId ?? 'org-1' }),
    )
  } else {
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([])
  }

  const router = createAppRouter(createMemoryHistory())
  registerGuards(router)
  return router
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('未登录用户的导航约束', () => {
  it('未登录访问 /app：跳转登录页并记录原始目标', async () => {
    // 保护行为：受保护页面不能被未登录用户进入（覆盖测试 11）
    vi.mocked(authApi.me).mockResolvedValue(USER) // 兜底：即便有残留也不影响
    const router = createAppRouter(createMemoryHistory())
    registerGuards(router)

    await router.push('/app')

    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/app')
  })

  it('未登录访问 /organizations：同样跳转登录页并记录目标', async () => {
    // 保护行为：企业选择页同样属于登录后页面
    const router = createAppRouter(createMemoryHistory())
    registerGuards(router)

    await router.push('/organizations')

    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/organizations')
  })

  it('未登录访问 /app/chat 与会话详情：同样跳转登录页并记录目标', async () => {
    // 保护行为：客服对话路由必须沿用与 /app 相同的认证守卫
    const router = createAppRouter(createMemoryHistory())
    registerGuards(router)

    await router.push('/app/chat')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/app/chat')

    await router.push('/app/chat/conv-1')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/app/chat/conv-1')
  })

  it('未登录访问 /app/approvals 与审批详情：同样跳转登录页并记录目标', async () => {
    // 保护行为：审批中心与审批详情路由必须沿用与 /app 相同的认证守卫
    const router = createAppRouter(createMemoryHistory())
    registerGuards(router)

    await router.push('/app/approvals')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/app/approvals')

    await router.push('/app/approvals/a-1')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/app/approvals/a-1')
  })

  it('未登录访问 /app/knowledge 与文档详情：同样跳转登录页并记录目标', async () => {
    // 保护行为：知识库与文档详情路由必须沿用与 /app 相同的认证守卫
    const router = createAppRouter(createMemoryHistory())
    registerGuards(router)

    await router.push('/app/knowledge')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/app/knowledge')

    await router.push('/app/knowledge/doc-1')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/app/knowledge/doc-1')
  })

  it('未登录访问 /login 与 /register：直接放行', async () => {
    // 边界情况：公开页面不得被守卫拦截
    const router = createAppRouter(createMemoryHistory())
    registerGuards(router)

    await router.push('/login')
    expect(router.currentRoute.value.name).toBe('login')

    await router.push('/register')
    expect(router.currentRoute.value.name).toBe('register')
  })
})

describe('登录用户访问公开页的改道', () => {
  it('已登录且有企业：访问 /login 自动回主界面', async () => {
    // 保护行为：已登录用户不应停留在登录页（覆盖需求「已登录合理跳转」）
    const router = await setupLoggedInRouter({ withOrganization: true })

    await router.push('/login')

    expect(router.currentRoute.value.name).toBe('app')
  })

  it('已登录但无企业：访问 /login 去企业选择页', async () => {
    // 边界情况：没有企业时不能直接进主界面，应引导创建/选择企业
    const router = await setupLoggedInRouter({ withOrganization: false })

    await router.push('/login')

    expect(router.currentRoute.value.name).toBe('organizations')
  })
})

describe('主界面的企业上下文约束', () => {
  it('已登录但没有选择企业：不能进入 /app，改道企业选择页并记录目标', async () => {
    // 保护行为：没有企业上下文不能进入主界面（覆盖测试 12）
    const router = await setupLoggedInRouter({ withOrganization: false })

    await router.push('/app')

    expect(router.currentRoute.value.name).toBe('organizations')
    expect(router.currentRoute.value.query.redirect).toBe('/app')
  })

  it('已登录但没有选择企业：不能进入 /app/chat/:conversationId', async () => {
    // 边界情况：客服对话页与会话详情同样要求企业上下文（防止旧企业会话残留）
    const router = await setupLoggedInRouter({ withOrganization: false })

    await router.push('/app/chat/conv-1')

    expect(router.currentRoute.value.name).toBe('organizations')
    expect(router.currentRoute.value.query.redirect).toBe('/app/chat/conv-1')
  })

  it('已登录但没有选择企业：不能直接访问审批详情 URL', async () => {
    // 边界情况：审批中心与审批详情必须要求企业上下文（直接访问详情URL场景）
    const router = await setupLoggedInRouter({ withOrganization: false })

    await router.push('/app/approvals/a-1')

    expect(router.currentRoute.value.name).toBe('organizations')
    expect(router.currentRoute.value.query.redirect).toBe('/app/approvals/a-1')
  })

  it('已登录但没有选择企业：不能直接访问知识库详情 URL', async () => {
    // 边界情况：知识库与文档详情必须要求企业上下文（直接访问详情URL场景）
    const router = await setupLoggedInRouter({ withOrganization: false })

    await router.push('/app/knowledge/doc-1')

    expect(router.currentRoute.value.name).toBe('organizations')
    expect(router.currentRoute.value.query.redirect).toBe('/app/knowledge/doc-1')
  })

  it('已登录且本地企业仍有效：直接进入知识库列表（深链放行）', async () => {
    // 保护行为：刷新页面后本地企业选择有效时，知识库深链可放行
    const router = await setupLoggedInRouter({ withOrganization: true })

    await router.push('/app/knowledge/doc-1')

    expect(router.currentRoute.value.name).toBe('knowledge-detail')
  })

  it('已登录且本地企业仍有效：直接进入审批中心（刷新后恢复）', async () => {
    // 保护行为：刷新页面后本地企业选择有效时，审批中心深链可放行
    const router = await setupLoggedInRouter({ withOrganization: true })

    await router.push('/app/approvals')

    expect(router.currentRoute.value.name).toBe('approvals')
  })

  it('已登录且本地企业仍有效：直接进入 /app（刷新后恢复）', async () => {
    // 保护行为：刷新页面后本地企业选择有效时守卫放行主界面
    const router = await setupLoggedInRouter({ withOrganization: true })

    await router.push('/app')

    expect(router.currentRoute.value.name).toBe('app')
  })

  it('本地令牌已过期：访问 /app 重新回登录页', async () => {
    // 边界情况：过期令牌不得进入受保护页面，且不调用 /auth/me
    window.localStorage.setItem(
      AUTH_STORAGE_KEY,
      JSON.stringify({
        accessToken: 'stale',
        tokenType: 'bearer',
        expiresIn: 1,
        savedAt: Date.now() - 10_000,
      }),
    )
    const router = createAppRouter(createMemoryHistory())
    registerGuards(router)

    await router.push('/app')

    expect(router.currentRoute.value.name).toBe('login')
    expect(authApi.me).not.toHaveBeenCalled()
  })
})

describe('初始化等待', () => {
  it('无本地令牌时初始化同步完成：守卫不发起网络请求也不闪跳', async () => {
    // 保护行为：页面刷新时守卫须等认证初始化（含 /auth/me）完成后再决定
    // 去向；无令牌场景不应有任何网络往返，避免闪烁与错误重定向
    const router = createAppRouter(createMemoryHistory())
    registerGuards(router)

    await router.push('/app')

    expect(router.currentRoute.value.name).toBe('login')
    expect(authApi.me).not.toHaveBeenCalled()
  })

  it('有本地令牌时守卫等待 /auth/me 完成才放行，不提前改道', async () => {
    // 保护行为：令牌存在时 /auth/me 结果决定去向，导航不得先于请求完成
    let resolveMe!: (value: typeof USER) => void
    vi.mocked(authApi.me).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveMe = resolve
        }),
    )
    window.localStorage.setItem(
      AUTH_STORAGE_KEY,
      JSON.stringify({ accessToken: 'token-1', tokenType: 'bearer', expiresIn: 1800, savedAt: Date.now() }),
    )
    const router = createAppRouter(createMemoryHistory())
    registerGuards(router)

    const navigation = router.push('/app')
    // /auth/me 尚未返回时，导航必须处于进行中（守卫等待中）
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(router.currentRoute.value.name).not.toBe('login')

    resolveMe(USER)
    await navigation
  })
})
