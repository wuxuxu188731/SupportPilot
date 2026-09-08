/*
 * 统一 HTTP 客户端（基于 Axios）。
 *
 * 职责：
 *  1. 自动附加 Authorization: Bearer <token>（从本地持久化读取，避免与
 *     Pinia Store 循环依赖）；
 *  2. 租户接口自动附加 X-Organization-ID（判定规则见 isTenantRequest）；
 *  3. 把后端三种错误形态统一转换为 ApiError 后原样抛出（不吞错误）；
 *  4. 除登录请求自身外的 401 统一触发「登录过期」回调（由应用层注册）。
 *
 * 退出登录只清理前端状态（后端无服务端 logout / refresh token），
 * 因此本文件不调用任何登出接口。
 */

import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'

import { ApiError, toApiError, toApiErrorFromThrowable } from './errors'
import { readAuthStorage, readOrganizationStorage } from '@/stores/persistence'

/** 环境变量配置的 API 基础地址；开发环境默认使用 /api（由 Vite 代理转发）。 */
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api'

/** 是否为「登录」请求：其 401 属于凭据错误，不应触发全局登出跳转。 */
const LOGIN_PATH_PATTERN = /^\/auth\/login$/i

/** 是否为租户接口：除 /auth/* 与 /organizations/* 外的所有接口都需要
 *  X-Organization-ID 请求头（与后端依赖 get_current_tenant 的范围一致）。 */
export function isTenantRequestPath(requestPath: string): boolean {
  // requestPath 为带 baseURL 前缀的完整请求路径（如 /api/conversations/）
  const path = requestPath.replace(API_BASE_URL, '').split('?')[0]
  if (path.startsWith('/auth/') || path.startsWith('/organizations/')) {
    return false
  }
  return path.startsWith('/conversations') ||
    path.startsWith('/knowledge') ||
    path.startsWith('/approvals') ||
    path.startsWith('/action-runs')
}

/** 请求拦截器附加逻辑：可独立测试的纯函数。 */
export function applyRequestCredentials(config: InternalAxiosRequestConfig): InternalAxiosRequestConfig {
  const auth = readAuthStorage()
  if (auth) {
    // 附加 Bearer Token（后端为 JWT，header 名不区分大小写）
    config.headers.set('Authorization', `Bearer ${auth.accessToken}`)
  }
  if (isTenantRequestPath(config.url ?? '')) {
    const organizationId = readOrganizationStorage()
    if (organizationId) {
      // 附加租户头：缺少该头的租户请求会被后端拒绝（400）
      config.headers.set('X-Organization-ID', organizationId)
    }
  }
  return config
}

/** 响应失败归一化：任意 Axios 错误 → ApiError（不吞掉，继续抛出）。 */
export function toApiErrorFromAxiosError(error: AxiosError): ApiError {
  const status = error.response?.status ?? 0
  const body = error.response?.data
  if (status === 0) {
    return toApiErrorFromThrowable(error)
  }
  return new ApiError(toApiError(status, body, error))
}

/** 统一处理 401 的回调；由应用入口在 store 就绪后注册。 */
let unauthorizedHandler: (() => void) | null = null

/** 注册「登录过期」回调（应用初始化时由 router/auth store 注册一次）。 */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

function createHttpClient(): AxiosInstance {
  const client = axios.create({
    baseURL: API_BASE_URL,
    timeout: 60_000,
  })

  client.interceptors.request.use(applyRequestCredentials)

  client.interceptors.response.use(
    // 成功响应直接放行，不做任何改写
    (response) => response,
    (error: AxiosError) => {
      if (axios.isAxiosError(error)) {
        // 401：除登录接口自身外，一律触发全局「登录过期」处理
        // （清除 Token/用户/企业并跳转登录页，见 auth store 的 handleSessionExpired）
        const status = error.response?.status ?? 0
        const requestUrl = error.config?.url ?? ''
        const isLoginRequest = status === 401 && LOGIN_PATH_PATTERN.test(requestUrl.replace(API_BASE_URL, ''))
        if (status === 401 && !isLoginRequest) {
          unauthorizedHandler?.()
        }
      }
      // 统一转换为 ApiError 后继续抛出，页面负责展示有意义的失败状态
      return Promise.reject(toApiErrorFromAxiosError(error))
    },
  )
  return client
}

/** 共享的 Axios 实例：API 模块通过它发起请求。 */
export const httpClient = createHttpClient()
