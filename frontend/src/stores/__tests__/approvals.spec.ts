/*
 * 审批 Store 测试：列表筛选与分页（无总数、去重）、详情与 Run 加载、
 * 404 详情失效、决定 200/201/202/409/422/403 分流、Run 恢复 200/409/503/403、
 * 企业切换清理、旧企业迟到响应隔离，以及列表/Run 自动刷新（假定时器）。
 * 全程 mock API 层，不依赖任何真实模型或外部服务。
 */

import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  ApprovalDetailResponse,
  ApprovalListItem,
  DecisionResponse,
  DecisionSubmitResult,
  RunStatusResponse,
  RunView,
} from '@/api/actionTypes'
import { ORGANIZATION_STORAGE_KEY } from '@/stores/persistence'
import { runTenantResetHandlers } from '@/stores/tenantReset'
import { ApiError } from '@/api/errors'

vi.mock('@/api/action', () => ({
  listApprovals: vi.fn(),
  getApprovalDetail: vi.fn(),
  decideApproval: vi.fn(),
  getRunStatus: vi.fn(),
  resumeRun: vi.fn(),
}))
vi.mock('@/api/organization', () => ({
  listOrganizations: vi.fn(),
  createOrganization: vi.fn(),
}))

import * as actionApi from '@/api/action'
import * as organizationApi from '@/api/organization'
import { useApprovalStore } from '@/stores/approvals'
import { useOrganizationStore } from '@/stores/organization'

// —— 数据构造 ——

