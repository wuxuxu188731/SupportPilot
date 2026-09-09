/*
 * ApprovalListView 页面集成测试：默认待审批筛选、列表项字段展示、
 * 空态/错误/重试、筛选切换、加载更多、进入详情、企业名称提示。
 * mock API 层与登录/企业上下文，不触达网络。
 */

import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'
import type { ApprovalListItem } from '@/api/actionTypes'
import { createAppRouter, registerGuards } from '@/router'
import ApprovalListView from '@/views/ApprovalListView.vue'
import { AUTH_STORAGE_KEY, ORGANIZATION_STORAGE_KEY } from '@/stores/persistence'
import { flushNavigation } from '@/test/routerHelpers'

vi.mock('@/api/auth', () => ({
  login: vi.fn(),
  register: vi.fn(),
  me: vi.fn(),
}))
vi.mock('@/api/organization', () => ({
  listOrganizations: vi.fn(),
  createOrganization: vi.fn(),
}))
vi.mock('@/api/action', () => ({
  listApprovals: vi.fn(),
  getApprovalDetail: vi.fn(),
  decideApproval: vi.fn(),
  getRunStatus: vi.fn(),
  resumeRun: vi.fn(),
}))

import * as actionApi from '@/api/action'
import * as authApi from '@/api/auth'
import * as organizationApi from '@/api/organization'

const USER = { user_id: 'u-1', username: 'alice', created_at: '2026-09-01 00:00:00' }
const ORG_LIST = [{ organization_id: 'org-1', name: '示例企业', role: 'admin' as const }]

/** 构造审批列表项。 */
function approvalItem(approvalId: string, partial: Partial<ApprovalListItem> = {}): ApprovalListItem {
  return {
    approval_id: approvalId,
    proposal_id: `proposal-${approvalId}`,
    order_id: 'ORD-A-001',
    action_type: 'refund',
    approval_status: 'pending',
    run_status: 'awaiting_approval',
    current_version: {
      version_id: 'v-1',
      version_no: 1,
      amount_cents: 6000,
      currency: 'CNY',
      reason_code: 'quality_issue',
      reason_text: '商品存在质量问题',
      refund_scope: 'partial',
      coupon_valid_days: null,
      created_by_user_id: 'u-1',
      created_at: '2026-09-01T00:00:00Z',
    },
    version_count: 1,
    decision: null,
    created_at: '2026-09-01T00:00:00Z',
    ...partial,
  }
}

/** 生成一页满 20 条的列表（分页「还有更多」场景）。 */
function fullPage(): ApprovalListItem[] {
  return Array.from({ length: 20 }, (_, index) => approvalItem(`a-${index}`))
}

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

/** 挂载审批列表页并导航到指定路径（注册守卫，让企业上下文真实加载）。 */
async function mountApprovalsView(path: string) {
  const router = createAppRouter(createMemoryHistory())
  const pinia = createPinia()
  setActivePinia(pinia)
  registerGuards(router)
  await router.push(path)
  await router.isReady()
  const wrapper = mount(
    { render: () => h(NDialogProvider, null, { default: () => h(NMessageProvider, null, { default: () => h(ApprovalListView) }) }) },
    {
      global: { plugins: [pinia, router] },
    },
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
  vi.mocked(actionApi.listApprovals).mockResolvedValue([])
})

