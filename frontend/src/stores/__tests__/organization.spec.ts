/*
 * 企业上下文 Store 测试：列表加载、本地选择恢复/失效清除、创建、切换。
 * 通过 mock API 模块进行，不依赖真实后端或网络。
 */

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ORGANIZATION_STORAGE_KEY } from '@/stores/persistence'
import { useOrganizationStore } from '@/stores/organization'
import { registerTenantResetHandler } from '@/stores/tenantReset'

// mock 企业 API：测试不触达网络
vi.mock('@/api/organization', () => ({
  listOrganizations: vi.fn(),
  createOrganization: vi.fn(),
}))

import * as organizationApi from '@/api/organization'
import type { OrganizationAccess } from '@/api/types'

/** 构造一条企业访问记录。 */
function org(id: string, name: string, role: 'admin' | 'agent' = 'admin'): OrganizationAccess {
  return { organization_id: id, name, role }
}

/** 在企业 Store 中预置一条列表（模拟已加载状态）。 */
async function storeWithList(items: OrganizationAccess[]): Promise<ReturnType<typeof useOrganizationStore>> {
  vi.mocked(organizationApi.listOrganizations).mockResolvedValue(items)
  const store = useOrganizationStore()
  await store.load()
  return store
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('企业列表加载', () => {
  it('加载成功：填充列表并标记已加载', async () => {
    // 保护行为：企业列表来源必须是后端最新数据（覆盖测试 8）
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([org('org-1', '示例企业', 'admin')])

    const store = useOrganizationStore()
    await store.load()

    expect(store.loaded).toBe(true)
    expect(store.loading).toBe(false)
    expect(store.error).toBeNull()
    expect(store.organizations).toHaveLength(1)
    expect(store.organizations[0].name).toBe('示例企业')
  })

  it('加载失败：记录错误且不进入已加载状态', async () => {
    // 保护行为：失败必须可被页面感知（错误 + 重试），不允许静默吞掉
    vi.mocked(organizationApi.listOrganizations).mockRejectedValue(new Error('无法连接服务器'))

    const store = useOrganizationStore()
    await store.load()

    expect(store.loaded).toBe(false)
    expect(store.error).toContain('无法连接服务器')
    expect(store.organizations).toEqual([])
  })

  it('已加载成功后不会重复请求（除非强制刷新）', async () => {
    // 边界情况：避免守卫与页面挂载多次触发重复请求
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([org('org-1', 'A')])
    const store = useOrganizationStore()
    await store.load()
    await store.load()

    expect(organizationApi.listOrganizations).toHaveBeenCalledTimes(1)
  })
})

describe('本地企业选择：刷新后恢复', () => {
  it('本地企业仍在最新列表中：恢复选择', async () => {
    // 保护行为：刷新页面后应恢复上次选择（覆盖测试 9）
    window.localStorage.setItem(ORGANIZATION_STORAGE_KEY, JSON.stringify({ organizationId: 'org-1' }))
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
      org('org-1', '我的企业', 'agent'),
      org('org-2', '别的企业'),
    ])

    const store = useOrganizationStore()
    await store.load()

    expect(store.currentOrganizationId).toBe('org-1')
    expect(store.hasCurrentOrganization).toBe(true)
    expect(store.currentRole).toBe('agent')
    // 本地存储保持原值不变
    expect(JSON.parse(window.localStorage.getItem(ORGANIZATION_STORAGE_KEY) ?? '{}').organizationId).toBe('org-1')
  })

  it('本地企业已失效（被移出/删除）：清除选择并要求重新选择', async () => {
    // 保护行为：失去企业权限后不得继续携带旧企业请求（覆盖测试 10）
    window.localStorage.setItem(ORGANIZATION_STORAGE_KEY, JSON.stringify({ organizationId: 'org-old' }))
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([org('org-1', '现存企业')])

    const store = useOrganizationStore()
    await store.load()

    expect(store.currentOrganizationId).toBeNull()
    expect(store.hasCurrentOrganization).toBe(false)
    expect(window.localStorage.getItem(ORGANIZATION_STORAGE_KEY)).toBeNull()
  })

  it('本地保存的企业 id 为空字符串时按未选择处理', async () => {
    // 边界情况：脏数据（空 id）不应被当作有效选择
    window.localStorage.setItem(ORGANIZATION_STORAGE_KEY, JSON.stringify({ organizationId: '' }))
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([org('org-1', '现存企业')])

    const store = useOrganizationStore()
    await store.load()

    expect(store.hasCurrentOrganization).toBe(false)
  })
})

