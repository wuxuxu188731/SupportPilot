/*
 * 成员管理 Store 测试：列表加载、租户切换隔离、添加/改角色/移除的结果分类。
 * 通过 mock API 模块进行，不依赖真实后端或网络。
 */

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'
import { AUTH_STORAGE_KEY, ORGANIZATION_STORAGE_KEY } from '@/stores/persistence'
import { useMembersStore } from '@/stores/members'
import { useOrganizationStore } from '@/stores/organization'

vi.mock('@/api/auth', () => ({
  login: vi.fn(),
  register: vi.fn(),
  me: vi.fn(),
}))
vi.mock('@/api/organization', () => ({
  listOrganizations: vi.fn(),
  createOrganization: vi.fn(),
  listMembers: vi.fn(),
  addMember: vi.fn(),
  updateMemberRole: vi.fn(),
  removeMember: vi.fn(),
}))

import * as authApi from '@/api/auth'
import * as organizationApi from '@/api/organization'
import type { OrganizationMember } from '@/api/types'

const CURRENT_USER = { user_id: 'u-1', username: 'alice', created_at: '2026-09-01 00:00:00' }

/** 构造一条成员记录。 */
function member(
  userId: string,
  username: string,
  role: 'admin' | 'agent' = 'agent',
): OrganizationMember {
  return { user_id: userId, username, role, created_at: '2026-09-02 03:04:05' }
}

/** 构造带指定 HTTP 状态的 ApiError。 */
function apiError(status: number, message: string): ApiError {
  return new ApiError({ status, code: null, message, fieldErrors: {}, raw: null })
}

/** 预置登录态、当前企业与已有成员列表。 */
async function setupStore(members: OrganizationMember[]) {
  window.localStorage.setItem(
    ORGANIZATION_STORAGE_KEY,
    JSON.stringify({ organizationId: 'org-1' }),
  )
  vi.mocked(authApi.me).mockResolvedValue(CURRENT_USER)
  vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
    { organization_id: 'org-1', name: '示例企业', role: 'admin' },
  ])
  vi.mocked(organizationApi.listMembers).mockResolvedValue(members)

  // 认证 store 提供 currentUserId（用于「我」的标记与自我保护）
  const { useAuthStore } = await import('@/stores/auth')
  const authStore = useAuthStore()
  await authStore.ensureInitialized()

  const organizationStore = useOrganizationStore()
  await organizationStore.load()

  const store = useMembersStore()
  await store.loadMembers()
  return store
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  window.localStorage.setItem(
    AUTH_STORAGE_KEY,
    JSON.stringify({ accessToken: 'token-1', tokenType: 'bearer', expiresIn: 1800, savedAt: Date.now() }),
  )
})

describe('成员列表加载', () => {
  it('加载成功：填充成员列表并记录企业 id', async () => {
    // 保护行为：成员列表来源必须是后端最新数据，且记录数据所属企业
    const store = await setupStore([member('u-1', 'alice', 'admin'), member('u-2', 'bob')])

    expect(store.members).toHaveLength(2)
    expect(store.loadedOrganizationId).toBe('org-1')
    expect(store.loading).toBe(false)
    expect(store.error).toBeNull()
    expect(store.adminCount).toBe(1)
    expect(store.isEmpty).toBe(false)
  })

  it('已有数据时不重复请求，除非强制刷新', async () => {
    // 边界情况：页面重复挂载不应反复请求
    const store = await setupStore([member('u-1', 'alice', 'admin')])

    await store.loadMembers()
    expect(organizationApi.listMembers).toHaveBeenCalledTimes(1)

    await store.loadMembers(true)
    expect(organizationApi.listMembers).toHaveBeenCalledTimes(2)
  })

  it('加载失败：记录错误消息且不写入脏数据', async () => {
    // 保护行为：失败必须可被页面感知（错误 + 重试），不允许静默吞掉
    window.localStorage.setItem(
      ORGANIZATION_STORAGE_KEY,
      JSON.stringify({ organizationId: 'org-1' }),
    )
    vi.mocked(authApi.me).mockResolvedValue(CURRENT_USER)
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
      { organization_id: 'org-1', name: '示例企业', role: 'admin' },
    ])
    vi.mocked(organizationApi.listMembers).mockRejectedValue(new Error('无法连接到服务器'))
    const organizationStore = useOrganizationStore()
    await organizationStore.load()

    const store = useMembersStore()
    await store.loadMembers()

    expect(store.error).toContain('无法连接到服务器')
    expect(store.members).toEqual([])
  })

  it('无当前企业时不发起租户请求', async () => {
    // 边界情况：没有企业上下文时不得请求成员接口（后端会 400）
    const store = useMembersStore()

    await store.loadMembers()

    expect(organizationApi.listMembers).not.toHaveBeenCalled()
    expect(store.error).toBe('请先选择企业')
  })

  it('租户重置后清空成员与所属企业', async () => {
    // 保护行为：切换企业必须清空成员缓存，避免旧企业成员泄漏到新企业界面
    const store = await setupStore([member('u-1', 'alice', 'admin')])

    // 通过切换企业触发已注册的租户重置回调
    const organizationStore = useOrganizationStore()
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
      { organization_id: 'org-2', name: '第二家企业', role: 'agent' },
    ])
    await organizationStore.load(true)
    await organizationStore.select('org-2')

    expect(store.members).toEqual([])
    expect(store.loadedOrganizationId).toBeNull()
  })
})