describe('ApprovalListView 列表渲染', () => {
  it('默认以待审批筛选加载并渲染列表项字段', async () => {
    // 保护行为：列表项展示动作类型/金额/状态/订单/审批ID/版本（覆盖测试 1）
    vi.mocked(actionApi.listApprovals).mockResolvedValue([
      approvalItem('a-1'),
    ])
    seedLogin()
    const { wrapper } = await mountApprovalsView('/app/approvals')

    expect(actionApi.listApprovals).toHaveBeenCalledWith({
      status: 'pending',
      limit: 20,
      offset: 0,
    })
    expect(wrapper.find('[data-test="approval-list"]').exists()).toBe(true)
    const item = wrapper.find('[data-test="approval-item"]')
    expect(item.text()).toContain('退款')
    expect(item.text()).toContain('待审批')
    expect(item.text()).toContain('等待审批')
    expect(item.text()).toContain('¥60.00')
    expect(item.text()).toContain('ORD-A-001')
    expect(item.text()).toContain('a-1')
    expect(item.text()).toContain('V1 · 共 1 个版本')
    expect(wrapper.text()).toContain('示例企业')
  })

  it('空列表展示空态（不伪造待审批总数）', async () => {
    // 保护行为：无数据时展示明确空态（覆盖测试 6 的 empty）
    seedLogin()
    const { wrapper } = await mountApprovalsView('/app/approvals')

    expect(wrapper.find('[data-test="approval-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('暂无待审批提案')
    expect(wrapper.find('[data-test="refresh-approvals"]').exists()).toBe(true)
  })

  it('加载失败展示错误与重试，点击重试恢复', async () => {
    // 保护行为：error 状态 + retry 恢复（覆盖测试 6）
    vi.mocked(actionApi.listApprovals).mockRejectedValueOnce(new ApiError({
      status: 500, code: null, message: '服务器内部错误，请稍后重试', fieldErrors: {}, raw: null,
    }))
    seedLogin()
    const { wrapper } = await mountApprovalsView('/app/approvals')

    expect(wrapper.text()).toContain('审批列表加载失败')

    vi.mocked(actionApi.listApprovals).mockResolvedValueOnce([approvalItem('a-1')])
    await wrapper.find('[data-test="retry-approvals"]').trigger('click')
    await flushPromises()

    expect(actionApi.listApprovals).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-test="approval-list"]').exists()).toBe(true)
  })

  it('筛选 Tab 切换：全部不传 status，其它状态原样传给后端', async () => {
    // 保护行为：状态筛选切换从 offset=0 重新加载（覆盖测试 2）
    seedLogin()
    const { wrapper } = await mountApprovalsView('/app/approvals')

    const allTab = wrapper.find('.n-tabs-tab[data-name="all"]')
    expect(allTab.exists()).toBe(true)
    await allTab.trigger('click')
    await flushPromises()
    expect(actionApi.listApprovals).toHaveBeenLastCalledWith({ limit: 20, offset: 0 })

    await wrapper.find('.n-tabs-tab[data-name="rejected"]').trigger('click')
    await flushPromises()
    expect(actionApi.listApprovals).toHaveBeenLastCalledWith({
      status: 'rejected',
      limit: 20,
      offset: 0,
    })
    expect(wrapper.text()).toContain('暂无已拒绝审批')
  })

  it('有下一页时展示「加载更多」，点击加载下一页', async () => {
    // 保护行为：hasMore 由「返回数量 == limit」驱动（覆盖测试 3）
    vi.mocked(actionApi.listApprovals).mockResolvedValueOnce(fullPage())
    seedLogin()
    const { wrapper } = await mountApprovalsView('/app/approvals')

    const loadMore = wrapper.find('[data-test="load-more-approvals"]')
    expect(loadMore.exists()).toBe(true)

    vi.mocked(actionApi.listApprovals).mockResolvedValueOnce([approvalItem('a-20')])
    await loadMore.trigger('click')
    await flushPromises()

    expect(actionApi.listApprovals).toHaveBeenLastCalledWith({
      status: 'pending',
      limit: 20,
      offset: 20,
    })
    expect(wrapper.findAll('[data-test="approval-item"]')).toHaveLength(21)
  })

  it('点击列表项进入审批详情路由', async () => {
    // 保护行为：列表项详情入口跳转 /app/approvals/:approvalId（覆盖测试 7/8）
    vi.mocked(actionApi.listApprovals).mockResolvedValue([approvalItem('a-1')])
    seedLogin()
    const { wrapper, router } = await mountApprovalsView('/app/approvals')

    await wrapper.find('[data-test="approval-detail-link"]').trigger('click')
    await flushNavigation()

    expect(router.currentRoute.value.name).toBe('approval-detail')
    expect(router.currentRoute.value.params.approvalId).toBe('a-1')
  })

  it('手动刷新按钮强制重载当前筛选第一页', async () => {
    // 保护行为：手动刷新入口必须真正可用
    seedLogin()
    const { wrapper } = await mountApprovalsView('/app/approvals')

    await wrapper.find('[data-test="refresh-approvals"]').trigger('click')
    await flushPromises()

    expect(actionApi.listApprovals).toHaveBeenCalledWith({
      status: 'pending',
      limit: 20,
      offset: 0,
    })
  })

  it('已有决定摘要展示决定类型与备注', async () => {
    // 保护行为：已决定审批展示决定摘要（不伪造用户名）
    vi.mocked(actionApi.listApprovals).mockResolvedValue([
      approvalItem('a-1', {
        approval_status: 'approved',
        decision: {
          decision_id: 'd-1',
          decision: 'approved',
          decided_version_id: 'v-1',
          decided_by_user_id: 'admin-u-1',
          comment: '同意全额退款',
          created_at: '2026-09-02T00:00:00Z',
        },
      }),
    ])
    seedLogin()
    const { wrapper } = await mountApprovalsView('/app/approvals')

    const item = wrapper.find('[data-test="approval-item"]')
    expect(item.text()).toContain('已批准')
    expect(item.find('[data-test="approval-decision"]').text()).toContain('批准')
    expect(item.find('[data-test="approval-decision"]').text()).toContain('同意全额退款')
  })
})
