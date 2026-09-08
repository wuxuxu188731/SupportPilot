/*
 * 认证 Store（Pinia）：管理访问令牌、登录态恢复、退出登录。
 *
 * 事实说明（写进界面与文档）：
 *  - 后端没有 refresh token，也没有服务端 logout 接口，因此「退出登录」
 *    只清理前端本地状态；令牌只能等待自然过期；
 *  - Token 持久化在 localStorage（XSS 风险边界见 frontend/README.md）；
 *  - 角色权限的真正校验在后端，本 Store 保存的用户信息只用于界面展示。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import * as authApi from '@/api/auth'
import type { UserInfo } from '@/api/types'
import {
  clearAuthStorage,
  readAuthStorage,
  writeAuthStorage,
} from '@/stores/persistence'
import { runTenantResetHandlers } from '@/stores/tenantReset'
import { useOrganizationStore } from '@/stores/organization'

export const useAuthStore = defineStore('auth', () => {
  // —— 状态 ——

  /** 访问令牌（JWT）；空字符串表示未登录 */
  const accessToken = ref('')
  /** 令牌类型：后端返回 "bearer" */
  const tokenType = ref('')
  /** 令牌有效秒数（后端配置决定） */
  const expiresIn = ref(0)
  /** 令牌本地保存时间（epoch 毫秒），用于推导过期时刻 */
  const savedAt = ref(0)
  /** 当前登录用户（登录成功或 /auth/me 恢复成功后填充） */
  const currentUser = ref<UserInfo | null>(null)
  /** 登录/注册/恢复等请求进行中标志 */
  const loading = ref(false)
  /** 初始化是否已完成（防止页面刷新期间守卫误跳转） */
  const isInitialized = ref(false)
  /** 初始化失败原因（网络等非 401 原因），用于界面提示 */
  const initError = ref<string | null>(null)

  // —— 派生状态 ——

  /** 令牌过期时刻（epoch 毫秒）；无令牌时为 0 */
  const expiresAt = computed(() => (savedAt.value > 0 ? savedAt.value + expiresIn.value * 1000 : 0))

  /** 是否处于登录态：令牌与当前用户都存在才算登录 */
  const isLoggedIn = computed(() => accessToken.value !== '' && currentUser.value !== null)

  /** 令牌是否已在本地判定过期（过期后不再尝试 /auth/me 恢复） */
  const isLocallyExpired = computed(() => {
    if (accessToken.value === '' || savedAt.value <= 0) return false
    return Date.now() >= expiresAt.value
  })

  /** 内部防并发：同一初始化过程只执行一次 */
  let initPromise: Promise<void> | null = null

  // —— 内部工具 ——

  /** 把令牌写入状态与本地存储（登录成功后调用）。 */
  function persistToken(token: { access_token: string; token_type: string; expires_in: number }): void {
    accessToken.value = token.access_token
    tokenType.value = token.token_type
    expiresIn.value = token.expires_in
    savedAt.value = Date.now()
    writeAuthStorage({
      accessToken: token.access_token,
      tokenType: token.token_type,
      expiresIn: token.expires_in,
      savedAt: savedAt.value,
    })
  }

  /** 从本地存储恢复令牌到内存状态（页面刷新时调用）。 */
  function restoreTokenFromStorage(): void {
    const stored = readAuthStorage()
    if (!stored) return
    accessToken.value = stored.accessToken
    tokenType.value = stored.tokenType
    expiresIn.value = stored.expiresIn
    savedAt.value = stored.savedAt
  }

  // —— 对外动作 ——

  /** 注册新账号：成功仅返回用户信息，不产生登录态（后端注册接口不返回 Token）。 */
  async function register(username: string, password: string): Promise<UserInfo> {
    loading.value = true
    try {
      return await authApi.register({ username, password })
    } finally {
      loading.value = false
    }
  }

  /** 登录：签发令牌后立即获取当前用户；任一环节失败都回滚令牌，不残留半登录态。 */
  async function login(username: string, password: string): Promise<void> {
    if (loading.value) return // 表单/按钮已 disabled，这里再兜底防止并发提交
    loading.value = true
    try {
      const token = await authApi.login({ username, password })
      persistToken(token)
      try {
        currentUser.value = await authApi.me()
      } catch (error) {
        // 令牌已签发但获取用户失败（网络等）：回滚到未登录态，避免不一致
        clearSession()
        throw error
      }
    } finally {
      loading.value = false
    }
  }

  /**
   * 登录状态恢复：页面刷新/首次进入时调用一次。
   *  - 无本地令牌 → 直接完成初始化；
   *  - 本地判定过期 → 清理令牌后完成初始化；
   *  - 有令牌 → 调 /auth/me 验证；401 说明令牌失效 → 清理；
   *    网络等非 401 错误 → 保留令牌但记录错误（不误删用户会话）。
   */
  async function restore(): Promise<void> {
    if (isInitialized.value) return
    if (initPromise) return initPromise
    initPromise = doRestore().finally(() => {
      isInitialized.value = true
    })
    return initPromise
  }

  async function doRestore(): Promise<void> {
    const stored = readAuthStorage()
    if (!stored) return
    restoreTokenFromStorage()
    if (Date.now() >= expiresAt.value) {
      // 本地已可判定过期：直接清理，不再消耗一次请求
      clearSession()
      return
    }
    loading.value = true
    initError.value = null
    try {
      currentUser.value = await authApi.me()
    } catch (error) {
      if (error instanceof Error && 'status' in error && (error as { status: number }).status === 401) {
        // 令牌被后端判定无效/过期：清理登录态
        clearSession()
      } else {
        // 网络/服务端暂时不可用：保留令牌，登录态未确认（守卫会引导去登录页）
        initError.value = '无法验证登录状态，请检查网络后重试'
      }
    } finally {
      loading.value = false
    }
  }

  /** 等待初始化完成（路由守卫与页面挂载时使用）。 */
  async function ensureInitialized(): Promise<void> {
    await restore()
  }

  /** 清理令牌与用户状态（不触达后端，见文件头事实说明）。 */
  function clearSession(): void {
    accessToken.value = ''
    tokenType.value = ''
    expiresIn.value = 0
    savedAt.value = 0
    currentUser.value = null
    initError.value = null
    clearAuthStorage()
  }

  /** 退出登录：清理认证态、企业态与全部租户缓存（本地行为）。 */
  async function logout(): Promise<void> {
    const organizationStore = useOrganizationStore()
    await runTenantResetHandlers()
    organizationStore.reset()
    clearSession()
  }

  /** 任意接口返回 401（登录接口除外）时的统一过期处理：同上清理本地状态。 */
  async function handleSessionExpired(): Promise<void> {
    await logout()
  }

  return {
    // 状态
    accessToken,
    tokenType,
    expiresIn,
    savedAt,
    currentUser,
    loading,
    isInitialized,
    initError,
    // 派生
    expiresAt,
    isLoggedIn,
    isLocallyExpired,
    // 动作
    register,
    login,
    restore,
    ensureInitialized,
    logout,
    handleSessionExpired,
    clearSession,
  }
})