/** 构造一个版本视图对象。 */
function versionView(partial: Record<string, unknown> = {}) {
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

/** 构造审批列表项。 */
function approvalItem(approvalId: string, partial: Record<string, unknown> = {}): ApprovalListItem {
  return {
    approval_id: approvalId,
    proposal_id: `proposal-${approvalId}`,
    order_id: 'order-1',
    action_type: 'refund',
    approval_status: 'pending',
    run_status: 'awaiting_approval',
    current_version: versionView() as ApprovalListItem['current_version'],
    version_count: 1,
    decision: null,
    created_at: '2026-09-01T00:00:00Z',
    ...partial,
  } as ApprovalListItem
}

/** 构造审批详情。 */
function approvalDetail(approvalId = 'a-1', partial: Record<string, unknown> = {}): ApprovalDetailResponse {
  return {
    approval_id: approvalId,
    proposal_id: 'p-1',
    order_id: 'order-1',
    action_type: 'refund',
    approval_status: 'pending',
    run: {
      run_id: 'r-1',
      workflow_type: 'refund',
      status: 'awaiting_approval',
      created_by_user_id: 'u-1',
      last_error_code: null,
      last_error_retryable: false,
      created_at: '2026-09-01T00:00:00Z',
      updated_at: '2026-09-01T00:00:00Z',
      completed_at: null,
    } as RunView,
    requested_version: versionView() as ApprovalDetailResponse['requested_version'],
    current_version: versionView() as ApprovalDetailResponse['current_version'],
    versions: [versionView() as ApprovalDetailResponse['versions'][number]],
    decision: null,
    self_approved: false,
    created_at: '2026-09-01T00:00:00Z',
    ...partial,
  } as ApprovalDetailResponse
}

/** 构造 Run 完整状态（runPartial 可覆盖 Run 字段）。 */
function runStatus(
  runId = 'r-1',
  runPartial: Partial<RunView> = {},
  extra: Record<string, unknown> = {},
): RunStatusResponse {
  return {
    run: {
      run_id: runId,
      workflow_type: 'refund',
      status: 'awaiting_approval',
      created_by_user_id: 'u-1',
      last_error_code: null,
      last_error_retryable: false,
      created_at: '2026-09-01T00:00:00Z',
      updated_at: '2026-09-01T00:00:00Z',
      completed_at: null,
      ...runPartial,
    } as RunView,
    proposal: null,
    approval: null,
    decision: null,
    current_version: null,
    execution: null,
    result: null,
    ...extra,
  } as RunStatusResponse
}

/** 构造决定响应。 */
function decisionResponse(partial: Record<string, unknown> = {}): DecisionResponse {
  return {
    decision_id: 'd-1',
    decision: 'approved',
    approval_id: 'a-1',
    proposal_id: 'p-1',
    run_id: 'r-1',
    run_status: 'running',
    decided_version_id: 'v-1',
    decided_by_user_id: 'admin-u',
    comment: null,
    self_approved: true,
    resume_required: false,
    resume_error_code: null,
    created_at: '2026-09-01T01:00:00Z',
    ...partial,
  } as DecisionResponse
}

/** 生成带状态码与稳定码的 ApiError（模拟后端领域错误）。 */
function apiError(status: number, code: string | null = null, message = 'failed'): ApiError {
  return new ApiError({ status, code, message, fieldErrors: {}, raw: null })
}

// —— 环境准备 ——

/** 预置当前企业 + 角色（admin）。 */
function seedOrganization(organizationId = 'org-1', role: 'admin' | 'agent' = 'admin'): void {
  window.localStorage.setItem(
    ORGANIZATION_STORAGE_KEY,
    JSON.stringify({ organizationId }),
  )
  const organizationStore = useOrganizationStore()
  organizationStore.currentOrganizationId = organizationId
  organizationStore.organizations = [
    { organization_id: organizationId, name: '示例企业', role },
  ]
}

/** 默认 API mock：空列表 + 详情 + run 状态 + 决定响应。 */
function mockDefaultApis(): void {
  vi.mocked(actionApi.listApprovals).mockResolvedValue([])
  vi.mocked(actionApi.getApprovalDetail).mockResolvedValue(approvalDetail())
  vi.mocked(actionApi.getRunStatus).mockResolvedValue(runStatus())
  vi.mocked(actionApi.decideApproval).mockResolvedValue({ status: 201, data: decisionResponse() })
  vi.mocked(actionApi.resumeRun).mockResolvedValue({
    run: runStatus('r-1').run,
    resume_ok: true,
    error_code: null,
    result: { business_record_id: 'br-1', order_marked_refunded: true },
  })
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.useRealTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

// —— 列表：筛选与分页 ——

describe('审批列表：筛选与分页', () => {
  it('默认以 pending 筛选加载，参数为 limit=20、offset=0', async () => {
    // 保护行为：进入审批中心默认只看待审批（覆盖测试 1）
    vi.mocked(actionApi.listApprovals).mockResolvedValue([approvalItem('a-1')])
    const store = useApprovalStore()
    seedOrganization()

    await store.loadApprovals()

    expect(actionApi.listApprovals).toHaveBeenCalledWith({
      status: 'pending',
      limit: 20,
      offset: 0,
    })
    expect(store.approvals).toHaveLength(1)
    expect(store.listLoading).toBe(false)
    expect(store.listError).toBeNull()
  })

  it('切换到「全部」时不传 status，并从 offset=0 重新加载', async () => {
    // 保护行为：切换筛选属于新查询，必须清空列表从 0 开始（覆盖测试 2）
    vi.mocked(actionApi.listApprovals).mockResolvedValue([])
    const store = useApprovalStore()
    seedOrganization()

    store.filterStatus = 'all'
    await store.loadApprovals()

    expect(actionApi.listApprovals).toHaveBeenCalledWith({
      limit: 20,
      offset: 0,
    })
  })

  it('按最近一页是否拉满判断还有更多（无总数接口）', async () => {
    // 保护行为：不伪造待审批总数，hasMore 只能按「返回数量 == limit」推断（覆盖测试 5）
    const fullPage = Array.from({ length: 20 }, (_, index) => approvalItem(`a-${index}`))
    vi.mocked(actionApi.listApprovals).mockResolvedValueOnce(fullPage)
    const store = useApprovalStore()
    seedOrganization()

    await store.loadApprovals()
    expect(store.listHasMore).toBe(true)
    expect(store.approvals).toHaveLength(20)

    // 下一页不足一页 → 到底
    vi.mocked(actionApi.listApprovals).mockResolvedValueOnce([approvalItem('a-20')])
    await store.loadMoreApprovals()
    expect(store.listHasMore).toBe(false)
  })

  it('加载更多：offset 取已加载条数并去重追加', async () => {
    // 保护行为：分页结果必须按 approval_id 去重（覆盖测试 3/4）
    const page1 = Array.from({ length: 20 }, (_, index) => approvalItem(`a-${index}`))
    vi.mocked(actionApi.listApprovals).mockResolvedValueOnce(page1)
    const store = useApprovalStore()
    seedOrganization()
    await store.loadApprovals()

    // 第二页包含一条重复数据
    vi.mocked(actionApi.listApprovals).mockResolvedValueOnce([
      approvalItem('a-5'),
      approvalItem('a-21'),
    ])
    await store.loadMoreApprovals()

    expect(actionApi.listApprovals).toHaveBeenLastCalledWith({
      status: 'pending',
      limit: 20,
      offset: 20,
    })
    expect(store.approvals).toHaveLength(21)
    const ids = store.approvals.map((item) => item.approval_id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('加载失败记录错误，retry 后恢复', async () => {
    // 保护行为：列表 loading/empty/error/retry 状态完整（覆盖测试 6）
    vi.mocked(actionApi.listApprovals).mockRejectedValueOnce(apiError(500))
    const store = useApprovalStore()
    seedOrganization()

    await store.loadApprovals()
    expect(store.listError).toBeTruthy()
    expect(store.listLoading).toBe(false)

    vi.mocked(actionApi.listApprovals).mockResolvedValueOnce([approvalItem('a-1')])
    await store.loadApprovals(true)
    expect(store.listError).toBeNull()
    expect(store.approvals).toHaveLength(1)
  })

  it('加载中重复调用被拦截', async () => {
    // 边界情况：防止列表请求重叠
    let resolveList!: (value: ApprovalListItem[]) => void
    vi.mocked(actionApi.listApprovals).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveList = resolve
        }),
    )
    const store = useApprovalStore()
    seedOrganization()

    const first = store.loadApprovals()
    await store.loadApprovals()
    expect(vi.mocked(actionApi.listApprovals)).toHaveBeenCalledTimes(1)
    resolveList([])
    await first
  })
})

// —— 详情与 Run ——

describe('审批详情与 Run 状态', () => {
  it('加载详情后按 run.run_id 加载 Run 完整状态', async () => {
    // 保护行为：详情页必须展示 Run 完整状态（覆盖测试 7/8）
    mockDefaultApis()
    const store = useApprovalStore()
    seedOrganization()

    await store.loadApprovalDetail('a-1')

    expect(store.detail?.approval_id).toBe('a-1')
    expect(actionApi.getApprovalDetail).toHaveBeenCalledWith('a-1')

    await store.loadRunStatus('r-1')
    expect(store.run?.run.run_id).toBe('r-1')
    expect(actionApi.getRunStatus).toHaveBeenCalledWith('r-1')
  })

  it('详情 404：清空详情与 Run，返回 not-found 并给出提示', async () => {
    // 保护行为：404 详情失效处理（覆盖测试 9）——列表页回退依据
    vi.mocked(actionApi.getApprovalDetail).mockRejectedValueOnce(
      apiError(404, 'APPROVAL_NOT_FOUND'),
    )
    const store = useApprovalStore()
    seedOrganization()
    store.detail = approvalDetail()
    store.run = runStatus()

    const result = await store.loadApprovalDetail('a-missing')

    expect(result).toBe('not-found')
    expect(store.detail).toBeNull()
    expect(store.run).toBeNull()
    expect(store.notice).toBe('审批不存在或已不可访问')
  })

  it('详情加载失败保留错误状态，可重试', async () => {
    // 边界情况：详情错误态与重试路径
    vi.mocked(actionApi.getApprovalDetail).mockRejectedValueOnce(apiError(500))
    const store = useApprovalStore()
    seedOrganization()

    await store.loadApprovalDetail('a-1')
    expect(store.detailError).toBeTruthy()
    expect(store.detail).toBeNull()

    await store.loadApprovalDetail('a-1')
    expect(store.detailError).toBeNull()
  })
})

// —— 审批决定 —200/201/202/409/422/403 ——

describe('审批决定：状态码分流', () => {
  beforeEach(() => {
    seedOrganization()
    mockDefaultApis()
  })

  it('201：提示已恢复并重新加载详情、Run 与列表缓存', async () => {
    // 保护行为：首次决定且自动恢复成功 → 三处刷新（覆盖测试 23）
    const store = useApprovalStore()

    const outcome = await store.decide('a-1', { decision: 'approved', changes: null, comment: null })

    expect(outcome).toMatchObject({ result: 'created' })
    expect(store.notice).toBe('审批已提交，工作流已恢复。')
    expect(store.lastDecision?.status).toBe(201)
    expect(actionApi.decideApproval).toHaveBeenCalledWith('a-1', {
      decision: 'approved',
      changes: null,
      comment: null,
    })
    expect(actionApi.getApprovalDetail).toHaveBeenCalled()
    expect(actionApi.getRunStatus).toHaveBeenCalled()
    expect(actionApi.listApprovals).toHaveBeenCalled()
  })

  it('202：决定已保存但恢复失败，保留恢复入口（resume_required）', async () => {
    // 保护行为：202 不是普通失败，提示「可稍后恢复」并保留恢复依据（覆盖测试 24）
    vi.mocked(actionApi.decideApproval).mockResolvedValueOnce({
      status: 202,
      data: decisionResponse({ resume_required: true, resume_error_code: 'CHECKPOINT_UNAVAILABLE' }),
    })
    const store = useApprovalStore()

    const outcome = await store.decide('a-1', { decision: 'approved', changes: null, comment: null })

    expect(outcome.result).toBe('pending')
    expect(store.notice).toContain('审批决定已保存')
    expect(store.notice).toContain('可以稍后恢复')
    expect(store.lastDecision?.data.resume_required).toBe(true)
    expect(store.lastDecision?.data.resume_error_code).toBe('CHECKPOINT_UNAVAILABLE')
  })

  it('200：相同决定幂等重放，提示已提交并加载最新状态', async () => {
    // 保护行为：200 只重读状态，不得重复追加本地版本/决定（覆盖测试 23）
    vi.mocked(actionApi.decideApproval).mockResolvedValueOnce({
      status: 200,
      data: decisionResponse({ decision_id: 'd-1' }),
    })
    const store = useApprovalStore()

    const outcome = await store.decide('a-1', { decision: 'approved', changes: null, comment: null })

    expect(outcome.result).toBe('idempotent')
    expect(store.notice).toBe('该决定此前已经提交，已加载最新状态。')
  })

  it('409：提示已被处理并立即刷新详情与列表', async () => {
    // 保护行为：409 后必须刷新服务端状态，不能保留可提交旧表单（覆盖测试 25）
    vi.mocked(actionApi.decideApproval).mockRejectedValueOnce(
      apiError(409, 'APPROVAL_ALREADY_DECIDED'),
    )
    const store = useApprovalStore()

    const outcome = await store.decide('a-1', { decision: 'approved', changes: null, comment: null })

    expect(outcome.result).toBe('conflict')
    expect(store.notice).toBe('该审批已被其他操作处理，正在刷新最新状态。')
    expect(actionApi.getApprovalDetail).toHaveBeenCalled()
    expect(actionApi.listApprovals).toHaveBeenCalled()
  })

  it('422：返回错误对象供表单映射；不清空详情', async () => {
    // 保护行为：422 字段错误回传表单，不清空用户输入（覆盖测试 26）
    const fieldError = new ApiError({
      status: 422,
      code: 'APPROVAL_INVALID_CHANGES',
      message: '修改内容不合法',
      fieldErrors: { amount_cents: 'value is not a valid integer' },
      raw: null,
    })
    vi.mocked(actionApi.decideApproval).mockRejectedValueOnce(fieldError)
    const store = useApprovalStore()
    store.detail = approvalDetail()

    const outcome = await store.decide('a-1', { decision: 'approved', changes: null, comment: null })

    expect(outcome.result).toBe('error')
    expect(outcome.error).toBe(fieldError)
    expect(store.detail).not.toBeNull() // 表单状态不受影响
    expect(store.deciding).toBe(false)
  })

  it('403：刷新企业角色并提示权限变化', async () => {
    // 保护行为：403 必须刷新当前企业列表与角色状态（覆盖测试 27）
    vi.mocked(actionApi.decideApproval).mockRejectedValueOnce(
      apiError(403, 'APPROVAL_ADMIN_REQUIRED'),
    )
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([])
    const store = useApprovalStore()

    const outcome = await store.decide('a-1', { decision: 'approved', changes: null, comment: null })

    expect(outcome.result).toBe('forbidden')
    expect(organizationApi.listOrganizations).toHaveBeenCalled()
  })

  it('提交期间重复调用被拦截（防重复点击）', async () => {
    // 边界情况：决定按钮 loading 之外的 store 层防护（覆盖测试 22）
    let resolveDecide!: (value: DecisionSubmitResult) => void
    vi.mocked(actionApi.decideApproval).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveDecide = resolve
        }),
    )
    const store = useApprovalStore()

    const first = store.decide('a-1', { decision: 'approved', changes: null, comment: null })
    const second = await store.decide('a-1', { decision: 'approved', changes: null, comment: null })

    expect(second.result).toBe('ignored')
    expect(vi.mocked(actionApi.decideApproval)).toHaveBeenCalledTimes(1)

    resolveDecide({ status: 201, data: decisionResponse() })
    await first
  })
})

