/*
 * 审批 Store（Pinia）：审批列表、详情、决定、Run 状态与恢复。
 *
 * 事实与安全说明（与后端行为对齐）：
 *  - 审批列表/详情/Run 只属于「当前企业」：所有请求由 http 层自动携带
 *    X-Organization-ID，跨企业资源后端统一返回 404；
 *  - 决定接口的真实 HTTP 状态码 200/201/202 语义不同（幂等重放 / 首次且
 *    自动恢复成功 / 决定已保存但恢复失败），本 Store 按状态码分流提示与
 *    刷新动作，202 不是普通失败；
 *  - 决定与恢复都不做自动重试：重复提交由服务端幂等（200/409）处理；
 *  - 切换企业/退出登录时由 tenantReset 机制清理全部审批状态，旧企业的
 *    在途请求返回后被 epoch + 目标校验丢弃，不写入新企业；
 *  - 审批列表与详情**不持久化到 localStorage**（跨设备/跨企业禁用复用）。
 *
 * 轮询约定：
 *  - 列表 30 秒低频刷新 + Run 仅 queued/running 时 5 秒短周期刷新；
 *  - 仅页面可见时运行（tick 检查 document.visibilityState），页面隐藏/
 *    卸载/切换企业即停止；
 *  - 不允许请求重叠（tick 只在空闲时发起）；终态或 awaiting_approval 停止
 *    轮询；自动刷新失败静默，绝不覆盖用户正在查看的错误状态。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import * as actionApi from '@/api/action'
import type {
  ApprovalDetailResponse,
  ApprovalListItem,
  ApprovalStatusValue,
  DecisionRequestPayload,
  DecisionResponse,
  RunStatusResponse,
} from '@/api/actionTypes'
import { ApiError } from '@/api/errors'
import { useOrganizationStore } from '@/stores/organization'
import { registerTenantResetHandler } from '@/stores/tenantReset'
import { canResumeRun } from '@/utils/actionDisplay'

/** 审批列表每页条数（后端默认 20；无总数接口，按拉满推断还有更多）。 */
export const APPROVAL_PAGE_SIZE = 20

/** 审批列表低频刷新周期（毫秒）。 */
export const APPROVAL_LIST_REFRESH_MS = 30_000

/** Run（仅 queued/running）短周期刷新间隔（毫秒）。 */
export const APPROVAL_RUN_REFRESH_MS = 5_000

/** 审批筛选状态：null 表示全部（不传 status 查询参数）。 */
export type ApprovalFilter = ApprovalStatusValue | 'all'

/** 审批列表加载结果。 */
export type ApprovalListResult = 'ok' | 'error'

/** 审批详情加载结果：not-found 表示 404（不存在或已不可访问）。 */
export type ApprovalDetailResult = 'ok' | 'not-found' | 'error'

/** 决定提交结果：按真实 HTTP 状态码与错误分类。 */
export type DecisionSubmitResultKind =
  | 'created' // 201 首次决定且自动恢复成功
  | 'pending' // 202 决定已保存但工作流恢复失败（保留恢复入口）
  | 'idempotent' // 200 相同决定的幂等重放
  | 'conflict' // 409 已被其他操作处理
  | 'forbidden' // 403 角色不再是 admin
  | 'not-found' // 404 审批不存在或已不可访问
  | 'error' // 其它错误（含 422，交由表单映射字段）
  | 'ignored' // 请求已失效（企业切换/退出）或重复提交拦截

/** Run 恢复结果。 */
export type ResumeSubmitResultKind =
  | 'ok' // 恢复成功或幂等返回已有结果
  | 'conflict' // 409 不可恢复或状态冲突（已刷新）
  | 'retryable-failed' // 503 恢复失败但业务事实有效，可稍后重试
  | 'forbidden' // 403 角色不再是 admin
  | 'not-found' // 404 Run 不存在或已不可访问
  | 'error' // 其它错误
  | 'ignored' // 请求已失效或重复提交拦截

