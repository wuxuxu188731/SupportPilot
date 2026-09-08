/*
 * 企业选择页组件测试：企业列表渲染与进入、空状态创建引导。
 */

import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '@/router'
import { flushNavigation } from '@/test/routerHelpers'
import OrganizationsView from '@/views/OrganizationsView.vue'

vi.mock('@/api/auth', () => ({
  login: vi.fn(),
  register: vi.fn(),
  me: vi.fn(),
}))
vi.mock('@/api/organization', () => ({
  listOrganizations: vi.fn(),
  createOrganization: vi.fn(),
}))

import * as organizationApi from '@/api/organization'
import { useOrganizationStore } from '@/stores/organization'

/** 用消息提供者包裹被测页面并注入路由/Pinia。 */
async function mountOrganizationsView() {
  const router = createAppRouter(createMemoryHistory())
  const pinia = createPinia()
  const wrapper = mount(
    { render: () => h(NMessageProvider, null, { default: () => h(OrganizationsView) }) },
    {
      global: {
        plugins: [pinia, router],
      },
    },
  )
  // 等待路由初始导航完成，避免后续程序化跳转与初始导航竞争
  await router.isReady()
  return { wrapper, router }
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('企业选择页', () => {
  it('渲染企业列表：展示企业名称与当前角色', async () => {
    // 保护行为：企业卡片必须展示后端返回的名称与我的角色（覆盖页面列表）
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
      { organization_id: 'org-1', name: '甲企业', role: 'admin' },
      { organization_id: 'org-2', name: '乙企业', role: 'agent' },
    ])

    const { wrapper } = await mountOrganizationsView()
    await flushPromises()

    expect(wrapper.text()).toContain('甲企业')
    expect(wrapper.text()).toContain('乙企业')
    expect(wrapper.text()).toContain('管理员')
    expect(wrapper.text()).toContain('客服')
  })

  it('点击企业卡片：选择企业并跳转主界面（无 redirect 时）', async () => {
    // 保护行为：选择企业后应进入主界面，且企业成为当前企业
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
      { organization_id: 'org-1', name: '甲企业', role: 'admin' },
    ])

    const { wrapper, router } = await mountOrganizationsView()
    await flushPromises()

    await wrapper.find('.org-card').trigger('click')
    await flushPromises()
    // 等待懒加载目标路由组件就绪，导航落定后再断言
    await flushNavigation()

    expect(router.currentRoute.value.name).toBe('app')
    const store = useOrganizationStore()
    expect(store.currentOrganizationId).toBe('org-1')
  })

  it('无任何企业：展示空状态与创建引导', async () => {
    // 保护行为：新用户（无企业）应看到明确引导而不是空白或假数据
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([])

    const { wrapper } = await mountOrganizationsView()
    await flushPromises()

    expect(wrapper.text()).toContain('你还没有加入任何企业')
    expect(wrapper.text()).toContain('创建企业')
    expect(wrapper.find('.org-card').exists()).toBe(false)
  })

  it('企业列表加载失败：展示错误与重试按钮', async () => {
    // 保护行为：加载失败必须可见（错误 + 重试），页面不允许假装成功
    vi.mocked(organizationApi.listOrganizations).mockRejectedValueOnce(new Error('网络不可用'))
    vi.mocked(organizationApi.listOrganizations).mockResolvedValueOnce([
      { organization_id: 'org-1', name: '甲企业', role: 'admin' },
    ])

    const { wrapper } = await mountOrganizationsView()
    await flushPromises()

    expect(wrapper.text()).toContain('网络不可用')

    // 点击重试后加载成功
    const retryButton = wrapper.findAll('button').find((button) => button.text().includes('重试'))
    expect(retryButton).toBeDefined()
    await retryButton!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('甲企业')
  })
})