// —— Run 恢复 ——

describe('Run 恢复', () => {
  beforeEach(() => {
    seedOrganization()
    mockDefaultApis()
  })

  it('200：提示成功并刷新 Run、详情与列表', async () => {
    // 保护行为：恢复成功刷新三处状态（覆盖测试 28）
    const store = useApprovalStore()

    const outcome = await store.resumeRun('r-1')

    expect(outcome.result).toBe('ok')
    expect(store.notice).toBe('执行已恢复，最新状态已刷新。')
    expect(actionApi.resumeRun).toHaveBeenCalledWith('r-1')
    expect(actionApi.getRunStatus).toHaveBeenCalled()
    expect(actionApi.listApprovals).toHaveBeenCalled()
    expect(store.resuming).toBe(false)
  })

  it('409：提示状态已变化并刷新 Run 与详情', async () => {
    // 保护行为：409 后刷新并隐藏/禁用旧恢复按钮（覆盖测试 29）
    vi.mocked(actionApi.resumeRun).mockRejectedValueOnce(
      apiError(409, 'RUN_NOT_RESUMABLE'),
    )
    const store = useApprovalStore()
    await store.loadApprovalDetail('a-1') // 详情已加载：恢复入口所在页面

    const outcome = await store.resumeRun('r-1')

    expect(outcome.result).toBe('conflict')
    expect(store.notice).toContain('状态已经变化或当前不可恢复')
    expect(actionApi.getRunStatus).toHaveBeenCalled()
    expect(actionApi.getApprovalDetail).toHaveBeenCalled()
  })

  it('503：保留决定/版本/审批事实并允许稍后重试', async () => {
    // 保护行为：503 不清除任何业务事实，按钮重新可用（覆盖测试 30）
    vi.mocked(actionApi.resumeRun).mockRejectedValueOnce(
      apiError(503, 'CHECKPOINT_UNAVAILABLE'),
    )
    const store = useApprovalStore()
    store.detail = approvalDetail()
    store.run = runStatus()
    store.lastDecision = {
      status: 202,
      data: decisionResponse({ resume_required: true, resume_error_code: 'CHECKPOINT_UNAVAILABLE' }),
    }
    const detailBefore = store.detail
    const runBefore = store.run

    const outcome = await store.resumeRun('r-1')

    expect(outcome.result).toBe('retryable-failed')
    expect(store.notice).toBe('本次恢复失败，但已经保存的审批决定仍然有效，可以稍后再试。')
    expect(store.detail).toBe(detailBefore)
    expect(store.run).toBe(runBefore)
    expect(store.lastDecision?.data.decision).toBe('approved')
    expect(store.resuming).toBe(false) // 允许用户稍后手动重试
  })

  it('403：刷新角色状态', async () => {
    // 保护行为：恢复 403 后移除管理员操作能力（覆盖测试 27 同规则）
    vi.mocked(actionApi.resumeRun).mockRejectedValueOnce(
      apiError(403, 'APPROVAL_ADMIN_REQUIRED'),
    )
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([])
    const store = useApprovalStore()

    const outcome = await store.resumeRun('r-1')

    expect(outcome.result).toBe('forbidden')
    expect(organizationApi.listOrganizations).toHaveBeenCalled()
  })
})

