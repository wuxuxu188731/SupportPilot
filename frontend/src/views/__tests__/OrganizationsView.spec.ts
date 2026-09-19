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
import { ApiError } from '@/api/errors'
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
vi.mock('@/api/demoData', () => ({
  generateDemoData: vi.fn(),
}))
/** 消息提示 spy：只替换 useMessage，其余 naive-ui 组件保持真实实现。 */
const messageApi = {
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}
vi.mock('naive-ui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('naive-ui')>()
  return { ...actual, useMessage: () => messageApi }
})

import * as organizationApi from '@/api/organization'
import * as demoDataApi from '@/api/demoData'
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
    const pushSpy = vi.spyOn(router, 'push')

    await wrapper.find('.org-card-body').trigger('click')
    await flushPromises()
    // 等待懒加载目标路由组件就绪，导航落定后再断言
    await flushNavigation()

    expect(pushSpy).toHaveBeenCalledWith('/app')
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

  it('管理员企业卡片展示「生成测试数据」，客服卡片不展示', async () => {
    // 保护行为：demo 数据初始化是管理员能力，客服看不到入口（后端也会 403）；
    // 每个企业卡片各有一个按钮，避免多企业时把数据灌错企业。
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
      { organization_id: 'org-1', name: '甲企业', role: 'admin' },
      { organization_id: 'org-2', name: '乙企业', role: 'agent' },
    ])

    const { wrapper } = await mountOrganizationsView()
    await flushPromises()

    const buttons = wrapper.findAll('[data-test="generate-demo-data"]')
    expect(buttons).toHaveLength(1)
    expect(buttons[0].attributes('aria-label')).toBe('为 甲企业 生成测试数据')
  })

  it('点击生成测试数据：调用接口并展示新增明细与时间基准', async () => {
    // 保护行为：按钮必须带上该企业 id 调用 /demo-data/（此时本地可能还没有
    // 「当前企业」，不能依赖请求头自动注入），并把新增条数与时间基准回显给用户。
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
      { organization_id: 'org-1', name: '甲企业', role: 'admin' },
    ])
    vi.mocked(demoDataApi.generateDemoData).mockResolvedValue({
      organization_id: 'org-1',
      reference_at: '2026-09-19T13:02:54+00:00',
      counts: { customers: 2, orders: 3, shipments: 2, tickets: 2, ticket_comments: 3 },
      customer_nos: ['CUST-001', 'CUST-002'],
      order_nos: ['ORD-DELAY-001', 'ORD-TRANSIT-001', 'ORD-DELIVERED-001'],
      shipment_nos: ['SHP-TRANSIT-001', 'SHP-DELIVERED-001'],
      ticket_nos: ['TKT-DELAY-001', 'TKT-DAMAGE-001'],
    })

    const { wrapper, router } = await mountOrganizationsView()
    await flushPromises()
    const pushSpy = vi.spyOn(router, 'push')

    await wrapper.find('[data-test="generate-demo-data"]').trigger('click')
    await flushPromises()

    expect(demoDataApi.generateDemoData).toHaveBeenCalledWith('org-1')
    const summary = wrapper.find('[data-test="demo-data-summary"]').text()
    expect(summary).toContain('甲企业')
    expect(summary).toContain('新增 12 条')
    expect(summary).toContain('客户 2')
    expect(summary).toContain('工单 2')
    // 生成数据不等于进入企业：不跳转，由用户自己决定进入哪个企业
    expect(pushSpy).not.toHaveBeenCalled()
  })

  it('重复生成（新增为 0）：如实说明数据已存在，不假装新增', async () => {
    // 边界情况：幂等接口第二次返回全 0，界面不能把它说成「已生成 12 条」
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
      { organization_id: 'org-1', name: '甲企业', role: 'admin' },
    ])
    vi.mocked(demoDataApi.generateDemoData).mockResolvedValue({
      organization_id: 'org-1',
      reference_at: '2026-09-19T13:02:54+00:00',
      counts: { customers: 0, orders: 0, shipments: 0, tickets: 0, ticket_comments: 0 },
      customer_nos: ['CUST-001', 'CUST-002'],
      order_nos: ['ORD-DELAY-001', 'ORD-TRANSIT-001', 'ORD-DELIVERED-001'],
      shipment_nos: ['SHP-TRANSIT-001', 'SHP-DELIVERED-001'],
      ticket_nos: ['TKT-DELAY-001', 'TKT-DAMAGE-001'],
    })

    const { wrapper } = await mountOrganizationsView()
    await flushPromises()

    await wrapper.find('[data-test="generate-demo-data"]').trigger('click')
    await flushPromises()

    const summary = wrapper.find('[data-test="demo-data-summary"]').text()
    expect(summary).toContain('演示数据已存在')
    expect(summary).toContain('新增 0 条')
  })

  it('生成失败（409 冲突）：给出可操作的引导文案', async () => {
    // 边界情况：演示编号被业务操作改过时后端返回 409，
    // 页面必须把「为什么失败 + 怎么办」讲清楚，而不是只弹一句原始英文
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
      { organization_id: 'org-1', name: '甲企业', role: 'admin' },
    ])
    vi.mocked(demoDataApi.generateDemoData).mockRejectedValue(
      new ApiError({
        status: 409,
        code: null,
        message: 'ORD-DELIVERED-001 already exists with different data',
        fieldErrors: {},
        raw: null,
      }),
    )

    const { wrapper } = await mountOrganizationsView()
    await flushPromises()

    await wrapper.find('[data-test="generate-demo-data"]').trigger('click')
    await flushPromises()

    const errorText = messageApi.error.mock.calls.at(-1)?.[0] as string
    expect(errorText).toContain('ORD-DELIVERED-001')
    expect(errorText).toContain('请新建一个企业后重新生成')
    expect(wrapper.find('[data-test="demo-data-summary"]').exists()).toBe(false)
  })
})
