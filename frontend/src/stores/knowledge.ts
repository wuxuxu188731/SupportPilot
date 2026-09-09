/*
 * 知识库 Store（Pinia）：文档列表、详情、本地搜索/筛选/排序、上传、
 * 版本管理、停用/启用、入库任务查询与轮询。
 *
 * 事实与安全说明（与后端行为对齐）：
 *  - 文档列表/详情/任务只属于「当前企业」：所有请求由 http 层自动携带
 *    X-Organization-ID，跨企业资源后端统一返回 404；
 *  - 后端列表接口无分页、无搜索参数：搜索/筛选/排序全部为**前端本地
 *    处理**（作用于已加载的完整列表），不得向后端发送不存在的查询参数；
 *  - 上传请求使用独立长超时且**不自动重试**：超时/断网后提示「结果状态
 *    可能不确定」，由用户刷新文档列表后自行决定是否重新上传；
 *  - 上传回执按 status 分流：succeeded 视为成功；deduplicated=true 表示
 *    内容与当前有效版本相同（未创建重复版本）；queued/running 保存
 *    job_id 并启动受控轮询；failed 查询任务详情；
 *  - 入库任务轮询：仅 queued/running 时每 5 秒查询一次，仅页面可见时
 *    运行（tick 内检查 visibilityState），页面隐藏即暂停、恢复可见后
 *    继续、卸载/切换企业即停止；不允许请求重叠；终态停止；
 *  - 当前接口**没有任务列表**：刷新页面后若未保存 job_id，只能通过文档
 *    详情中的 latest_job 恢复有效版本最近一次任务——本 Store 不把
 *    job_id 写入 localStorage，也不伪造任务历史；
 *  - 切换企业/退出登录时由 tenantReset 机制清理全部知识库状态，旧企业
 *    在途请求返回后被 epoch + 目标校验丢弃，不写入新企业；
 *  - 知识库数据**不写入 localStorage**（跨设备/跨企业禁用复用）。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import * as knowledgeApi from '@/api/knowledge'
import type {
  DocumentSourceTypeValue,
  DocumentStatusValue,
  IngestionJobInfo,
  IngestionReceipt,
  KnowledgeDocumentDetail,
  KnowledgeDocumentSummary,
} from '@/api/knowledgeTypes'
import { ApiError } from '@/api/errors'
import { useOrganizationStore } from '@/stores/organization'
import { registerTenantResetHandler } from '@/stores/tenantReset'

/** 入库任务轮询间隔（毫秒）。 */
export const KNOWLEDGE_JOB_POLL_MS = 5_000

/** 状态筛选值：all 表示不过滤（本地筛选，不发送给后端）。 */
export type KnowledgeStatusFilter = DocumentStatusValue | 'all'

/** 来源类型筛选值：all 表示不过滤（本地筛选）。 */
export type KnowledgeSourceFilter = DocumentSourceTypeValue | 'all'

/** 列表排序方式（本地排序，不发送给后端）。 */
export type KnowledgeSortBy = 'updated-desc' | 'created-asc' | 'created-desc' | 'title-asc'

/** 上传提交结果分类（供页面提示与跳转）。 */
export type KnowledgeUploadOutcome =
  | { result: 'succeeded' | 'deduplicated' | 'pending' | 'failed'; receipt: IngestionReceipt }
  | { result: 'conflict' | 'forbidden' | 'not-found' | 'timeout-uncertain' | 'error'; receipt: null }

/** 停用/启用结果分类。 */
export type KnowledgeStatusActionResult =
  | 'ok'
  | 'conflict'
  | 'forbidden'
  | 'not-found'
  | 'service-unavailable'
  | 'error'
  | 'ignored'

/** 判断错误是否为 ApiError。 */
function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

/** 通用的稳定安全错误消息展示：优先 code/message，不展示堆栈。 */
function safeErrorMessage(error: unknown, fallback: string): string {
  if (isApiError(error)) return error.message || fallback
  return error instanceof Error ? error.message : fallback
}