describe('添加成员', () => {
  it('添加成功：返回 ok 并刷新列表（拿到服务端加入时间）', async () => {
    // 保护行为：添加成功后列表必须重新拉取，不伪造加入时间
    const store = await setupStore([member('u-1', 'alice', 'admin')])
    vi.mocked(organizationApi.addMember).mockResolvedValue({
      organization_id: 'org-1',
      user_id: 'u-2',
      role: 'agent',
    })
    vi.mocked(organizationApi.listMembers).mockResolvedValue([
      member('u-1', 'alice', 'admin'),
      member('u-2', 'bob'),
    ])

    const outcome = await store.addMember('bob', 'agent')

    expect(outcome.result).toBe('ok')
    expect(store.members).toHaveLength(2)
    expect(organizationApi.addMember).toHaveBeenCalledWith('org-1', {
      username: 'bob',
      role: 'agent',
    })
  })

  it('重复添加（409）分类为 duplicate', async () => {
    // 保护行为：409 语义是「已是成员」，需要给出可区分的提示
    const store = await setupStore([member('u-1', 'alice', 'admin')])
    vi.mocked(organizationApi.addMember).mockRejectedValue(
      apiError(409, '该用户已是本企业成员'),
    )

    const outcome = await store.addMember('bob', 'agent')

    expect(outcome.result).toBe('duplicate')
  })

  it('用户名不存在（404）分类为 user-missing', async () => {
    // 边界情况：404 语义是「对方未注册」，提示应引导核对用户名
    const store = await setupStore([member('u-1', 'alice', 'admin')])
    vi.mocked(organizationApi.addMember).mockRejectedValue(
      apiError(404, '用户不存在，请确认对方已注册且用户名正确'),
    )

    const outcome = await store.addMember('nobody', 'agent')

    expect(outcome.result).toBe('user-missing')
  })

  it('非管理员（403）分类为 forbidden 并刷新角色', async () => {
    // 保护行为：403 后必须重置角色缓存，让界面入口随服务端收敛
    const store = await setupStore([member('u-1', 'alice', 'agent')])
    vi.mocked(organizationApi.addMember).mockRejectedValue(
      apiError(403, '该操作需要企业管理员权限'),
    )
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
      { organization_id: 'org-1', name: '示例企业', role: 'agent' },
    ])

    const outcome = await store.addMember('bob', 'agent')

    expect(outcome.result).toBe('forbidden')
    expect(store.isAdmin).toBe(false)
  })
})

