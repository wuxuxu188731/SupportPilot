/*
 * 主布局菜单测试：各功能模块导航可用、菜单高亮跟随路由。
 */

import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '@/router'
import MainLayout from '@/layouts/MainLayout.vue'
import { flushNavigation } from '@/test/routerHelpers'
import { AUTH_STORAGE_KEY, ORGANIZATION_STORAGE_KEY } from '@/stores/persistence'

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

const USER = { user_id: 'u-1', username: 'alice', created_at: '2026-09-01 00:00:00' }
const ORG_LIST = [{ organization_id: 'org-1', name: '示例企业', role: 'admin' as const }]

function seedLogin(): void {
  window.localStorage.setItem(
    AUTH_STORAGE_KEY,
    JSON.stringify({ accessToken: 'token-1', tokenType: 'bearer', expiresIn: 1800, savedAt: Date.now() }),
  )
  window.localStorage.setItem(
    ORGANIZATION_STORAGE_KEY,
    JSON.stringify({ organizationId: 'org-1' }),
  )
}

async function mountLayout(path: string) {
  const router = createAppRouter(createMemoryHistory())
  await router.push(path)
  await router.isReady()
  const wrapper = mount(
    { render: () => h(NDialogProvider, null, { default: () => h(NMessageProvider, null, { default: () => h(MainLayout) }) }) },
    { global: { plugins: [createPinia(), router] } },
  )
  await flushPromises()
  return { wrapper, router }
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.mocked(authApi.me).mockResolvedValue(USER)
  vi.mocked(organizationApi.listOrganizations).mockResolvedValue(ORG_LIST)
  seedLogin()
})

describe('MainLayout 功能菜单', () => {
  it('五个功能模块全部开放可用，不再有待实现占位', async () => {
    // 保护行为：菜单中所有已开放模块都是可点击项，不存在「（待实现）」占位
    const { wrapper } = await mountLayout('/app')

    for (const label of ['工作台', '客服对话', '审批中心', '知识库', '成员管理']) {
      expect(wrapper.text()).toContain(label)
      expect(wrapper.text()).not.toContain(`${label}（待实现）`)
    }
    expect(wrapper.text()).not.toContain('待实现')
  })

  it('在 /app/chat 页面时菜单高亮「客服对话」', async () => {
    // 保护行为：菜单选中态必须跟随当前激活页面（覆盖测试 7）
    const { wrapper } = await mountLayout('/app/chat/conv-1')

    const selected = wrapper
      .findAll('.n-menu-item-content')
      .find((element) => [...element.classes()].some((name) => name.includes('--selected')))
    expect(selected).toBeDefined()
    expect(selected!.text()).toContain('客服对话')
  })

  it('在 /app/approvals 页面时菜单高亮「审批中心」', async () => {
    // 保护行为：审批中心菜单必须跟随审批列表/详情路由高亮
    const { wrapper } = await mountLayout('/app/approvals')

    const selected = wrapper
      .findAll('.n-menu-item-content')
      .find((element) => [...element.classes()].some((name) => name.includes('--selected')))
    expect(selected).toBeDefined()
    expect(selected!.text()).toContain('审批中心')
  })

  it('在 /app/knowledge 与详情页面时菜单高亮「知识库」', async () => {
    // 保护行为：知识库菜单必须跟随知识库列表/详情路由高亮
    const list = await mountLayout('/app/knowledge')
    const listSelected = list.wrapper
      .findAll('.n-menu-item-content')
      .find((element) => [...element.classes()].some((name) => name.includes('--selected')))
    expect(listSelected).toBeDefined()
    expect(listSelected!.text()).toContain('知识库')

    const detail = await mountLayout('/app/knowledge/doc-1')
    const detailSelected = detail.wrapper
      .findAll('.n-menu-item-content')
      .find((element) => [...element.classes()].some((name) => name.includes('--selected')))
    expect(detailSelected).toBeDefined()
    expect(detailSelected!.text()).toContain('知识库')
  })

  it('点击「客服对话」菜单跳转到聊天空状态页面', async () => {
    // 保护行为：主界面导航入口必须真正可达客服对话页面
    const { wrapper, router } = await mountLayout('/app')

    const item = wrapper
      .findAll('.n-menu-item-content')
      .find((element) => element.text().includes('客服对话'))
    expect(item).toBeDefined()
    await item!.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.name).toBe('chat')
  })

  it('点击「审批中心」菜单跳转到审批中心页面', async () => {
    // 保护行为：审批中心导航入口必须真正可达（覆盖需求 1）
    const { wrapper, router } = await mountLayout('/app')

    const item = wrapper
      .findAll('.n-menu-item-content')
      .find((element) => element.text().includes('审批中心'))
    expect(item).toBeDefined()
    await item!.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.name).toBe('approvals')
  })

  it('点击「知识库」菜单跳转到知识库列表页面', async () => {
    // 保护行为：知识库导航入口必须真正可达
    const { wrapper, router } = await mountLayout('/app')

    const item = wrapper
      .findAll('.n-menu-item-content')
      .find((element) => element.text().includes('知识库'))
    expect(item).toBeDefined()
    await item!.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.name).toBe('knowledge')
  })

  it('点击「成员管理」菜单跳转到成员管理页面并高亮', async () => {
    // 保护行为：成员管理已从占位改为真实入口，菜单高亮跟随 /app/members
    const { wrapper, router } = await mountLayout('/app')

    const item = wrapper
      .findAll('.n-menu-item-content')
      .find((element) => element.text().includes('成员管理'))
    expect(item).toBeDefined()
    await item!.trigger('click')
    // 成员管理页为懒加载路由组件：等待其加载完成后再断言路由落定
    await flushNavigation()

    expect(router.currentRoute.value.name).toBe('members')

    const selected = wrapper
      .findAll('.n-menu-item-content')
      .find((element) => [...element.classes()].some((name) => name.includes('--selected')))
    expect(selected).toBeDefined()
    expect(selected!.text()).toContain('成员管理')
  })
})