describe('创建企业', () => {
  it('创建成功：刷新列表并自动选择新企业', async () => {
    // 保护行为：创建者应立即获得 admin 角色并可进入新企业
    vi.mocked(organizationApi.listOrganizations)
      .mockResolvedValueOnce([]) // 首次加载：空列表
      .mockResolvedValueOnce([org('org-new', '新公司', 'admin')]) // 创建后刷新
    vi.mocked(organizationApi.createOrganization).mockResolvedValue({
      organization_id: 'org-new',
      name: '新公司',
    })

    const store = useOrganizationStore()
    await store.load()
    expect(store.isEmpty).toBe(true)

    await store.create('新公司')

    expect(store.currentOrganizationId).toBe('org-new')
    expect(store.currentRole).toBe('admin')
    expect(JSON.parse(window.localStorage.getItem(ORGANIZATION_STORAGE_KEY) ?? '{}').organizationId).toBe('org-new')
  })

  it('创建期间重复调用被拦截', async () => {
    // 边界情况：store 层防重复创建兜底（与按钮 loading 双保险）
    let resolveCreate!: (value: { organization_id: string; name: string }) => void
    vi.mocked(organizationApi.createOrganization).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCreate = resolve
        }),
    )
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([org('org-new', '新公司', 'admin')])

    const store = useOrganizationStore()
    const first = store.create('新公司')
    await store.create('新公司') // 进行中：忽略

    expect(organizationApi.createOrganization).toHaveBeenCalledTimes(1)
    resolveCreate({ organization_id: 'org-new', name: '新公司' })
    await first
  })
})

describe('选择与切换企业', () => {
  it('选择企业：更新状态与本地存储', async () => {
    // 保护行为：点击选择后应立即成为当前企业并持久化
    const store = await storeWithList([org('org-1', '甲企业'), org('org-2', '乙企业')])
    const ok = await store.select('org-2')

    expect(ok).toBe(true)
    expect(store.currentOrganizationId).toBe('org-2')
    expect(JSON.parse(window.localStorage.getItem(ORGANIZATION_STORAGE_KEY) ?? '{}').organizationId).toBe('org-2')
  })

  it('切换企业时执行租户缓存清理回调（预留统一重置机制）', async () => {
    // 保护行为：切换企业必须触发已注册的租户清理，防止旧企业缓存泄漏
    const store = await storeWithList([org('org-1', '甲企业'), org('org-2', '乙企业')])
    const cleanup = vi.fn()
    registerTenantResetHandler(cleanup)

    await store.select('org-2')

    expect(cleanup).toHaveBeenCalledTimes(1)
  })

  it('选择相同企业不重复执行缓存清理', async () => {
    // 边界情况：重复进入当前企业（如刷新守卫路径）不应反复清理缓存
    const store = await storeWithList([org('org-1', '甲企业')])
    await store.select('org-1')
    const cleanup = vi.fn()
    registerTenantResetHandler(cleanup)

    await store.select('org-1')

    expect(cleanup).not.toHaveBeenCalled()
  })

  it('选择不在列表中的企业：返回失败且不改变当前状态', async () => {
    // 边界情况：已被移出企业的选择请求必须失败，不能产生脏状态
    const store = await storeWithList([org('org-1', '甲企业')])
    const ok = await store.select('org-ghost')

    expect(ok).toBe(false)
    expect(store.currentOrganizationId).toBeNull()
  })
})

describe('整体重置', () => {
  it('reset：清空列表、选择与加载状态（退出登录使用）', async () => {
    // 保护行为：退出登录后所有企业上下文必须归零
    const store = await storeWithList([org('org-1', '甲企业')])
    await store.select('org-1')

    store.reset()

    expect(store.organizations).toEqual([])
    expect(store.currentOrganizationId).toBeNull()
    expect(store.hasCurrentOrganization).toBe(false)
    expect(store.loaded).toBe(false)
    expect(window.localStorage.getItem(ORGANIZATION_STORAGE_KEY)).toBeNull()
  })
})
