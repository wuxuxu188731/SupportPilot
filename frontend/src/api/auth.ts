/*
 * 认证模块 API 封装：/auth/register、/auth/login、/auth/me。
 * 本模块不携带 X-Organization-ID（后端认证接口不依赖租户上下文）。
 */

import { httpClient } from './http'
import type { LoginRequest, RegisterRequest, TokenResponse, UserInfo } from './types'

/** 注册账号（后端不返回 Token，注册成功后需再调用登录接口）。 */
export async function register(payload: RegisterRequest): Promise<UserInfo> {
  const response = await httpClient.post<UserInfo>('/auth/register', payload)
  return response.data
}

/** 登录并签发访问令牌。 */
export async function login(payload: LoginRequest): Promise<TokenResponse> {
  const response = await httpClient.post<TokenResponse>('/auth/login', payload)
  return response.data
}

/** 查询当前登录用户：用于页面刷新后的登录状态恢复。 */
export async function me(): Promise<UserInfo> {
  const response = await httpClient.get<UserInfo>('/auth/me')
  return response.data
}