// —— 企业切换与迟到响应 ——

describe('企业切换与迟到响应隔离', () => {
  it('切换企业：审批状态全部清理（tenantReset 机制）', async () => {
    // 保护行为：企业切换清空列表/详情/Run/筛选/提示（覆盖测试 32）
    vi.mocked(actionApi.listApprovals).mockResolvedValue([approvalItem('a-1')])
    const store = useApprovalStore()
    seedOrganization('org-old')
    await store.loadApprovals()
    store.detail = approvalDetail()
    store.run = runStatus()
    store.lastDecision = { status: 201, data: decisionResponse() }
    store.notice = '旧提示'

    seedOrganization('org-new')
    await runTenantResetHandlers()

    expect(store.approvals).toEqual([])
    expect(store.detail).toBeNull()
    expect(store.run).toBeNull()
    expect(store.lastDecision).toBeNull()
    expect(store.notice).toBeNull()
    expect(store.organizationId).toBeNull()
    expect(store.filterStatus).toBe('pending')
  })

  it('旧企业迟到决定响应不污染新企业状态', async () => {
    // 保护行为：旧企业决定在途期间切换企业，返回后必须丢弃（覆盖测试 33）
    let resolveDecide!: (value: DecisionSubmitResult) => void
    vi.mocked(actionApi.decideApproval).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveDecide = resolve
        }),
    )
    const store = useApprovalStore()
    seedOrganization('org-old')

    const pending = store.decide('a-1', { decision: 'approved', changes: null, comment: null })

    seedOrganization('org-new')
    await runTenantResetHandlers()

    resolveDecide({ status: 201, data: decisionResponse() })
    await pending

    expect(store.lastDecision).toBeNull()
    expect(store.notice).toBeNull()
    expect(store.organizationId).toBeNull()
    expect(store.deciding).toBe(false)
  })
})