describe('修改成员角色', () => {
  it('修改成功：以服务端响应更新本地列表项', async () => {
    // 保护行为：角色以服务端返回值为准，不做乐观更新
    const store = await setupStore([member('u-1', 'alice', 'admin'), member('u-2', 'bob')])
    vi.mocked(organizationApi.updateMemberRole).mockResolvedValue({
      organization_id: 'org-1',
      user_id: 'u-2',
      role: 'admin',
    })

    const outcome = await store.changeRole('u-2', 'admin')

    expect(outcome.result).toBe('ok')
    expect(store.members.find((item) => item.user_id === 'u-2')?.role).toBe('admin')
    expect(store.adminCount).toBe(2)
  })

  it('不能修改自己的角色：直接返回 self-conflict 且不发请求', async () => {
    // 保护行为：后端禁止自我改角色，前端不应发出必然被 409 拒绝的请求
    const store = await setupStore([member('u-1', 'alice', 'admin')])

    const outcome = await store.changeRole('u-1', 'agent')

    expect(outcome.result).toBe('self-conflict')
    expect(organizationApi.updateMemberRole).not.toHaveBeenCalled()
  })

  it('成员已被移除（404）：分类为 not-found 并刷新列表', async () => {
    // 边界情况：并发移除后改角色会 404，必须刷新列表纠正界面
    const store = await setupStore([member('u-1', 'alice', 'admin'), member('u-2', 'bob')])
    vi.mocked(organizationApi.updateMemberRole).mockRejectedValue(
      apiError(404, '成员不存在'),
    )
    vi.mocked(organizationApi.listMembers).mockResolvedValue([member('u-1', 'alice', 'admin')])

    const outcome = await store.changeRole('u-2', 'admin')

    expect(outcome.result).toBe('not-found')
    expect(store.members).toHaveLength(1)
  })

  it('服务端 409（自我操作）：分类为 self-conflict', async () => {
    // 边界情况：并发场景下服务端仍会拦自我操作，需与服务端语义一致
    const store = await setupStore([member('u-1', 'alice', 'admin'), member('u-2', 'bob', 'admin')])
    vi.mocked(organizationApi.updateMemberRole).mockRejectedValue(apiError(409, '冲突'))

    const outcome = await store.changeRole('u-2', 'agent')

    expect(outcome.result).toBe('self-conflict')
  })
})

describe('移除成员', () => {
  it('移除成功：从本地列表移除该成员', async () => {
    // 保护行为：移除成功后成员必须立即从列表消失
    const store = await setupStore([member('u-1', 'alice', 'admin'), member('u-2', 'bob')])
    vi.mocked(organizationApi.removeMember).mockResolvedValue(undefined)

    const outcome = await store.removeMember('u-2')

    expect(outcome.result).toBe('ok')
    expect(store.members.map((item) => item.user_id)).toEqual(['u-1'])
    expect(organizationApi.removeMember).toHaveBeenCalledWith('org-1', 'u-2')
  })

  it('不能移除自己：直接返回 self-conflict 且不发请求', async () => {
    // 保护行为：企业必须始终保留管理员，前端不发出必然被拒绝的自我移除请求
    const store = await setupStore([member('u-1', 'alice', 'admin')])

    const outcome = await store.removeMember('u-1')

    expect(outcome.result).toBe('self-conflict')
    expect(organizationApi.removeMember).not.toHaveBeenCalled()
  })

  it('非管理员（403）：分类为 forbidden 且保留成员', async () => {
    // 边界情况：权限不足时不得改动本地列表
    const store = await setupStore([member('u-1', 'alice', 'agent'), member('u-2', 'bob')])
    vi.mocked(organizationApi.removeMember).mockRejectedValue(apiError(403, '需要管理员'))

    const outcome = await store.removeMember('u-2')

    expect(outcome.result).toBe('forbidden')
    expect(store.members).toHaveLength(2)
  })

  it('网络失败：分类为 error 且给出兜底文案', async () => {
    // 边界情况：非 ApiError（网络层异常）必须落到统一兜底文案，
    // 不能把原始异常文本直接展示给用户
    const store = await setupStore([member('u-1', 'alice', 'admin'), member('u-2', 'bob')])
    vi.mocked(organizationApi.removeMember).mockRejectedValue(new Error('boom'))

    const outcome = await store.removeMember('u-2')

    expect(outcome.result).toBe('error')
    if (outcome.result === 'error') {
      expect(outcome.message).toBe('移除成员失败，请稍后重试')
    }
    expect(store.members).toHaveLength(2)
  })

  it('服务端错误（ApiError）：带回已翻译的可展示消息', async () => {
    // 保护行为：ApiError 自带的用户可读消息应优先于兜底文案
    const store = await setupStore([member('u-1', 'alice', 'admin'), member('u-2', 'bob')])
    vi.mocked(organizationApi.removeMember).mockRejectedValue(
      apiError(500, '服务器内部错误，请稍后重试'),
    )

    const outcome = await store.removeMember('u-2')

    expect(outcome.result).toBe('error')
    if (outcome.result === 'error') {
      expect(outcome.message).toBe('服务器内部错误，请稍后重试')
    }
  })
})
