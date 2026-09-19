/*
 * 演示数据模块 API 封装：POST /demo-data/（为当前企业生成演示业务数据）。
 *
 * 与其它模块的差异（依据 docs/frontend/api-inventory.md 4.6.1）：
 *  - 该接口**必须**读取 X-Organization-ID 请求头，因此 http.ts 的租户判定
 *    已把 /demo-data 纳入租户前缀；
 *  - 调用方可以显式传入企业 id —— 「选择企业」页上用户可能还没进入任何企业
 *    （此时本地存储里没有当前企业），未显式传入时才回退到「当前企业」；
 *  - 请求无响应体：不要传 organization_id / user_id / role 等字段，
 *    企业与调用者身份全部由服务端从认证链取得。
 */

import { httpClient } from './http'
import { readAuthStorage, readOrganizationStorage } from '@/stores/persistence'
import type { DemoSeedResult } from './types'

/**
 * 生成演示业务数据（幂等，仅企业管理员）。
 *
 * 服务端会按「调用时刻」重算演示订单/物流的日期，因此任何日期点击都能得到
 * 可用于对话的数据；重复调用不会重复创建（counts 全为 0）。
 */
export async function generateDemoData(
  organizationId?: string,
): Promise<DemoSeedResult> {
  const targetOrganizationId = organizationId ?? readOrganizationStorage()
  const headers: Record<string, string> = {}
  // 未选择企业时本地没有当前企业，显式传入的企业 id 由 store 直接在内存中给出；
  // 两者都缺失时交给后端返回明确的 400/404，而不是发一个空头。
  if (targetOrganizationId) {
    headers['X-Organization-ID'] = targetOrganizationId
  }
  // 拦截器只在租户请求上附加 Token，这里补一次读，保证与显式头配对成功
  const auth = readAuthStorage()
  if (auth) {
    headers.Authorization = `Bearer ${auth.accessToken}`
  }
  const response = await httpClient.post<DemoSeedResult>('/demo-data/', null, { headers })
  return response.data
}
