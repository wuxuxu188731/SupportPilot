/*
 * 认证 Store 测试：登录、登录态恢复、401 清理、退出登录。
 * 通过 mock API 模块进行，不依赖真实后端或网络。
 */

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'
import { AUTH_STORAGE_KEY, ORGANIZATION_STORAGE_KEY } from '@/stores/persistence'
import { useAuthStore } from '@/stores/auth'
import { useOrganizationStore } from '@/stores/organization'

// mock 认证 API：测试不触达网络
vi.mock('@/api/auth', () => ({
  login: vi.fn(),
  register: vi.fn(),
  me: vi.fn(),
}))

// mock 企业 API：退出登录场景需要企业 store 实例（其 API 不应被调用）
vi.mock('@/api/organization', () => ({
  listOrganizations: vi.fn(),
  createOrganization: vi.fn(),
}))

import * as authApi from '@/api/auth'

/** 模拟的用户信息（与后端 UserResponse 一致）。 */
const USER = { user_id: 'u-1', username: 'alice', created_at: '2026-09-01 00:00:00' }

/** 往本地存储写入一组未过期的令牌（等价于曾经登录成功）。 */
function seedValidAuth(token = 'token-1'): void {
  window.localStorage.setItem(
    AUTH_STORAGE_KEY,
    JSON.stringify({ accessToken: token, tokenType: 'bearer', expiresIn: 1800, savedAt: Date.now() }),
  )
}

/** 构造已过期的本地令牌（savedAt 很早，expiresIn 很小）。 */
function seedExpiredAuth(): void {
  window.localStorage.setItem(
    AUTH_STORAGE_KEY,
    JSON.stringify({
      accessToken: 'stale-token',
      tokenType: 'bearer',
      expiresIn: 1,
      savedAt: Date.now() - 10_000,
    }),
  )
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('登录', () => {
  it('登录成功后保存 Token 到状态与本地存储，并拉取当前用户', async () => {
    // 保护行为：登录 = 签发令牌 + 获取用户信息，两者完成才算登录态
    vi.mocked(authApi.login).mockResolvedValue({
      access_token: 'token-abc',
      token_type: 'bearer',
      expires_in: 1800,
    })
    vi.mocked(authApi.me).mockResolvedValue(USER)

    const store = useAuthStore()
    await store.login('alice', 'correct-password')

    expect(authApi.login).toHaveBeenCalledWith({ username: 'alice', password: 'correct-password' })
    expect(store.accessToken).toBe('token-abc')
    expect(store.currentUser).toEqual(USER)
    expect(store.isLoggedIn).toBe(true)
    expect(store.loading).toBe(false)
    // Token 持久化成功
    const persisted = JSON.parse(window.localStorage.getItem(AUTH_STORAGE_KEY) ?? '{}')
    expect(persisted.accessToken).toBe('token-abc')
    expect(typeof persisted.savedAt).toBe('number')
  })

  it('登录期间重复提交被拦截：并发调用只发一次登录请求', async () => {
    // 保护行为：store 层对并发登录的兜底（与按钮 loading 双保险）
    let resolveLogin!: (value: { access_token: string; token_type: string; expires_in: number }) => void
    vi.mocked(authApi.login).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveLogin = resolve
        }),
    )
    vi.mocked(authApi.me).mockResolvedValue(USER)

    const store = useAuthStore()
    const first = store.login('alice', 'pw')
    // 首个请求尚未完成时再次调用：应被直接忽略
    await store.login('alice', 'pw')

    expect(authApi.login).toHaveBeenCalledTimes(1)
    resolveLogin({ access_token: 't', token_type: 'bearer', expires_in: 1800 })
    await first
  })

  it('令牌已签发但获取用户失败时回滚：不残留半登录态', async () => {
    // 保护行为：me 失败（网络等）不能留下「有 Token 无用户」的不一致状态
    vi.mocked(authApi.login).mockResolvedValue({
      access_token: 'token-abc',
      token_type: 'bearer',
      expires_in: 1800,
    })
    vi.mocked(authApi.me).mockRejectedValue(new Error('network down'))

    const store = useAuthStore()
    await expect(store.login('alice', 'pw')).rejects.toThrow('network down')

    expect(store.accessToken).toBe('')
    expect(store.currentUser).toBeNull()
    expect(store.isLoggedIn).toBe(false)
    expect(window.localStorage.getItem(AUTH_STORAGE_KEY)).toBeNull()
  })
})