// —— 自动刷新（假定时器） ——

describe('自动刷新（假定时器）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mockDefaultApis()
  })

  it('列表轮询：30 秒低频刷新，加载中不重叠', async () => {
    // 保护行为：list 轮询仅在空闲时发起，不允许请求重叠
    const store = useApprovalStore()
    seedOrganization()
    await store.loadApprovals()
    expect(vi.mocked(actionApi.listApprovals)).toHaveBeenCalledTimes(1)

    store.startListAutoRefresh()
    // 第一次 tick 在 30 秒后
    await vi.advanceTimersByTimeAsync(30_000)
    expect(vi.mocked(actionApi.listApprovals)).toHaveBeenCalledTimes(2)

    // 加载中：tick 必须跳过（不重叠）
    store.listLoading = true
    await vi.advanceTimersByTimeAsync(30_000)
    expect(vi.mocked(actionApi.listApprovals)).toHaveBeenCalledTimes(2)

    store.listLoading = false
    await vi.advanceTimersByTimeAsync(30_000)
    expect(vi.mocked(actionApi.listApprovals)).toHaveBeenCalledTimes(3)

    store.stopListAutoRefresh()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(vi.mocked(actionApi.listApprovals)).toHaveBeenCalledTimes(3)
  })

  it('列表轮询：页面不可见时不刷新', async () => {
    // 保护行为：仅页面可见时运行（覆盖「页面不可见时不轮询」）
    const store = useApprovalStore()
    seedOrganization()
    await store.loadApprovals()

    store.startListAutoRefresh()
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    try {
      await vi.advanceTimersByTimeAsync(30_000)
      expect(vi.mocked(actionApi.listApprovals)).toHaveBeenCalledTimes(1)
    } finally {
      delete (document as { visibilityState?: string }).visibilityState
    }
  })

  it('列表轮询：用户正在查看的错误不被自动刷新覆盖', async () => {
    // 保护行为：自动刷新不得覆盖用户正在查看的错误
    const store = useApprovalStore()
    seedOrganization()
    store.listError = '加载失败'

    store.startListAutoRefresh()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(actionApi.listApprovals).not.toHaveBeenCalled()
  })

  it('Run 轮询：仅 queued/running 时刷新，终态自动停止', async () => {
    // 保护行为：Run 非终态短周期轮询，终态停止（覆盖「终态停止」）
    vi.mocked(actionApi.getRunStatus).mockResolvedValue(runStatus('r-1', { status: 'queued' }))
    const store = useApprovalStore()
    seedOrganization()
    store.detail = approvalDetail()
    await store.loadRunStatus('r-1')

    store.startRunAutoRefresh('r-1')
    await vi.advanceTimersByTimeAsync(5_000)
    expect(vi.mocked(actionApi.getRunStatus)).toHaveBeenCalledTimes(2)

    // Run 推进为终态：下一个 tick 停止轮询
    vi.mocked(actionApi.getRunStatus).mockResolvedValue(
      runStatus('r-1', { status: 'succeeded' }, {
        result: { business_record_id: 'br-1', order_marked_refunded: true },
      }),
    )
    await vi.advanceTimersByTimeAsync(5_000)
    const callsAfterSucceeded = vi.mocked(actionApi.getRunStatus).mock.calls.length
    await vi.advanceTimersByTimeAsync(15_000)
    expect(vi.mocked(actionApi.getRunStatus).mock.calls.length).toBe(callsAfterSucceeded)
  })

  it('Run 轮询：等待审批状态不轮询', async () => {
    // 保护行为：awaiting_approval 不需要轮询 Run（有决定后由主动刷新驱动）
    const store = useApprovalStore()
    seedOrganization()
    store.detail = approvalDetail()
    await store.loadRunStatus('r-1')

    store.startRunAutoRefresh('r-1')
    await vi.advanceTimersByTimeAsync(5_000)
    expect(vi.mocked(actionApi.getRunStatus)).toHaveBeenCalledTimes(1)
  })
})