export const useKnowledgeStore = defineStore('knowledge', () => {
  // —— 状态 ——

  /** 数据所属企业 id：请求发起时捕获，写入前校验（与 organization store 同步） */
  const organizationId = ref<string | null>(null)
  /** 当前企业全部文档摘要（后端无分页，一次加载完整列表） */
  const documents = ref<KnowledgeDocumentSummary[]>([])
  /** 文档列表加载中 */
  const documentsLoading = ref(false)
  /** 文档列表加载错误消息；null 表示无错误 */
  const documentsError = ref<string | null>(null)
  /** 当前本地搜索词（作用于 title 与 document_id，本地过滤） */
  const searchText = ref('')
  /** 当前状态筛选（本地过滤）；all 表示不过滤 */
  const statusFilter = ref<KnowledgeStatusFilter>('all')
  /** 当前来源类型筛选（本地过滤）；all 表示不过滤 */
  const sourceTypeFilter = ref<KnowledgeSourceFilter>('all')
  /** 当前排序方式（本地排序；默认「最近更新」） */
  const sortBy = ref<KnowledgeSortBy>('updated-desc')
  /** 当前查看的文档 id（详情页） */
  const currentDocumentId = ref<string | null>(null)
  /** 当前文档详情；未加载或已失效为 null */
  const documentDetail = ref<KnowledgeDocumentDetail | null>(null)
  /** 文档详情加载中 */
  const detailLoading = ref(false)
  /** 文档详情加载错误消息；null 表示无错误 */
  const detailError = ref<string | null>(null)
  /** 新文档上传进行中（防重复提交） */
  const uploadSubmitting = ref(false)
  /** 新版本上传进行中（防重复提交） */
  const versionUploadSubmitting = ref(false)
  /** 最近一次上传回执（视图展示用；企业切换时清空） */
  const uploadReceipt = ref<IngestionReceipt | null>(null)
  /** 当前入库任务（轮询/查询结果）；企业切换时清空 */
  const ingestionJob = ref<IngestionJobInfo | null>(null)
  /** 入库任务加载中（区分手动查询与轮询） */
  const jobLoading = ref(false)
  /** 正在执行停用/启用的文档 id 集合（防重复提交） */
  const statusUpdatingDocumentIds = ref<string[]>([])
  /** 最近一次一次性提示（视图展示后清除） */
  const notice = ref<string | null>(null)
  /** 请求失效标记：企业切换/退出登录时递增，旧响应据此丢弃 */
  const requestEpoch = ref(0)

  /** 内部定时器句柄：入库任务轮询（非响应式，浏览器返回 number）。 */
  let jobPollTimer: number | null = null

  // —— 派生状态 ——

  /** 当前用户在当前企业的角色是否为 admin（仅界面展示，后端仍实时校验） */
  const isAdmin = computed(() => useOrganizationStore().currentRole === 'admin')

  /** 本地搜索/筛选/排序后的文档列表（纯前端处理，不请求后端）。 */
  const filteredDocuments = computed(() => {
    const query = searchText.value.trim().toLowerCase()
    const list = documents.value.filter((item) => {
      if (statusFilter.value !== 'all' && item.status !== statusFilter.value) return false
      if (sourceTypeFilter.value !== 'all' && item.source_type !== sourceTypeFilter.value) return false
      if (query) {
        const haystack = `${item.title}\n${item.document_id}`.toLowerCase()
        if (!haystack.includes(query)) return false
      }
      return true
    })
    const sorted = [...list]
    switch (sortBy.value) {
      case 'created-asc':
        // 后端默认返回创建时间升序；并列时按 document_id 稳定排序
        sorted.sort((a, b) => a.created_at.localeCompare(b.created_at) || a.document_id.localeCompare(b.document_id))
        break
      case 'created-desc':
        sorted.sort((a, b) => b.created_at.localeCompare(a.created_at) || a.document_id.localeCompare(b.document_id))
        break
      case 'title-asc':
        sorted.sort((a, b) => a.title.localeCompare(b.title, 'zh-Hans-CN') || a.document_id.localeCompare(b.document_id))
        break
      case 'updated-desc':
      default:
        // 默认「最近更新」：更新时间倒序，并列按文档 id 稳定排序
        sorted.sort((a, b) => b.updated_at.localeCompare(a.updated_at) || a.document_id.localeCompare(b.document_id))
        break
    }
    return sorted
  })

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

  /** 追加一次性提示。 */
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

  /** 当前文档是否正在执行状态变更（防重复提交）。 */
  function isStatusUpdating(documentId: string): boolean {
    return statusUpdatingDocumentIds.value.includes(documentId)
  }

  // —— 租户重置 ——

  /** 切换企业/退出登录时的租户缓存清理：全部知识库状态归零并失效旧请求。 */
  function resetForTenantChange(): void {
    requestEpoch.value += 1
    stopJobPolling()
    organizationId.value = null
    documents.value = []
    documentsLoading.value = false
    documentsError.value = null
    searchText.value = ''
    statusFilter.value = 'all'
    sourceTypeFilter.value = 'all'
    sortBy.value = 'updated-desc'
    currentDocumentId.value = null
    documentDetail.value = null
    detailLoading.value = false
    detailError.value = null
    uploadSubmitting.value = false
    versionUploadSubmitting.value = false
    uploadReceipt.value = null
    ingestionJob.value = null
    jobLoading.value = false
    statusUpdatingDocumentIds.value = []
    notice.value = null
  }

  // 注册到租户重置机制：企业切换或退出登录时自动清理知识库状态
  registerTenantResetHandler(resetForTenantChange)

  // —— 文档列表 ——

  /**
   * 加载文档列表（force=true 强制刷新；否则已有数据时跳过）。
   * 后端一次返回全部文档（无分页）。
   */
  async function loadDocuments(force = false): Promise<void> {
    if (documentsLoading.value) return
    if (!force && documents.value.length > 0) return
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) {
      documentsError.value = '请先选择企业'
      return
    }
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    documentsLoading.value = true
    documentsError.value = null
    try {
      const list = await knowledgeApi.listKnowledgeDocuments()
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return
      documents.value = list
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return
      documentsError.value = safeErrorMessage(error, '知识库文档列表加载失败')
    } finally {
      if (requestEpoch.value === epoch) {
        documentsLoading.value = false
      }
    }
  }

  /** 手动刷新：忽略已有数据强制重载（失败展示错误与重试）。 */
  async function refreshDocuments(): Promise<void> {
    await loadDocuments(true)
  }

  // —— 本地搜索/筛选/排序 ——

  /** 设置本地搜索词（仅改变过滤视图，不发起网络请求）。 */
  function setSearchText(text: string): void {
    searchText.value = text
  }

  /** 设置状态筛选（本地处理）。 */
  function setStatusFilter(filter: KnowledgeStatusFilter): void {
    statusFilter.value = filter
  }

  /** 设置来源类型筛选（本地处理）。 */
  function setSourceTypeFilter(filter: KnowledgeSourceFilter): void {
    sourceTypeFilter.value = filter
  }

  /** 设置排序方式（本地处理）。 */
  function setSortBy(sort: KnowledgeSortBy): void {
    sortBy.value = sort
  }

  // —— 文档详情 ——

  /** 加载文档详情；404 时清理详情并提示「文档不存在或已不可访问」。 */
  async function loadDocumentDetail(
    documentId: string,
    silent = false,
  ): Promise<'ok' | 'not-found' | 'error'> {
    if (!documentId) return 'error'
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) return 'error'
    const epoch = requestEpoch.value
    if (currentDocumentId.value !== documentId) {
      // 切换目标文档：使旧文档的在途结果失效并清空旧内容
      currentDocumentId.value = documentId
      documentDetail.value = null
      ingestionJob.value = null
      detailError.value = null
    }
    organizationId.value = capturedOrganizationId
    detailLoading.value = true
    if (!silent) detailError.value = null
    try {
      const data = await knowledgeApi.getKnowledgeDocumentDetail(documentId)
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'ok'
      if (currentDocumentId.value !== documentId) return 'ok' // 已切换到其它文档
      documentDetail.value = data
      // 详情自带有效版本最近任务：同步更新当前任务状态（迟到响应安全）
      ingestionJob.value = data.latest_job
      return 'ok'
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'ok'
      if (currentDocumentId.value !== documentId) return 'ok'
      if (isApiError(error) && error.status === 404) {
        documentDetail.value = null
        ingestionJob.value = null
        if (!silent) setNotice('文档不存在或已不可访问')
        return 'not-found'
      }
      if (!silent) {
        detailError.value = safeErrorMessage(error, '文档详情加载失败')
      }
      return 'error'
    } finally {
      if (requestEpoch.value === epoch) {
        detailLoading.value = false
      }
    }
  }

  // —— 上传新文档 ——

  /** 上传回执兜底分流：succeeded / deduplicated / queued+running / failed。 */
  function handleUploadReceipt(
    receipt: IngestionReceipt,
    context: 'document' | 'version',
  ): 'succeeded' | 'deduplicated' | 'pending' | 'failed' {
    uploadReceipt.value = receipt
    if (receipt.deduplicated) {
      setNotice('上传内容与当前有效版本相同，未创建重复版本。')
      return 'deduplicated'
    }
    if (receipt.status === 'succeeded') {
      setNotice(context === 'document' ? '文档上传并入库成功。' : '新版本已上传并激活，文档已刷新。')
      return 'succeeded'
    }
    if (receipt.status === 'queued' || receipt.status === 'running') {
      setNotice('上传已提交，知识正在后台入库，任务开始轮询。')
      void loadIngestionJob(receipt.job_id)
      startJobPolling(receipt.job_id)
      return 'pending'
    }
    // failed：展示安全失败状态并查询任务详情
    setNotice('上传已提交，但入库失败，请查看任务详情。')
    void loadIngestionJob(receipt.job_id)
    return 'failed'
  }

  /**
   * 上传新文档（仅 admin；store 只负责请求与分流，文件校验在表单层完成）。
   *
   * 返回结果分类供页面决定提示与跳转（succeeded 后进入详情）；
   * 任何失败都不会清空表单（由表单层保留标题与文件）。
   */
  async function uploadNewDocument(input: {
    title: string
    file: File
  }): Promise<KnowledgeUploadOutcome> {
    if (uploadSubmitting.value) return { result: 'error', receipt: null } // 防重复提交
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) return { result: 'error', receipt: null }
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    uploadSubmitting.value = true
    notice.value = null // 提交开始：移除旧提示
    try {
      const receipt = await knowledgeApi.uploadKnowledgeDocument(input)
      if (!isRequestCurrent(epoch, capturedOrganizationId)) {
        return { result: 'error', receipt: null } // 迟到响应丢弃
      }
      const kind = handleUploadReceipt(receipt, 'document')
      // 无论哪种回执都刷新列表；succeeded 时刷新详情交给页面跳转
      void refreshDocuments()
      if (kind === 'succeeded') {
        void loadDocumentDetail(receipt.document_id)
      }
      return { result: kind, receipt }
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) {
        return { result: 'error', receipt: null }
      }
      return classifyUploadError(error)
    } finally {
      if (requestEpoch.value === epoch) {
        uploadSubmitting.value = false
      }
    }
  }

  /** 上传错误分类（403/404/409/422/503/超时断网）。 */
  function classifyUploadError(error: unknown): KnowledgeUploadOutcome {
    if (isApiError(error)) {
      if (error.status === 403) {
        void refreshCurrentRole()
        setNotice('你的权限已经变化，已刷新当前企业角色。')
        return { result: 'forbidden', receipt: null }
      }
      if (error.status === 404) {
        setNotice('文档不存在或已不可访问')
        return { result: 'not-found', receipt: null }
      }
      if (error.status === 409) {
        setNotice('文档状态已经变化，正在刷新最新状态。')
        void refreshDocuments()
        if (currentDocumentId.value) {
          void loadDocumentDetail(currentDocumentId.value, true)
        }
        return { result: 'conflict', receipt: null }
      }
      if (error.status === 503) {
        // 入库基础设施失败：不显示成「没有知识」，如实提示稍后重试
        setNotice('入库服务暂时不可用（文档信息已由服务端记录），请稍后重试。')
        return { result: 'error', receipt: null }
      }
      if (error.status === 0) {
        // 超时/断网/连接中断：结果状态不确定，不许自动重试
        setNotice('结果状态可能不确定，请先刷新文档列表，再决定是否重新上传。')
        return { result: 'timeout-uncertain', receipt: null }
      }
      // 其余（含 422）：保留表单并展示稳定 code/message；422 字段错误由表单映射
      setNotice(safeErrorMessage(error, '上传失败，请检查后重试'))
      return { result: 'error', receipt: null }
    }
    setNotice('上传失败，请检查后重试')
    return { result: 'error', receipt: null }
  }

  // —— 上传新版本 ——

  /**
   * 为既有文档上传新版本（仅 admin）。
   *
   * deduplicated=true 时不添加任何本地虚假版本，只刷新详情；
   * 超时/断网时先刷新详情（latest_job 是任务状态的可信来源）再提示不确定。
   */
  async function uploadNewVersion(
    documentId: string,
    input: {
      title: string
      file: File
    },
  ): Promise<KnowledgeUploadOutcome> {
    if (versionUploadSubmitting.value) return { result: 'error', receipt: null } // 防重复提交
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId || !documentId) return { result: 'error', receipt: null }
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    versionUploadSubmitting.value = true
    notice.value = null
    try {
      const receipt = await knowledgeApi.uploadKnowledgeDocumentVersion(documentId, input)
      if (!isRequestCurrent(epoch, capturedOrganizationId)) {
        return { result: 'error', receipt: null }
      }
      const kind = handleUploadReceipt(receipt, 'version')
      // 刷新列表与详情，保持三处一致
      void refreshDocuments()
      void loadDocumentDetail(documentId, true)
      return { result: kind, receipt }
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) {
        return { result: 'error', receipt: null }
      }
      if (isApiError(error) && error.status === 0) {
        // 先刷新详情：latest_job 是刷新后恢复任务状态的唯一可信来源
        void loadDocumentDetail(documentId, true)
        void refreshDocuments()
        return classifyUploadError(error)
      }
      return classifyUploadError(error)
    } finally {
      if (requestEpoch.value === epoch) {
        versionUploadSubmitting.value = false
      }
    }
  }

  // —— 停用 / 启用 ——

  /** 停用文档（仅 active 文档可停用；仅 admin 入口）。 */
  async function disableDocument(documentId: string): Promise<KnowledgeStatusActionResult> {
    return changeDocumentStatus(documentId, 'disable')
  }

  /** 启用文档（仅 disabled 文档可启用；仅 admin 入口）。 */
  async function enableDocument(documentId: string): Promise<KnowledgeStatusActionResult> {
    return changeDocumentStatus(documentId, 'enable')
  }

  /** 停用/启用统一实现：防重复、403/404/409/503 分流、成功后刷新列表与详情。 */
  async function changeDocumentStatus(
    documentId: string,
    action: 'disable' | 'enable',
  ): Promise<KnowledgeStatusActionResult> {
    if (!documentId) return 'error'
    if (isStatusUpdating(documentId)) return 'ignored' // 防重复提交
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) return 'error'
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    statusUpdatingDocumentIds.value = [...statusUpdatingDocumentIds.value, documentId]
    try {
      if (action === 'disable') {
        await knowledgeApi.disableKnowledgeDocument(documentId)
      } else {
        await knowledgeApi.enableKnowledgeDocument(documentId)
      }
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'ignored'
      setNotice(action === 'disable' ? '文档已停用，不再参与知识检索。' : '文档已启用，重新参与知识检索。')
      // 成功后刷新列表与详情（详情此时可能正在加载其它文档，静默刷新）
      await refreshDocuments()
      if (currentDocumentId.value === documentId) {
        await loadDocumentDetail(documentId, true)
      }
      return 'ok'
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'ignored'
      return classifyStatusActionError(error)
    } finally {
      if (requestEpoch.value === epoch) {
        statusUpdatingDocumentIds.value = statusUpdatingDocumentIds.value.filter(
          (id) => id !== documentId,
        )
      }
    }
  }

  /** 停用/启用错误分类（403 刷新角色 / 404 提示不可访问 / 409 刷新最新状态 / 503 保留事实）。 */
  function classifyStatusActionError(error: unknown): KnowledgeStatusActionResult {
    if (isApiError(error)) {
      if (error.status === 403) {
        void refreshCurrentRole()
        setNotice('你的权限已经变化，已刷新当前企业角色。')
        return 'forbidden'
      }
      if (error.status === 404) {
        setNotice('文档不存在或已不可访问')
        void refreshDocuments()
        return 'not-found'
      }
      if (error.status === 409) {
        setNotice('文档状态已经变化，正在刷新最新状态。')
        void refreshDocuments()
        if (currentDocumentId.value) {
          void loadDocumentDetail(currentDocumentId.value, true)
        }
        return 'conflict'
      }
      if (error.status === 503) {
        // 保留当前服务端事实：文档状态仍以刚刚返回的为准，稍后重试
        setNotice('服务暂时不可用，文档当前状态未改变，请稍后重试。')
        return 'service-unavailable'
      }
      setNotice(safeErrorMessage(error, '操作失败，请稍后重试'))
      return 'error'
    }
    setNotice('操作失败，请稍后重试')
    return 'error'
  }

  // —— 入库任务 ——

  /** 查询单个入库任务（轮询时 silent=true 失败静默）。 */
  async function loadIngestionJob(
    jobId: string,
    silent = false,
  ): Promise<'ok' | 'not-found' | 'error'> {
    if (!jobId) return 'error'
    if (jobLoading.value) return ingestionJob.value ? 'ok' : 'error' // 不重叠
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) return 'error'
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    jobLoading.value = true
    try {
      const job = await knowledgeApi.getIngestionJob(jobId)
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'ok'
      ingestionJob.value = job
      return 'ok'
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'ok'
      if (isApiError(error) && error.status === 404) {
        if (!silent) setNotice('入库任务不存在或已不可访问')
        return 'not-found'
      }
      if (!silent) {
        setNotice(safeErrorMessage(error, '入库任务查询失败'))
      }
      return 'error'
    } finally {
      if (requestEpoch.value === epoch) {
        jobLoading.value = false
      }
    }
  }

  // —— 任务轮询 ——

  /**
   * 启动入库任务轮询：仅 queued/running 时每 5 秒查询；
   * tick 内校验页面可见性（隐藏即暂停）、请求空闲（不重叠）
   * 与任务状态（终态停止）；每次任务更新后同步刷新当前文档详情。
   */
  function startJobPolling(jobId: string): void {
    if (!jobId) return
    stopJobPolling()
    jobPollTimer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return // 页面隐藏：暂停
      if (jobLoading.value || !ingestionJob.value) return // 不重叠；尚未拿到首份任务
      if (ingestionJob.value.job_id !== jobId) return
      const status = ingestionJob.value.status
      if (status !== 'queued' && status !== 'running') {
        stopJobPolling() // succeeded/failed 等终态停止
        return
      }
      void pollJobOnce(jobId)
    }, KNOWLEDGE_JOB_POLL_MS)
  }

  /** 单次轮询：查询任务并同步刷新详情（静默，失败不打扰）。 */
  async function pollJobOnce(jobId: string): Promise<void> {
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) return
    const epoch = requestEpoch.value
    const targetDocumentId = currentDocumentId.value
    const result = await loadIngestionJob(jobId, true)
    if (!isRequestCurrent(epoch, capturedOrganizationId)) return
    const job = ingestionJob.value
    if (job && job.status !== 'queued' && job.status !== 'running') {
      stopJobPolling() // 终态停止
      setNotice(`入库任务${job.status === 'succeeded' ? '成功完成，文档可以用于知识检索。' : '失败，请查看任务详情。'}`)
    }
    // 每次任务更新后同步刷新文档详情（不受结果影响；404/切换由内部丢弃）
    if (targetDocumentId && (result === 'ok' || result === 'not-found')) {
      void loadDocumentDetail(targetDocumentId, true)
    }
  }

  /** 停止入库任务轮询。 */
  function stopJobPolling(): void {
    if (jobPollTimer !== null) {
      window.clearInterval(jobPollTimer)
      jobPollTimer = null
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
    documents,
    documentsLoading,
    documentsError,
    searchText,
    statusFilter,
    sourceTypeFilter,
    sortBy,
    currentDocumentId,
    documentDetail,
    detailLoading,
    detailError,
    uploadSubmitting,
    versionUploadSubmitting,
    uploadReceipt,
    ingestionJob,
    jobLoading,
    statusUpdatingDocumentIds,
    notice,
    // 派生
    isAdmin,
    filteredDocuments,
    // 列表与筛选
    loadDocuments,
    refreshDocuments,
    setSearchText,
    setStatusFilter,
    setSourceTypeFilter,
    setSortBy,
    // 详情
    loadDocumentDetail,
    // 上传
    uploadNewDocument,
    uploadNewVersion,
    // 停用/启用
    disableDocument,
    enableDocument,
    // 任务
    loadIngestionJob,
    startJobPolling,
    stopJobPolling,
    // 其它
    clearNotice,
    reset,
  }
})
