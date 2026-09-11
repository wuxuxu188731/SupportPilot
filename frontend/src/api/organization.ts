/*
 * 组织模块 API 封装：/organizations/ 的创建、列表与成员管理。
 *
 * 事实说明（依据 docs/frontend/api-inventory.md）：
 *  - 本模块所有接口都不读取 X-Organization-ID 请求头：企业 id 走路径参数，
 *    服务端自行校验「我是否属于该企业 / 是否 admin」；
 *  - 成员管理写操作（添加/改角色/移除）后端强制 admin，前端隐藏入口只是体验优化；
 *  - 「改角色/移除」的目标成员统一用 user_id 指定（用户名可读性差不适合做标识）。
 */

import { httpClient } from './http'
import type {
  AddMemberRequest,
  CreateOrganizationRequest,
  MembershipResponse,
  OrganizationAccess,
  OrganizationInfo,
  OrganizationMember,
  UpdateMemberRoleRequest,
} from './types'

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

/** 成员列表：企业内成员均可读，返回成员用户名与角色（不含任何凭据字段）。 */
export async function listMembers(organizationId: string): Promise<OrganizationMember[]> {
  const response = await httpClient.get<OrganizationMember[]>(
    `/organizations/${organizationId}/members/`,
  )
  return response.data
}

/** 添加成员：按精确用户名把已注册用户加入企业（仅 admin）。 */
export async function addMember(
  organizationId: string,
  payload: AddMemberRequest,
): Promise<MembershipResponse> {
  const response = await httpClient.post<MembershipResponse>(
    `/organizations/${organizationId}/members/`,
    payload,
  )
  return response.data
}

/** 修改成员角色（仅 admin）；后端拒绝管理员修改自己的角色（409）。 */
export async function updateMemberRole(
  organizationId: string,
  userId: string,
  payload: UpdateMemberRoleRequest,
): Promise<MembershipResponse> {
  const response = await httpClient.patch<MembershipResponse>(
    `/organizations/${organizationId}/members/${userId}/`,
    payload,
  )
  return response.data
}

/** 移除成员（仅 admin）；后端拒绝管理员移除自己（409），成功返回 204 无响应体。 */
export async function removeMember(organizationId: string, userId: string): Promise<void> {
  await httpClient.delete<void>(`/organizations/${organizationId}/members/${userId}/`)
}