/** 决定提交的完整返回（供页面决定提示与表单错误映射）。 */
export interface DecisionSubmitOutcome {
  /** 决定结果分类 */
  result: DecisionSubmitResultKind
  /** 错误对象：result=error 且为 422 时携带字段错误，供表单映射 */
  error: ApiError | null
}

/** Run 恢复的完整返回。 */
export interface ResumeSubmitOutcome {
  /** 恢复结果分类 */
  result: ResumeSubmitResultKind
  /** 错误对象：result=error 时携带（503 在 retryable-failed 中） */
  error: ApiError | null
}

/** 判断错误是否为 ApiError。 */
function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

export const useApprovalStore = defineStore('approvals', () => {
  // —— 状态 ——

  /** 数据所属企业 id：请求发起时捕获，写入前校验（与 organization store 同步） */
  const organizationId = ref<string | null>(null)
  /** 当前筛选状态；默认 pending（进入审批中心默认看待审批） */
  const filterStatus = ref<ApprovalFilter>('pending')
  /** 审批列表（当前企业 + 当前筛选，无总数，按分页追加） */
  const approvals = ref<ApprovalListItem[]>([])
  /** 审批列表加载中 */
  const listLoading = ref(false)
  /** 审批列表加载错误消息；null 表示无错误 */
  const listError = ref<string | null>(null)
  /** 列表是否还可能存在下一页（按最近一页是否拉满推断，不伪造总数） */
  const listHasMore = ref(false)
  /** 当前审批详情；未加载或已失效为 null */
  const detail = ref<ApprovalDetailResponse | null>(null)
  /** 审批详情加载中 */
  const detailLoading = ref(false)
  /** 审批详情加载错误消息；null 表示无错误 */
  const detailError = ref<string | null>(null)
  /** 当前审批详情对应的目标审批 id（防止切换审批后旧响应覆盖） */
  const detailTargetId = ref<string | null>(null)
  /** Run 完整状态（从 detail.run.run_id 拉取） */
  const run = ref<RunStatusResponse | null>(null)
  /** Run 状态加载中 */
  const runLoading = ref(false)
  /** Run 状态加载错误消息；null 表示无错误 */
  const runError = ref<string | null>(null)
  /** 审批决定提交中（防重复点击） */
  const deciding = ref(false)
  /** Run 恢复提交中（防重复点击） */
  const resuming = ref(false)
  /** 最近一次决定提交结果（含真实状态码），202 时保留恢复入口依据 */
  const lastDecision = ref<{ status: number; data: DecisionResponse } | null>(null)
  /** 最近一次一次性提示（如 202/409/503 语义），视图展示后清除 */
  const notice = ref<string | null>(null)
  /** 请求失效标记：企业切换/退出登录时递增，旧响应据此丢弃 */
  const requestEpoch = ref(0)

  /** 内部定时器句柄：列表 30s 与 Run 5s 轮询（非响应式，浏览器返回 number）。 */
  let listRefreshTimer: number | null = null
  let runRefreshTimer: number | null = null

  // —— 派生状态 ——

  /** 当前用户在当前企业的角色是否为 admin（仅界面展示，后端仍实时校验） */
  const isAdmin = computed(() => useOrganizationStore().currentRole === 'admin')
  /** 是否显示决定表单：admin + 审批仍待审批 + 尚无已落库决定 */
  const canSubmitDecision = computed(
    () =>
      isAdmin.value &&
      detail.value?.approval_status === 'pending' &&
      detail.value?.decision === null,
  )
  /** 是否显示 Run 恢复入口：条件集中由 canResumeRun 派生 */
  const canResume = computed(() =>
    canResumeRun(run.value, isAdmin.value, lastDecision.value?.data.resume_required ?? false),
  )

  // —— 内部工具 ——

  /** 当前组织 store 快照：读取此刻的企业 id（无企业时为 null）。 */
  function captureOrganizationId(): string | null {
    const organizationStore = useOrganizationStore()
    return organizationStore.currentOrganizationId
  }

  /** 校验异步请求是否仍然有效：企业未切换、未退出、未被标记失效。 */
  function isRequestCurrent(epoch: number, capturedOrganizationId: string | null): boolean {
    return (
      requestEpoch.value === epoch &&
      organizationId.value !== null &&
      organizationId.value === capturedOrganizationId &&
      captureOrganizationId() === capturedOrganizationId
    )
  }

  /** 追加一次性提示（同文案不重复追加）。 */
  function setNotice(message: string): void {
    notice.value = message
  }

  /** 刷新当前角色状态：重新拉取企业列表（403 后角色可能已被移除/降级）。 */
  async function refreshCurrentRole(): Promise<void> {
    const organizationStore = useOrganizationStore()
    try {
      await organizationStore.load(true)
    } catch {
      // 角色刷新失败不阻断主流程，下一页请求仍会由后端再次校验
    }
  }

  // —— 租户重置 ——

  /** 切换企业/退出登录时的租户缓存清理：全部审批状态归零并失效旧请求。 */
  function resetForTenantChange(): void {
    requestEpoch.value += 1
    stopListAutoRefresh()
    stopRunAutoRefresh()
    organizationId.value = null
    filterStatus.value = 'pending'
    approvals.value = []
    listLoading.value = false
    listError.value = null
    listHasMore.value = false
    detail.value = null
    detailLoading.value = false
    detailError.value = null
    detailTargetId.value = null
    run.value = null
    runLoading.value = false
    runError.value = null
    deciding.value = false
    resuming.value = false
    lastDecision.value = null
    notice.value = null
  }

  // 注册到租户重置机制：企业切换或退出登录时自动清理审批状态
  registerTenantResetHandler(resetForTenantChange)

  // —— 审批列表 ——

  /** 加载审批列表（force=true 时忽略已有列表强制重载首页）。 */
  async function loadApprovals(force = false): Promise<ApprovalListResult> {
    if (listLoading.value) return 'error'
    if (!force && approvals.value.length > 0) return 'ok'
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) {
      listError.value = '请先选择企业'
      return 'error'
    }
    const epoch = requestEpoch.value
    const targetFilter = filterStatus.value
    organizationId.value = capturedOrganizationId
    listLoading.value = true
    listError.value = null
    try {
      const items = await actionApi.listApprovals({
        status: targetFilter === 'all' ? undefined : targetFilter,
        limit: APPROVAL_PAGE_SIZE,
        offset: 0,
      })
      if (
        !isRequestCurrent(epoch, capturedOrganizationId) ||
        filterStatus.value !== targetFilter
      ) {
        return 'ok' // 过期或已切换筛选：丢弃
      }
      approvals.value = items
      listHasMore.value = items.length >= APPROVAL_PAGE_SIZE
      return 'ok'
    } catch (error) {
      if (
        !isRequestCurrent(epoch, capturedOrganizationId) ||
        filterStatus.value !== targetFilter
      ) {
        return 'ok'
      }
      listError.value = error instanceof Error ? error.message : '审批列表加载失败'
      return 'error'
    } finally {
      if (requestEpoch.value === epoch) {
        listLoading.value = false
      }
    }
  }

  /** 切换筛选状态：清空列表并从 id=0 重新加载（切换筛选属于新查询）。 */
  async function setFilter(status: ApprovalFilter): Promise<ApprovalListResult> {
    if (filterStatus.value === status) return 'ok'
    filterStatus.value = status
    approvals.value = []
    listHasMore.value = false
    listError.value = null
    return loadApprovals()
  }

  /** 加载更多审批：offset 取当前已加载条数，按 approval_id 去重追加。 */
  async function loadMoreApprovals(): Promise<void> {
    if (listLoading.value || !listHasMore.value) return
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) return
    const epoch = requestEpoch.value
    const targetFilter = filterStatus.value
    organizationId.value = capturedOrganizationId
    listLoading.value = true
    try {
      const items = await actionApi.listApprovals({
        status: targetFilter === 'all' ? undefined : targetFilter,
        limit: APPROVAL_PAGE_SIZE,
        offset: approvals.value.length,
      })
      if (
        !isRequestCurrent(epoch, capturedOrganizationId) ||
        filterStatus.value !== targetFilter
      ) {
        return
      }
      // 合并去重：避免极端情况下同一审批出现在两页
      const known = new Set(approvals.value.map((item) => item.approval_id))
      const fresh = items.filter((item) => !known.has(item.approval_id))
      approvals.value = [...approvals.value, ...fresh]
      listHasMore.value = items.length >= APPROVAL_PAGE_SIZE
    } catch (error) {
      if (isRequestCurrent(epoch, capturedOrganizationId)) {
        listError.value = error instanceof Error ? error.message : '加载更多审批失败'
      }
    } finally {
      if (requestEpoch.value === epoch) {
        listLoading.value = false
      }
    }
  }

  /** 静默刷新首页：失败不影响用户查看的内容，也不覆盖已有错误状态。 */
  async function refreshApprovalsSilently(): Promise<void> {
    if (listLoading.value) return
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) return
    const epoch = requestEpoch.value
    const targetFilter = filterStatus.value
    listLoading.value = true
    try {
      const items = await actionApi.listApprovals({
        status: targetFilter === 'all' ? undefined : targetFilter,
        limit: APPROVAL_PAGE_SIZE,
        offset: 0,
      })
      if (
        !isRequestCurrent(epoch, capturedOrganizationId) ||
        filterStatus.value !== targetFilter
      ) {
        return
      }
      approvals.value = items
      listHasMore.value = items.length >= APPROVAL_PAGE_SIZE
    } catch {
      // 静默刷新失败不打扰用户；下一次手动刷新或轮询会重试
    } finally {
      if (requestEpoch.value === epoch) {
        listLoading.value = false
      }
    }
  }

  // —— 审批详情 ——

  /** 加载审批详情；404 时清理详情并提示「审批不存在或已不可访问」。 */
  async function loadApprovalDetail(approvalId: string): Promise<ApprovalDetailResult> {
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId || !approvalId) return 'error'
    if (detailTargetId.value !== approvalId) {
      // 切换目标审批：使旧目标的详情/Run 在途结果失效并清空旧内容
      detailTargetId.value = approvalId
      detail.value = null
      run.value = null
      runError.value = null
      detailError.value = null
    }
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    detailLoading.value = true
    detailError.value = null
    try {
      const data = await actionApi.getApprovalDetail(approvalId)
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'ok'
      if (detailTargetId.value !== approvalId) return 'ok' // 已切换到其它审批
      detail.value = data
      return 'ok'
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'ok'
      if (detailTargetId.value !== approvalId) return 'ok'
      if (isApiError(error) && error.status === 404) {
        detail.value = null
        run.value = null
        setNotice('审批不存在或已不可访问')
        return 'not-found'
      }
      detailError.value = error instanceof Error ? error.message : '审批详情加载失败'
      return 'error'
    } finally {
      if (requestEpoch.value === epoch) {
        detailLoading.value = false
      }
    }
  }

  // —— Run 状态 ——

  /** 加载 Run 完整状态（silent=true 时失败静默，供轮询使用）。 */
  async function loadRunStatus(runId: string, silent = false): Promise<'ok' | 'error' | 'not-found'> {
    if (!runId) return 'error'
    if (runLoading.value) return run.value ? 'ok' : 'error'
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) return 'error'
    const epoch = requestEpoch.value
    const targetApprovalId = detailTargetId.value
    organizationId.value = capturedOrganizationId
    runLoading.value = true
    if (!silent) runError.value = null
    try {
      const data = await actionApi.getRunStatus(runId)
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'ok'
      if (targetApprovalId !== null && detailTargetId.value !== targetApprovalId) return 'ok'
      run.value = data
      return 'ok'
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'ok'
      if (!silent) {
        if (isApiError(error) && error.status === 404) {
          run.value = null
          runError.value = '任务不存在或已不可访问'
        } else {
          runError.value = error instanceof Error ? error.message : 'Run 状态加载失败'
        }
      }
      return 'error'
    } finally {
      if (requestEpoch.value === epoch) {
        runLoading.value = false
      }
    }
  }

  // —— 审批决定 ——

  /** 决定提交后重新加载详情、Run 与列表缓存（保持三处一致）。 */
  async function reloadAfterDecision(outcome: { status: number; data: DecisionResponse }): Promise<void> {
    await loadApprovalDetail(outcome.data.approval_id)
    await loadRunStatus(outcome.data.run_id)
    await refreshApprovalsSilently()
  }

  /**
   * 提交审批决定（仅 admin，前端角色只用于展示）。
   *
   * 按真实 HTTP 状态码分流：
   *  - 201：首次决定且自动恢复成功 → 重新加载详情/Run/列表；
   *  - 202：决定已保存但恢复失败 → 保留决定摘要与恢复入口，重读服务端状态；
   *  - 200：相同决定幂等重放 → 只重读状态，不重复追加本地版本/决定；
   *  - 409：已被其他操作处理 → 立即重载详情与列表，不保留可提交旧表单；
   *  - 403：刷新企业列表与角色状态；
   *  - 422：返回错误对象，由表单映射字段并保留用户输入。
   */
  async function decide(
    approvalId: string,
    payload: DecisionRequestPayload,
  ): Promise<DecisionSubmitOutcome> {
    if (deciding.value) return { result: 'ignored', error: null } // 防重复提交
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId || !approvalId) return { result: 'error', error: null }
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    deciding.value = true
    notice.value = null // 提交开始：移除旧提示，避免新旧提示叠加
    try {
      const outcome = await actionApi.decideApproval(approvalId, payload)
      if (!isRequestCurrent(epoch, capturedOrganizationId)) {
        return { result: 'ignored', error: null }
      }
      lastDecision.value = outcome
      if (outcome.status === 201) {
        setNotice('审批已提交，工作流已恢复。')
        await reloadAfterDecision(outcome)
        return { result: 'created', error: null }
      }
      if (outcome.status === 202) {
        // 业务决定已保存、执行未完成：不是普通失败，保留恢复入口
        setNotice('审批决定已保存，但工作流恢复失败，可以稍后恢复。')
        await reloadAfterDecision(outcome)
        return { result: 'pending', error: null }
      }
      setNotice('该决定此前已经提交，已加载最新状态。')
      await reloadAfterDecision(outcome)
      return { result: 'idempotent', error: null }
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) {
        return { result: 'ignored', error: null }
      }
      if (isApiError(error)) {
        if (error.status === 409) {
          setNotice('该审批已被其他操作处理，正在刷新最新状态。')
          await loadApprovalDetail(approvalId)
          await refreshApprovalsSilently()
          return { result: 'conflict', error: null }
        }
        if (error.status === 403) {
          await refreshCurrentRole()
          setNotice('你的权限已经变化，已刷新当前企业角色。')
          return { result: 'forbidden', error: null }
        }
        if (error.status === 404) {
          detail.value = null
          run.value = null
          setNotice('审批不存在或已不可访问')
          return { result: 'not-found', error: null }
        }
        return { result: 'error', error }
      }
      return { result: 'error', error: null }
    } finally {
      if (requestEpoch.value === epoch) {
        deciding.value = false
      }
    }
  }

  // —— Run 恢复 ——

  /**
   * 显式恢复 Run（仅 admin）。
   *
   * 响应处理：200 刷新 Run/详情/列表；409 提示状态变化并刷新；
   * 503 提示「决定仍然有效，可稍后重试」且**不清除**决定/版本/审批事实；
   * 403 刷新角色并移除管理员操作能力。恢复请求不做自动重试。
   */
  async function resumeRun(runId: string): Promise<ResumeSubmitOutcome> {
    if (resuming.value) return { result: 'ignored', error: null } // 防重复提交
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId || !runId) return { result: 'error', error: null }
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    resuming.value = true
    try {
      await actionApi.resumeRun(runId)
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return { result: 'ignored', error: null }
      setNotice('执行已恢复，最新状态已刷新。')
      await loadRunStatus(runId)
      if (detailTargetId.value) {
        await loadApprovalDetail(detailTargetId.value)
      }
      await refreshApprovalsSilently()
      return { result: 'ok', error: null }
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return { result: 'ignored', error: null }
      if (isApiError(error)) {
        if (error.status === 409) {
          setNotice('状态已经变化或当前不可恢复，已刷新最新状态。')
          await loadRunStatus(runId)
          if (detailTargetId.value) {
            await loadApprovalDetail(detailTargetId.value)
          }
          return { result: 'conflict', error: null }
        }
        if (error.status === 503) {
          // 恢复失败但业务事实仍然有效：保留决定/版本/审批，稍后手动重试
          setNotice('本次恢复失败，但已经保存的审批决定仍然有效，可以稍后再试。')
          return { result: 'retryable-failed', error: null }
        }
        if (error.status === 403) {
          await refreshCurrentRole()
          setNotice('你的权限已经变化，已刷新当前企业角色。')
          return { result: 'forbidden', error: null }
        }
        if (error.status === 404) {
          setNotice('任务不存在或已不可访问')
          return { result: 'not-found', error: null }
        }
        return { result: 'error', error }
      }
      return { result: 'error', error: null }
    } finally {
      if (requestEpoch.value === epoch) {
        resuming.value = false
      }
    }
  }

  // —— 轮询（列表 30s / Run 5s，仅页面可见且空闲时） ——

  /** 启动列表低频刷新（先停止既有定时器；tick 内部再校验可见性与空闲）。 */
  function startListAutoRefresh(): void {
    stopListAutoRefresh()
    listRefreshTimer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return
      if (listLoading.value || listError.value) return // 不重叠、不覆盖用户看到的错误
      void refreshApprovalsSilently()
    }, APPROVAL_LIST_REFRESH_MS)
  }

  /** 停止列表低频刷新。 */
  function stopListAutoRefresh(): void {
    if (listRefreshTimer !== null) {
      window.clearInterval(listRefreshTimer)
      listRefreshTimer = null
    }
  }

  /**
   * 启动 Run 短周期刷新：仅当 Run 处于 queued/running 时发起；
   * 进入其它状态（含终态与 awaiting_approval）后自动停止。
   */
  function startRunAutoRefresh(runId: string): void {
    if (!runId) return
    stopRunAutoRefresh()
    runRefreshTimer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return
      if (runLoading.value || runError.value) return // 不重叠、不覆盖用户看到的错误
      const currentRun = run.value
      if (!currentRun || currentRun.run.run_id !== runId) return
      if (currentRun.run.status !== 'queued' && currentRun.run.status !== 'running') {
        // 终态或 awaiting_approval 不需要轮询：停止定时器
        stopRunAutoRefresh()
        return
      }
      void loadRunStatus(runId, true)
    }, APPROVAL_RUN_REFRESH_MS)
  }

  /** 停止 Run 短周期刷新。 */
  function stopRunAutoRefresh(): void {
    if (runRefreshTimer !== null) {
      window.clearInterval(runRefreshTimer)
      runRefreshTimer = null
    }
  }

  // —— 其它 ——

  /** 视图展示完一次性提示后调用（清除 notice）。 */
  function clearNotice(): void {
    notice.value = null
  }

  /** 测试与调试用：整体复位（含失效标记递增与轮询停止）。 */
  function reset(): void {
    resetForTenantChange()
  }

  return {
    // 状态
    organizationId,
    filterStatus,
    approvals,
    listLoading,
    listError,
    listHasMore,
    detail,
    detailLoading,
    detailError,
    run,
    runLoading,
    runError,
    deciding,
    resuming,
    lastDecision,
    notice,
    // 派生
    isAdmin,
    canSubmitDecision,
    canResume,
    // 列表
    loadApprovals,
    setFilter,
    loadMoreApprovals,
    refreshApprovalsSilently,
    // 详情与 Run
    loadApprovalDetail,
    loadRunStatus,
    // 决定与恢复
    decide,
    resumeRun,
    // 轮询
    startListAutoRefresh,
    stopListAutoRefresh,
    startRunAutoRefresh,
    stopRunAutoRefresh,
    // 其它
    clearNotice,
    reset,
  }
})
