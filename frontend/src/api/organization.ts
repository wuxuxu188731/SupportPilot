/*
 * 组织模块 API 封装：/organizations/ 的创建与列表（后端企业接口
 * 不读取 X-Organization-ID，企业 id 通过路径或响应获得）。
 */

import { httpClient } from './http'
import type { CreateOrganizationRequest, OrganizationAccess, OrganizationInfo } from './types'

/** 创建企业：创建者自动成为该企业的 admin 成员。 */
export async function createOrganization(payload: CreateOrganizationRequest): Promise<OrganizationInfo> {
  const response = await httpClient.post<OrganizationInfo>('/organizations/', payload)
  return response.data
}

/** 列出当前用户加入的所有企业及各自角色。 */
export async function listOrganizations(): Promise<OrganizationAccess[]> {
  const response = await httpClient.get<OrganizationAccess[]>('/organizations/')
  return response.data
}
