/*
 * 企业成员管理 API 封装测试：路径、请求体与响应解析。
 * 通过 mock httpClient 进行，不触达网络。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

// mock 统一 HTTP 客户端：验证调用形态（含成员管理新增的 patch/delete）
vi.mock('@/api/http', () => ({
  httpClient: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}))

import * as organizationApi from '@/api/organization'
import { httpClient } from '@/api/http'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('成员列表 API', () => {
  it('GET /organizations/{id}/members/ 返回成员数组', async () => {
    // 保护行为：成员列表路径必须包含企业 id（后端据此校验我是否属于该企业）
    const members = [
      { user_id: 'u-1', username: 'alice', role: 'admin', created_at: '2026-09-01 00:00:00' },
    ]
    vi.mocked(httpClient.get).mockResolvedValue({ data: members })

    const result = await organizationApi.listMembers('org-1')

    expect(result).toEqual(members)
    expect(httpClient.get).toHaveBeenCalledWith('/organizations/org-1/members/')
  })
})

describe('添加成员 API', () => {
  it('POST /organizations/{id}/members/ 提交用户名与角色', async () => {
    // 保护行为：添加成员按精确用户名 + 角色提交，不额外携带 organization_id
    vi.mocked(httpClient.post).mockResolvedValue({
      data: { organization_id: 'org-1', user_id: 'u-2', role: 'agent' },
    })

    const result = await organizationApi.addMember('org-1', { username: 'bob', role: 'agent' })

    expect(result.user_id).toBe('u-2')
    expect(httpClient.post).toHaveBeenCalledWith('/organizations/org-1/members/', {
      username: 'bob',
      role: 'agent',
    })
  })
})

describe('修改成员角色 API', () => {
  it('PATCH /organizations/{id}/members/{user_id}/ 提交新角色', async () => {
    // 保护行为：目标成员用 user_id 定位（用户名可变，不适合作为标识）
    vi.mocked(httpClient.patch).mockResolvedValue({
      data: { organization_id: 'org-1', user_id: 'u-2', role: 'admin' },
    })

    const result = await organizationApi.updateMemberRole('org-1', 'u-2', { role: 'admin' })

    expect(result.role).toBe('admin')
    expect(httpClient.patch).toHaveBeenCalledWith('/organizations/org-1/members/u-2/', {
      role: 'admin',
    })
  })
})

describe('移除成员 API', () => {
  it('DELETE /organizations/{id}/members/{user_id}/ 且不解析响应体', async () => {
    // 保护行为：移除成功返回 204 无响应体，封装不得假设有 JSON 内容
    vi.mocked(httpClient.delete).mockResolvedValue({ data: undefined })

    await expect(organizationApi.removeMember('org-1', 'u-2')).resolves.toBeUndefined()

    expect(httpClient.delete).toHaveBeenCalledWith('/organizations/org-1/members/u-2/')
  })

  it('移除失败时把 ApiError 原样抛出（不吞错误）', async () => {
    // 边界情况：后端 409/403 等错误必须向上抛出，由 store 分类处理
    const error = new Error('administrators cannot remove their own membership')
    vi.mocked(httpClient.delete).mockRejectedValue(error)

    await expect(organizationApi.removeMember('org-1', 'u-1')).rejects.toThrow(
      'administrators cannot remove their own membership',
    )
  })
})
