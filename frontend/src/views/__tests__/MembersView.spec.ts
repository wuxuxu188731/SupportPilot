/*
 * 成员管理页组件测试：成员列表渲染、角色区分、写操作权限与二次确认。
 */

import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'
import { createAppRouter, registerGuards } from '@/router'
import { AUTH_STORAGE_KEY, ORGANIZATION_STORAGE_KEY } from '@/stores/persistence'
import MembersView from '@/views/MembersView.vue'

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
import { useMembersStore } from '@/stores/members'

const CURRENT_USER = { user_id: 'u-1', username: 'alice', created_at: '2026-09-01 00:00:00' }

/** 构造一条成员记录。 */
function member(userId: string, username: string, role: 'admin' | 'agent' = 'agent') {
  return { user_id: userId, username, role, created_at: '2026-09-02 03:04:05' }
}

/** 构造带指定 HTTP 状态的 ApiError。 */
function apiError(status: number): ApiError {
  return new ApiError({ status, code: null, message: `HTTP ${status}`, fieldErrors: {}, raw: null })
}

/** 以指定角色挂载成员管理页（走真实路由守卫，确保企业上下文已就绪）。 */
async function mountMembersView(myRole: 'admin' | 'agent' = 'admin') {
  window.localStorage.setItem(
    AUTH_STORAGE_KEY,
    JSON.stringify({ accessToken: 'token-1', tokenType: 'bearer', expiresIn: 1800, savedAt: Date.now() }),
  )
  window.localStorage.setItem(
    ORGANIZATION_STORAGE_KEY,
    JSON.stringify({ organizationId: 'org-1' }),
  )
  vi.mocked(authApi.me).mockResolvedValue(CURRENT_USER)
  vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
    { organization_id: 'org-1', name: '示例企业', role: myRole },
  ])

  const router = createAppRouter(createMemoryHistory())
  const pinia = createPinia()
  setActivePinia(pinia)
  registerGuards(router)
  await router.push('/app/members')
  await router.isReady()
  const wrapper = mount(
    {
      render: () =>
        h(NDialogProvider, null, {
          default: () => h(NMessageProvider, null, { default: () => h(MembersView) }),
        }),
    },
    { global: { plugins: [pinia, router], stubs: { teleport: true } } },
  )
  await flushPromises()
  return { wrapper, router }
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('成员列表展示', () => {
  it('渲染成员用户名、角色与加入时间', async () => {
    // 保护行为：成员行必须展示用户名与角色，供管理员核对成员构成
    vi.mocked(organizationApi.listMembers).mockResolvedValue([
      member('u-1', 'alice', 'admin'),
      member('u-2', 'bob'),
    ])

    const { wrapper } = await mountMembersView('admin')

    expect(wrapper.text()).toContain('alice')
    expect(wrapper.text()).toContain('bob')
    expect(wrapper.text()).toContain('管理员')
    expect(wrapper.text()).toContain('客服')
    expect(wrapper.text()).toContain('共 2 位成员')
  })

  it('标记「我」且不给自己提供改角色/移除入口', async () => {
    // 保护行为：后端禁止自我管理成员关系，界面不应提供必然失败的入口
    vi.mocked(organizationApi.listMembers).mockResolvedValue([
      member('u-1', 'alice', 'admin'),
      member('u-2', 'bob'),
    ])

    const { wrapper } = await mountMembersView('admin')

    expect(wrapper.find('[data-test="member-alice"]').text()).toContain('我')
    expect(wrapper.find('[data-test="member-alice"]').text()).toContain('不能管理自己的成员关系')
    expect(wrapper.find('[data-test="remove-alice"]').exists()).toBe(false)
    // 其他成员可管理
    expect(wrapper.find('[data-test="remove-bob"]').exists()).toBe(true)
  })

  it('agent 视角：不显示添加/移除入口并给出权限说明', async () => {
    // 保护行为：写操作入口仅对 admin 展示（后端仍会强制校验）
    vi.mocked(organizationApi.listMembers).mockResolvedValue([
      member('u-1', 'alice', 'admin'),
      member('u-2', 'bob'),
    ])

    const { wrapper } = await mountMembersView('agent')

    expect(wrapper.find('[data-test="open-add-member"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="remove-bob"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('需要管理员权限')
  })

  it('本地搜索：按用户名过滤成员', async () => {
    // 保护行为：搜索为前端本地筛选（后端成员接口无查询参数）
    vi.mocked(organizationApi.listMembers).mockResolvedValue([
      member('u-1', 'alice', 'admin'),
      member('u-2', 'bob'),
    ])

    const { wrapper } = await mountMembersView('admin')
    await wrapper.find('[data-test="member-search"] input').setValue('bob')
    await flushPromises()

    expect(wrapper.find('[data-test="member-bob"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="member-alice"]').exists()).toBe(false)
  })

  it('成员列表加载失败：展示错误与重试入口', async () => {
    // 保护行为：加载失败必须可见，不允许页面假装成功
    vi.mocked(organizationApi.listMembers).mockRejectedValueOnce(new Error('网络不可用'))
    vi.mocked(organizationApi.listMembers).mockResolvedValueOnce([member('u-1', 'alice', 'admin')])

    const { wrapper } = await mountMembersView('admin')
    expect(wrapper.text()).toContain('网络不可用')

    const retry = wrapper.findAll('button').find((button) => button.text().includes('重试'))
    expect(retry).toBeDefined()
    await retry!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('alice')
  })
})

describe('移除成员', () => {
  it('点击移除先确认，确认后才调用接口并从列表消失', async () => {
    // 保护行为：危险操作必须先确认；确认前不得发出请求
    vi.mocked(organizationApi.listMembers).mockResolvedValue([
      member('u-1', 'alice', 'admin'),
      member('u-2', 'bob'),
    ])
    vi.mocked(organizationApi.removeMember).mockResolvedValue(undefined)

    const { wrapper } = await mountMembersView('admin')
    await wrapper.find('[data-test="remove-bob"]').trigger('click')
    await flushPromises()

    expect(organizationApi.removeMember).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('确定把')

    await wrapper.find('[data-test="confirm-remove-member"]').trigger('click')
    await flushPromises()

    expect(organizationApi.removeMember).toHaveBeenCalledWith('org-1', 'u-2')
    expect(wrapper.find('[data-test="member-bob"]').exists()).toBe(false)
  })

  it('后端拒绝（403）：成员保留并提示权限不足', async () => {
    // 边界情况：权限被降级后服务端会 403，界面不得假装移除成功
    vi.mocked(organizationApi.listMembers).mockResolvedValue([
      member('u-1', 'alice', 'admin'),
      member('u-2', 'bob'),
    ])
    vi.mocked(organizationApi.removeMember).mockRejectedValue(apiError(403))

    const { wrapper } = await mountMembersView('admin')
    await wrapper.find('[data-test="remove-bob"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-test="confirm-remove-member"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="member-bob"]').exists()).toBe(true)
  })
})

describe('添加成员', () => {
  it('提交非法用户名：本地拦截且不发请求', async () => {
    // 边界情况：与后端同规则的前置校验（3–32 位小写字母/数字/下划线）
    vi.mocked(organizationApi.listMembers).mockResolvedValue([member('u-1', 'alice', 'admin')])

    const { wrapper } = await mountMembersView('admin')
    await wrapper.find('[data-test="open-add-member"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-test="member-username-input"] input').setValue('A')
    await wrapper.find('[data-test="submit-add-member"]').trigger('click')
    await flushPromises()

    expect(organizationApi.addMember).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('3–32 位')
  })

  it('添加成功：调用接口并刷新列表显示新成员', async () => {
    // 保护行为：添加成功后成员必须出现在列表中（以服务端数据为准）
    vi.mocked(organizationApi.listMembers)
      .mockResolvedValueOnce([member('u-1', 'alice', 'admin')])
      .mockResolvedValueOnce([member('u-1', 'alice', 'admin'), member('u-2', 'bob')])
    vi.mocked(organizationApi.addMember).mockResolvedValue({
      organization_id: 'org-1',
      user_id: 'u-2',
      role: 'agent',
    })

    const { wrapper } = await mountMembersView('admin')
    await wrapper.find('[data-test="open-add-member"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-test="member-username-input"] input').setValue('bob')
    await wrapper.find('[data-test="submit-add-member"]').trigger('click')
    await flushPromises()

    expect(organizationApi.addMember).toHaveBeenCalledWith('org-1', { username: 'bob', role: 'agent' })
    expect(wrapper.find('[data-test="member-bob"]').exists()).toBe(true)
  })

  it('用户名不存在（404）：提示核对用户名且不新增成员', async () => {
    // 边界情况：404 语义是「对方未注册」，提示必须引导核对用户名
    vi.mocked(organizationApi.listMembers).mockResolvedValue([member('u-1', 'alice', 'admin')])
    vi.mocked(organizationApi.addMember).mockRejectedValue(apiError(404))

    const { wrapper } = await mountMembersView('admin')
    await wrapper.find('[data-test="open-add-member"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-test="member-username-input"] input').setValue('nobody')
    await wrapper.find('[data-test="submit-add-member"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('用户不存在')
    expect(useMembersStore().members).toHaveLength(1)
  })
})