describe('登录状态恢复（/auth/me）', () => {
  it('无本地令牌：直接完成初始化且不发请求', async () => {
    // 保护行为：未登录用户刷新页面不应触发任何网络请求
    const store = useAuthStore()
    await store.ensureInitialized()
    expect(store.isInitialized).toBe(true)
    expect(authApi.me).not.toHaveBeenCalled()
    expect(store.isLoggedIn).toBe(false)
  })

  it('本地令牌未过期且 /auth/me 成功：恢复完整登录态', async () => {
    // 保护行为：页面刷新后凭令牌恢复用户信息（覆盖测试 3）
    seedValidAuth('token-1')
    vi.mocked(authApi.me).mockResolvedValue(USER)

    const store = useAuthStore()
    await store.ensureInitialized()

    expect(store.isInitialized).toBe(true)
    expect(store.currentUser).toEqual(USER)
    expect(store.isLoggedIn).toBe(true)
  })

  it('/auth/me 返回 401：清理令牌与用户状态', async () => {
    // 保护行为：令牌失效后不能继续假装登录（覆盖测试 4）
    seedValidAuth('expired-token')
    vi.mocked(authApi.me).mockRejectedValue(
      new ApiError({ status: 401, code: null, message: '登录已过期，请重新登录', fieldErrors: {}, raw: null }),
    )

    const store = useAuthStore()
    await store.ensureInitialized()

    expect(store.isLoggedIn).toBe(false)
    expect(store.accessToken).toBe('')
    expect(store.currentUser).toBeNull()
    expect(window.localStorage.getItem(AUTH_STORAGE_KEY)).toBeNull()
  })

  it('本地令牌已过期：直接清理，不发 /auth/me 请求', async () => {
    // 边界情况：expires_in/saved_at 可推算出令牌过期时不浪费网络请求
    seedExpiredAuth()
    const store = useAuthStore()
    await store.ensureInitialized()

    expect(authApi.me).not.toHaveBeenCalled()
    expect(store.isLoggedIn).toBe(false)
    expect(window.localStorage.getItem(AUTH_STORAGE_KEY)).toBeNull()
  })

  it('/auth/me 网络错误：保留令牌但标记初始化完成与错误信息', async () => {
    // 边界情况：服务端暂时不可达不能误删用户会话，等待网络恢复后由
    // 下一次 401 统一清理
    seedValidAuth('token-1')
    vi.mocked(authApi.me).mockRejectedValue(new Error('network down'))

    const store = useAuthStore()
    await store.ensureInitialized()

    expect(store.isInitialized).toBe(true)
    expect(store.isLoggedIn).toBe(false)
    expect(store.accessToken).toBe('token-1') // 令牌保留
    expect(store.initError).toContain('无法验证登录状态')
  })
})

describe('退出登录与会话过期', () => {
  it('退出登录清理全部认证与企业状态（本地行为，无后端登出）', async () => {
    // 保护行为：退出 = 清令牌/用户/企业选择/租户缓存（覆盖测试 5）
    seedValidAuth('token-1')
    window.localStorage.setItem(ORGANIZATION_STORAGE_KEY, JSON.stringify({ organizationId: 'org-1' }))
    vi.mocked(authApi.me).mockResolvedValue(USER)

    const store = useAuthStore()
    await store.ensureInitialized()
    expect(store.isLoggedIn).toBe(true)

    await store.logout()

    expect(store.accessToken).toBe('')
    expect(store.currentUser).toBeNull()
    expect(store.isLoggedIn).toBe(false)
    expect(window.localStorage.getItem(AUTH_STORAGE_KEY)).toBeNull()
    expect(window.localStorage.getItem(ORGANIZATION_STORAGE_KEY)).toBeNull()
    // 企业 store 也被整体重置
    const orgStore = useOrganizationStore()
    expect(orgStore.organizations).toEqual([])
    expect(orgStore.currentOrganizationId).toBeNull()
  })

  it('会话过期处理与退出登录一致：仅清理本地状态', async () => {
    // 保护行为：任意接口 401 触发的处理不应调后端登出接口
    seedValidAuth('token-1')
    vi.mocked(authApi.me).mockResolvedValue(USER)

    const store = useAuthStore()
    await store.ensureInitialized()
    await store.handleSessionExpired()

    expect(store.isLoggedIn).toBe(false)
    expect(window.localStorage.getItem(AUTH_STORAGE_KEY)).toBeNull()
  })
})

describe('注册', () => {
  it('注册成功后不产生登录态（后端注册接口不返回 Token）', async () => {
    // 保护行为：注册与登录必须分离；注册成功只返回用户信息
    vi.mocked(authApi.register).mockResolvedValue(USER)

    const store = useAuthStore()
    const user = await store.register('alice', 'correct-password')

    expect(user.username).toBe('alice')
    expect(store.isLoggedIn).toBe(false)
    expect(store.accessToken).toBe('')
    expect(authApi.login).not.toHaveBeenCalled()
  })
})
