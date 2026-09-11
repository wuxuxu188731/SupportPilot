/*
 * ApprovalDetailView 页面集成测试：详情加载与 Run 完整状态、agent 只读、
 * admin 决定表单、已决定不显示表单、404 回列表、批准/202/恢复流转、
 * 503 保留重试能力、版本时间线比较与空态不显示 "null"。
 * mock API 层与登录/企业上下文，不触达网络。
 */

import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'
import type {
  ApprovalDetailResponse,
  DecisionSubmitResult,
  RunStatusResponse,
  RunView,
  VersionView,
} from '@/api/actionTypes'
import { createAppRouter, registerGuards } from '@/router'
import ApprovalDetailView from '@/views/ApprovalDetailView.vue'
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

/** 构造版本视图。 */
function version(partial: Partial<VersionView> = {}): VersionView {
  return {
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
    ...partial,
  }
}

/** 构造 Run 视图。 */
function runView(partial: Partial<RunView> = {}): RunView {
  return {
    run_id: 'r-1',
    workflow_type: 'refund',
    status: 'awaiting_approval',
    created_by_user_id: 'u-1',
    last_error_code: null,
    last_error_retryable: false,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    completed_at: null,
    ...partial,
  }
}

/** 构造 Run 完整状态响应。 */
function runStatus(partial: Partial<RunStatusResponse> = {}): RunStatusResponse {
  return {
    run: runView(),
    proposal: null,
    approval: null,
    decision: null,
    current_version: null,
    execution: null,
    result: null,
    ...partial,
  }
}

/** 构造审批详情响应。 */
function detailResponse(partial: Partial<ApprovalDetailResponse> = {}): ApprovalDetailResponse {
  return {
    approval_id: 'a-1',
    proposal_id: 'p-1',
    order_id: 'ORD-A-001',
    action_type: 'refund',
    approval_status: 'pending',
    run: runView(),
    requested_version: version(),
    current_version: version(),
    versions: [version()],
    decision: null,
    self_approved: false,
    created_at: '2026-09-01T00:00:00Z',
    ...partial,
  }
}

/** 构造已决定的详情。 */
function decidedDetail(): ApprovalDetailResponse {
  return detailResponse({
    approval_status: 'approved',
    decision: {
      decision_id: 'd-1',
      decision: 'approved',
      decided_version_id: 'v-1',
      decided_by_user_id: 'admin-u-1',
      comment: '同意退款',
      created_at: '2026-09-02T00:00:00Z',
    },
    self_approved: true,
  })
}

/** 构造决定响应（status 参数模拟 201/202/200 分流）。 */
function decisionSubmit(status: number, partial: Record<string, unknown> = {}): DecisionSubmitResult {
  return {
    status,
    data: {
      decision_id: 'd-1',
      decision: 'approved',
      approval_id: 'a-1',
      proposal_id: 'p-1',
      run_id: 'r-1',
      run_status: 'running',
      decided_version_id: 'v-1',
      decided_by_user_id: 'admin-u-1',
      comment: null,
      self_approved: true,
      resume_required: false,
      resume_error_code: null,
      created_at: '2026-09-02T00:00:00Z',
      ...partial,
    },
  }
}

