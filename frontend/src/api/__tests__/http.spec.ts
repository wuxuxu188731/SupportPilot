/*
 * HTTP 客户端凭据注入测试：验证认证与租户 Header 的自动附加规则。
 * 保护行为：登录后请求携带 Bearer Token；租户接口携带 X-Organization-ID；
 * /auth/* 与 /organizations/* 等非租户接口不依赖当前企业。
 */

import { AxiosHeaders, type InternalAxiosRequestConfig } from 'axios'
import { beforeEach, describe, expect, it } from 'vitest'

import { applyRequestCredentials, isTenantRequestPath } from '@/api/http'
import {
  AUTH_STORAGE_KEY,
  ORGANIZATION_STORAGE_KEY,
} from '@/stores/persistence'

/** 构造一个带 Headers 的请求配置（模拟 Axios 拦截器收到的对象）。 */
function makeConfig(url: string): InternalAxiosRequestConfig {
  return {
    url,
    headers: new AxiosHeaders(),
  } as InternalAxiosRequestConfig
}

/** 预置本地认证与企业选择（等价于登录成功与选择企业后的存储状态）；
 *  organizationId 传 null 表示尚未选择企业。 */
function seedStorage(token = 'token-1', organizationId: string | null = null): void {
  window.localStorage.setItem(
    AUTH_STORAGE_KEY,
    JSON.stringify({ accessToken: token, tokenType: 'bearer', expiresIn: 1800, savedAt: Date.now() }),
  )
  if (organizationId) {
    window.localStorage.setItem(ORGANIZATION_STORAGE_KEY, JSON.stringify({ organizationId }))
  }
}

describe('isTenantRequestPath：租户接口判定', () => {
  it('会话/知识库/审批/Run 前缀的路径判定为租户接口', () => {
    // 保护行为：需要 X-Organization-ID 的接口范围与后端依赖一致
    expect(isTenantRequestPath('/api/conversations/abc/chat/')).toBe(true)
    expect(isTenantRequestPath('/api/knowledge/documents/')).toBe(true)
    expect(isTenantRequestPath('/api/approvals/?status=pending')).toBe(true)
    expect(isTenantRequestPath('/api/action-runs/r1/resume/')).toBe(true)
  })

  it('/auth/* 与 /organizations/* 判定为非租户接口', () => {
    // 保护行为：认证与企业管理接口不读取企业上下文，不应附加租户头
    expect(isTenantRequestPath('/api/auth/login')).toBe(false)
    expect(isTenantRequestPath('/api/auth/me')).toBe(false)
    expect(isTenantRequestPath('/api/organizations/')).toBe(false)
    expect(isTenantRequestPath('/api/organizations/org-1/members/')).toBe(false)
  })
})

describe('applyRequestCredentials：凭据自动注入', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('有本地令牌时自动附加 Authorization: Bearer 头', () => {
    // 保护行为：登录后所有请求自动携带令牌，页面无需手工拼接
    seedStorage('my-token', null)
    const config = applyRequestCredentials(makeConfig('/api/auth/me'))
    expect(config.headers.get('Authorization')).toBe('Bearer my-token')
  })

  it('租户接口自动附加 X-Organization-ID，且不影响 Bearer', () => {
    // 保护行为：租户接口同时携带认证与企业上下文两个头
    seedStorage('my-token', 'org-99')
    const config = applyRequestCredentials(makeConfig('/api/knowledge/documents/'))
    expect(config.headers.get('Authorization')).toBe('Bearer my-token')
    expect(config.headers.get('X-Organization-ID')).toBe('org-99')
  })

  it('无本地令牌时不附加 Authorization 头', () => {
    // 边界情况：未登录请求（如注册/登录）不能带失效的空 Bearer
    const config = applyRequestCredentials(makeConfig('/api/auth/login'))
    expect(config.headers.get('Authorization')).toBeUndefined()
  })

  it('非租户接口即使有企业选择也不附加 X-Organization-ID', () => {
    // 保护行为：/auth/* 与 /organizations/* 不依赖当前企业（覆盖 7）
    seedStorage('my-token', 'org-1')
    const authMe = applyRequestCredentials(makeConfig('/api/auth/me'))
    expect(authMe.headers.get('X-Organization-ID')).toBeUndefined()
    const orgList = applyRequestCredentials(makeConfig('/api/organizations/'))
    expect(orgList.headers.get('X-Organization-ID')).toBeUndefined()
  })

  it('租户接口但未选择企业时不附加 X-Organization-ID（后端会给出 400）', () => {
    // 边界情况：本地无企业选择时租户请求不带空头，交由后端返回明确错误
    seedStorage('my-token', null)
    const config = applyRequestCredentials(makeConfig('/api/conversations/'))
    expect(config.headers.get('X-Organization-ID')).toBeUndefined()
  })
})