/** 把 UTC 时刻按本地时区渲染为 "YYYY-MM-DD HH:mm"（与页面展示口径一致）。 */
function localDateTime(utcIso: string): string {
  const date = new Date(utcIso)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function seedLogin(role: 'admin' | 'agent' = 'admin'): void {
  window.localStorage.setItem(
    AUTH_STORAGE_KEY,
    JSON.stringify({ accessToken: 'token-1', tokenType: 'bearer', expiresIn: 1800, savedAt: Date.now() }),
  )
  window.localStorage.setItem(
    ORGANIZATION_STORAGE_KEY,
    JSON.stringify({ organizationId: 'org-1' }),
  )
  vi.mocked(organizationApi.listOrganizations).mockResolvedValue([
    { organization_id: 'org-1', name: '示例企业', role },
  ])
}

/** 挂载审批详情页并导航到指定路径（注册守卫，让企业上下文真实加载）。 */
async function mountDetailView(path = '/app/approvals/a-1') {
  const router = createAppRouter(createMemoryHistory())
  const pinia = createPinia()
  setActivePinia(pinia)
  registerGuards(router)
  await router.push(path)
  await router.isReady()
  const wrapper = mount(
    {
      render: () =>
        h(NDialogProvider, null, {
          default: () => h(NMessageProvider, null, { default: () => h(ApprovalDetailView) }),
        }),
    },
    {
      global: { plugins: [pinia, router], stubs: { teleport: true } },
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
  vi.mocked(actionApi.listApprovals).mockResolvedValue([])
  vi.mocked(actionApi.getApprovalDetail).mockResolvedValue(detailResponse())
  vi.mocked(actionApi.getRunStatus).mockResolvedValue(runStatus())
  vi.mocked(actionApi.resumeRun).mockResolvedValue({
    run: runView({ status: 'succeeded' }),
    resume_ok: true,
    error_code: null,
    result: { business_record_id: 'br-1', order_marked_refunded: true },
  })
})

describe('ApprovalDetailView 详情加载', () => {
  it('加载详情并展示基本信息、请求版本与 Run 完整状态', async () => {
    // 保护行为：详情页必须展示基本信息、请求版本与 Run 完整状态（覆盖测试 7/8）
    seedLogin()
    vi.mocked(actionApi.getRunStatus).mockResolvedValue(
      runStatus({
        run: runView({ status: 'succeeded', last_error_code: null }),
        execution: {
          execution_id: 'e-1',
          proposal_id: 'p-1',
          proposal_version_id: 'v-1',
          action_type: 'refund',
          status: 'succeeded',
          attempt_count: 2,
          error_code: null,
          error_retryable: false,
          claimed_at: '2026-09-01T00:00:00Z',
          updated_at: '2026-09-01T00:00:00Z',
          completed_at: '2026-09-01T00:01:00Z',
        },
        result: { business_record_id: 'br-1', order_marked_refunded: true },
      }),
    )
    const { wrapper } = await mountDetailView()

    // 详情与 Run 均从服务端加载
    expect(actionApi.getApprovalDetail).toHaveBeenCalledWith('a-1')
    expect(actionApi.getRunStatus).toHaveBeenCalledWith('r-1')

    const text = wrapper.text()
    expect(text).toContain('a-1')
    expect(text).toContain('p-1')
    expect(text).toContain('ORD-A-001')
    expect(text).toContain('¥60.00')
    expect(text).toContain('已成功')
    expect(text).toContain('br-1')
    expect(text).toContain('已标记订单为退款状态（模拟）')
    // 可空字段不能显示字符串 "null" / "undefined"
    expect(text).not.toContain('null')
    expect(text).not.toContain('undefined')
  })

  it('agent 角色：显示只读说明，无决定表单与恢复按钮', async () => {
    // 保护行为：agent 只读（覆盖测试 10）——不用 CSS 隐藏，而是如实展示说明
    seedLogin('agent')
    vi.mocked(actionApi.getRunStatus).mockResolvedValue(
      runStatus({ run: runView({ status: 'failed', last_error_retryable: true }) }),
    )
    const { wrapper } = await mountDetailView()

    expect(wrapper.text()).toContain('你当前是客服角色')
    expect(wrapper.text()).toContain('只有管理员可以作出决定或恢复执行')
    expect(wrapper.find('[data-test="decision-form"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="resume-row"]').exists()).toBe(false)
  })

  it('admin + 待审批 + 无决定：显示决定表单', async () => {
    // 保护行为：admin 在待审批且无决定时展示表单（覆盖测试 11）
    seedLogin('admin')
    const { wrapper } = await mountDetailView()

    expect(wrapper.find('[data-test="decision-form"]').exists()).toBe(true)
  })

  it('已决定审批：只展示决定摘要，不显示可提交表单', async () => {
    // 保护行为：已决定审批不能继续展示可提交表单（覆盖测试 12）
    seedLogin('admin')
    vi.mocked(actionApi.getApprovalDetail).mockResolvedValue(decidedDetail())
    const { wrapper } = await mountDetailView()

    expect(wrapper.find('[data-test="decision-summary"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('批准')
    expect(wrapper.text()).toContain('同意退款')
    expect(wrapper.text()).toContain('是（提案人与决定人为同一用户）')
    expect(wrapper.find('[data-test="decision-form"]').exists()).toBe(false)
  })

  it('详情 404：返回审批列表（提示由列表页承接）', async () => {
    // 保护行为：404 时返回审批列表并提示（覆盖测试 9/路由要求 6）
    seedLogin()
    vi.mocked(actionApi.getApprovalDetail).mockRejectedValue(
      new ApiError({ status: 404, code: 'APPROVAL_NOT_FOUND', message: 'not found', fieldErrors: {}, raw: null }),
    )
    const { router } = await mountDetailView()
    await flushNavigation()

    expect(router.currentRoute.value.name).toBe('approvals')
  })

  it('版本时间线：相邻版本标出前后差异，初始版本不标修改', async () => {
    // 保护行为：版本历史适合比较；同一版本不得误标「发生修改」（覆盖详情 3）
    seedLogin()
    vi.mocked(actionApi.getApprovalDetail).mockResolvedValue(
      detailResponse({
        current_version: version({ version_id: 'v-2', version_no: 2, amount_cents: 800 }),
        versions: [
          version(),
          version({ version_id: 'v-2', version_no: 2, amount_cents: 800, reason_text: '部分退款' }),
        ],
      }),
    )
    const { wrapper } = await mountDetailView()

    const text = wrapper.text()
    expect(text).toContain('初始版本（提案创建）')
    expect(text).toContain('相对上一版本的修改')
    expect(text).toContain('¥60.00 → ¥8.00')
    // 当前版本标记为 V2 且高亮
    expect(text).toContain('当前生效：V2')
  })
})

describe('ApprovalDetailView 时间展示（H-06）', () => {
  it('详情、版本时间线、决定与 Run 时间统一按本地时区展示，不原样透出 UTC ISO 文本', async () => {
    // 保护行为：同一系统内时间口径必须一致（列表页与成员页均已本地化），
    // 详情页不得把后端 UTC ISO 串（含 T/Z）直接透出，否则用户读到的时间比本地早 8 小时
    seedLogin()
    vi.mocked(actionApi.getApprovalDetail).mockResolvedValue(
      detailResponse({
        approval_status: 'approved',
        created_at: '2026-09-11T02:21:32Z',
        requested_version: version({ created_at: '2026-09-11T02:21:32Z' }),
        versions: [
          version({ created_at: '2026-09-11T02:21:32Z' }),
          version({ version_id: 'v-2', version_no: 2, created_at: '2026-09-11T02:23:00Z' }),
        ],
        current_version: version({ version_id: 'v-2', version_no: 2, created_at: '2026-09-11T02:23:00Z' }),
        decision: {
          decision_id: 'd-1',
          decision: 'approved',
          decided_version_id: 'v-2',
          decided_by_user_id: 'u-1',
          comment: '同意',
          created_at: '2026-09-11T02:24:09Z',
        },
      }),
    )
    vi.mocked(actionApi.getRunStatus).mockResolvedValue(
      runStatus({
        run: runView({
          status: 'succeeded',
          created_at: '2026-09-11T02:21:32Z',
          updated_at: '2026-09-11T02:24:10Z',
          completed_at: '2026-09-11T02:24:11Z',
        }),
      }),
    )
    const { wrapper } = await mountDetailView()
    const text = wrapper.text()

    // 原始 UTC ISO 文本不得原样出现（含 T/Z 后缀）
    expect(text).not.toContain('2026-09-11T02:21:32Z')
    expect(text).not.toContain('2026-09-11T02:24:09Z')
    expect(text).not.toContain('2026-09-11T02:23:00Z')

    // 基本信息 / 请求版本 / 版本时间线 / 决定摘要 / Run 面板均按本地时区展示
    expect(wrapper.find('[data-test="detail-created-at"]').text()).toBe(
      localDateTime('2026-09-11T02:21:32Z'),
    )
    expect(wrapper.find('[data-test="version-item-1"]').text()).toContain(
      localDateTime('2026-09-11T02:21:32Z'),
    )
    expect(wrapper.find('[data-test="version-item-2"]').text()).toContain(
      localDateTime('2026-09-11T02:23:00Z'),
    )
    expect(wrapper.find('[data-test="decision-created-at"]').text()).toBe(
      localDateTime('2026-09-11T02:24:09Z'),
    )
    expect(wrapper.find('[data-test="run-created-at"]').text()).toBe(
      localDateTime('2026-09-11T02:21:32Z'),
    )
    expect(wrapper.find('[data-test="run-updated-at"]').text()).toBe(
      localDateTime('2026-09-11T02:24:10Z'),
    )
    expect(wrapper.find('[data-test="run-completed-at"]').text()).toBe(
      localDateTime('2026-09-11T02:24:11Z'),
    )
  })

  it('Run 未完成时完成时间显示「—」，不显示原始空值', async () => {
    // 边界情况：未完成 Run 的 completed_at 为空，必须显示占位符而不是空白或 "null"
    seedLogin()
    vi.mocked(actionApi.getRunStatus).mockResolvedValue(
      runStatus({ run: runView({ completed_at: null }) }),
    )
    const { wrapper } = await mountDetailView()

    expect(wrapper.find('[data-test="run-completed-at"]').text()).toBe('—')
  })
})

describe('ApprovalDetailView 决定流转', () => {
  beforeEach(() => {
    seedLogin('admin')
    vi.mocked(actionApi.decideApproval).mockResolvedValue(decisionSubmit(201))
  })

  it('批准：确认后发请求，201 后重新加载详情并展示决定摘要', async () => {
    // 保护行为：确认前不发请求、201 分流（覆盖测试 21/23）
    vi.mocked(actionApi.getApprovalDetail)
      .mockResolvedValueOnce(detailResponse()) // 首次进入：待审批
      .mockResolvedValueOnce(decidedDetail()) // 201 后重载：已决定
    const { wrapper } = await mountDetailView()

    // 填写备注并打开确认框
    await wrapper.find('[data-test="comment-input"]').setValue('同意退款')
    await wrapper.find('[data-test="submit-decision"]').trigger('click')
    await flushPromises()

    expect(actionApi.decideApproval).not.toHaveBeenCalled()

    await wrapper.find('[data-test="confirm-decision"]').trigger('click')
    await flushPromises()

    expect(actionApi.decideApproval).toHaveBeenCalledWith('a-1', {
      decision: 'approved',
      changes: null,
      comment: '同意退款',
    })
    // reload 后详情变为已决定：表单消失、决定摘要出现
    expect(wrapper.find('[data-test="decision-summary"]').exists()).toBe(true)
  })

  it('202：提示决定已保存，保留恢复入口', async () => {
    // 保护行为：202 视为「业务决定已保存、执行未完成」（覆盖测试 24）
    vi.mocked(actionApi.decideApproval).mockResolvedValue(
      decisionSubmit(202, { resume_required: true, resume_error_code: 'CHECKPOINT_UNAVAILABLE' }),
    )
    const { wrapper } = await mountDetailView()

    await wrapper.find('[data-test="submit-decision"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-test="confirm-decision"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('审批决定已保存，但工作流恢复失败，可以稍后恢复。')
    expect(wrapper.find('[data-test="resume-row"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('CHECKPOINT_UNAVAILABLE')
  })

  it('409：提示已被其他操作处理并刷新', async () => {
    // 保护行为：409 后不能保留可提交旧表单（覆盖测试 25）
    vi.mocked(actionApi.decideApproval).mockRejectedValue(
      new ApiError({ status: 409, code: 'APPROVAL_ALREADY_DECIDED', message: 'conflict', fieldErrors: {}, raw: null }),
    )
    vi.mocked(actionApi.getApprovalDetail)
      .mockResolvedValueOnce(detailResponse()) // 首次进入：待审批（有表单）
      .mockResolvedValue(decidedDetail()) // 409 后重载：已被他人决定
    const { wrapper } = await mountDetailView()

    await wrapper.find('[data-test="submit-decision"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-test="confirm-decision"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('该审批已被其他操作处理')
    expect(wrapper.find('[data-test="decision-summary"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="decision-form"]').exists()).toBe(false)
  })

  it('422：错误映射到表单且不清空用户输入', async () => {
    // 保护行为：422 错误显示在对应表单、保留输入（覆盖测试 26）
    vi.mocked(actionApi.decideApproval).mockRejectedValue(
      new ApiError({
        status: 422,
        code: 'APPROVAL_INVALID_CHANGES',
        message: '修改内容不合法',
        fieldErrors: { amount_cents: 'value is not a valid integer' },
        raw: null,
      }),
    )
    const { wrapper } = await mountDetailView()

    await wrapper.find('[data-test="mode-approved_with_changes"]').setValue(true)
    await flushPromises()
    await wrapper.find('[data-test="amount-input"]').setValue('12.34')
    await wrapper.find('[data-test="submit-decision"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-test="confirm-decision"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="amount-error"]').text()).toContain('value is not a valid integer')
    expect((wrapper.find('[data-test="amount-input"]').element as HTMLInputElement).value).toBe('12.34')
    expect(wrapper.find('[data-test="decision-form"]').exists()).toBe(true)
  })
})

describe('ApprovalDetailView Run 恢复', () => {
  beforeEach(() => {
    seedLogin('admin')
    // 可重试失败：恢复按钮应出现
    vi.mocked(actionApi.getRunStatus).mockResolvedValue(
      runStatus({ run: runView({ status: 'failed', last_error_retryable: true }) }),
    )
  })

  it('恢复成功：刷新 Run 状态并提示', async () => {
    // 保护行为：Run 恢复成功（覆盖测试 28）
    const { wrapper } = await mountDetailView()

    expect(wrapper.find('[data-test="resume-row"]').exists()).toBe(true)
    await wrapper.find('[data-test="resume-run"]').trigger('click')
    await flushPromises()

    expect(actionApi.resumeRun).toHaveBeenCalledWith('r-1')
    expect(wrapper.text()).toContain('执行已恢复，最新状态已刷新。')
  })

  it('恢复 409：提示状态变化并刷新', async () => {
    // 保护行为：恢复 409 后刷新并隐藏/禁用旧恢复按钮（覆盖测试 29）
    vi.mocked(actionApi.resumeRun).mockRejectedValue(
      new ApiError({ status: 409, code: 'RUN_NOT_RESUMABLE', message: 'not resumable', fieldErrors: {}, raw: null }),
    )
    const { wrapper } = await mountDetailView()

    await wrapper.find('[data-test="resume-run"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('状态已经变化或当前不可恢复')
    expect(actionApi.getRunStatus).toHaveBeenCalled()
  })

  it('恢复 503：保留决定事实，按钮可稍后重试', async () => {
    // 保护行为：恢复 503 不清除决定，允许稍后手动重试（覆盖测试 30）
    vi.mocked(actionApi.resumeRun).mockRejectedValue(
      new ApiError({ status: 503, code: 'CHECKPOINT_UNAVAILABLE', message: 'unavailable', fieldErrors: {}, raw: null }),
    )
    const { wrapper } = await mountDetailView()

    await wrapper.find('[data-test="resume-run"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('本次恢复失败，但已经保存的审批决定仍然有效，可以稍后再试。')
    // 决定/版本/审批事实仍在
    expect(wrapper.find('[data-test="decision-form"]').exists()).toBe(true)
    // 按钮重新可用（resuming 结束）
    expect(wrapper.find('[data-test="resume-run"]').exists()).toBe(true)
    expect((wrapper.find('[data-test="resume-run"]').element as HTMLButtonElement).disabled).toBe(false)
  })
})
