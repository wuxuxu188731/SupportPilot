/*
 * 知识库 Store 测试：列表加载、本地搜索/筛选/排序（不请求后端）、详情与
 * 404 处理、上传回执分流（succeeded/deduplicated/queued/failed）、
 * 上传防重复与不自动重试、403/409/422/超时处理、停用/启用、
 * 入库任务轮询（假定时器：仅 queued/running、终态停止、页面隐藏暂停、
 * 请求不重叠、终端态刷新详情）、企业切换清理与旧企业迟到响应隔离。
 * 全程 mock API 层，不依赖任何真实模型或外部服务。
 */

import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'
import type {
  IngestionJobInfo,
  IngestionReceipt,
  KnowledgeDocumentDetail,
  KnowledgeDocumentSummary,
} from '@/api/knowledgeTypes'
import { ORGANIZATION_STORAGE_KEY } from '@/stores/persistence'
import { runTenantResetHandlers } from '@/stores/tenantReset'
import { KNOWLEDGE_JOB_POLL_MS } from '@/stores/knowledge'

vi.mock('@/api/knowledge', () => ({
  listKnowledgeDocuments: vi.fn(),
  getKnowledgeDocumentDetail: vi.fn(),
  uploadKnowledgeDocument: vi.fn(),
  uploadKnowledgeDocumentVersion: vi.fn(),
  disableKnowledgeDocument: vi.fn(),
  enableKnowledgeDocument: vi.fn(),
  getIngestionJob: vi.fn(),
}))
vi.mock('@/api/organization', () => ({
  listOrganizations: vi.fn(),
  createOrganization: vi.fn(),
}))

import * as knowledgeApi from '@/api/knowledge'
import * as organizationApi from '@/api/organization'
import { useKnowledgeStore } from '@/stores/knowledge'
import { useOrganizationStore } from '@/stores/organization'

// —— 数据构造 ——

/** 构造文档摘要。 */
function documentItem(partial: Partial<KnowledgeDocumentSummary> = {}): KnowledgeDocumentSummary {
  return {
    document_id: 'doc-1',
    title: '售后政策',
    source_type: 'markdown',
    status: 'active',
    active_version_id: 'v-1',
    created_at: '2026-09-01T00:00:00.000000+00:00',
    updated_at: '2026-09-01T00:00:00.000000+00:00',
    ...partial,
  }
}

/** 构造文档详情。 */
function documentDetail(partial: Partial<KnowledgeDocumentDetail> = {}): KnowledgeDocumentDetail {
  return {
    ...documentItem(),
    versions: [],
    latest_job: null,
    ...partial,
  }
}

/** 构造入库任务。 */
function jobInfo(partial: Partial<IngestionJobInfo> = {}): IngestionJobInfo {
  return {
    job_id: 'job-1',
    document_id: 'doc-1',
    version_id: 'v-1',
    status: 'running',
    attempt_count: 1,
    error_code: null,
    error_message: null,
    started_at: '2026-09-01T00:00:01.000000+00:00',
    finished_at: null,
    created_at: '2026-09-01T00:00:00.000000+00:00',
    ...partial,
  }
}

/** 构造上传回执。 */
function receipt(partial: Partial<IngestionReceipt> = {}): IngestionReceipt {
  return {
    document_id: 'doc-1',
    version_id: 'v-1',
    job_id: 'job-1',
    status: 'succeeded',
    deduplicated: false,
    ...partial,
  }
}

/** 构造带状态码的 ApiError。 */
function apiError(status: number, code: string | null = null, message = 'failed'): ApiError {
  return new ApiError({ status, code, message, fieldErrors: {}, raw: null })
}

// —— 环境准备 ——

/** 预置当前企业 + 角色（默认 admin，可加第二个企业用于切换测试）。 */
function seedOrganization(
  organizationId = 'org-1',
  role: 'admin' | 'agent' = 'admin',
  extraOrganizations: { organization_id: string; name: string; role: 'admin' | 'agent' }[] = [],
): void {
  window.localStorage.setItem(
    ORGANIZATION_STORAGE_KEY,
    JSON.stringify({ organizationId }),
  )
  const organizationStore = useOrganizationStore()
  organizationStore.currentOrganizationId = organizationId
  organizationStore.organizations = [
    { organization_id: organizationId, name: '示例企业', role },
    ...extraOrganizations,
  ]
}

/** 默认 mock：空列表 + 普通详情。 */
function mockDefaultApis(): void {
  vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([])
  vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(documentDetail())
  vi.mocked(knowledgeApi.getIngestionJob).mockResolvedValue(jobInfo())
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

// —— 列表：加载与本地搜索/筛选/排序 ——

describe('文档列表：加载与本地搜索/筛选/排序', () => {
  it('加载当前企业全部文档（后端无分页，一次拉全量）', async () => {
    // 保护行为：列表接口不带任何查询参数（后端无分页/搜索）
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([documentItem()])
    const store = useKnowledgeStore()
    seedOrganization()

    await store.loadDocuments()

    expect(knowledgeApi.listKnowledgeDocuments).toHaveBeenCalledTimes(1)
    expect(knowledgeApi.listKnowledgeDocuments).toHaveBeenCalledWith()
    expect(store.documents).toHaveLength(1)
    expect(store.documentsLoading).toBe(false)
    expect(store.documentsError).toBeNull()
  })

  it('本地搜索作用于 title 与 document_id（不发起网络请求）', async () => {
    // 保护行为：搜索是纯前端过滤，不得向后端发送不存在的查询参数
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([
      documentItem({ document_id: 'doc-1', title: '售后政策' }),
      documentItem({ document_id: 'doc-2', title: '退货流程' }),
    ])
    const store = useKnowledgeStore()
    seedOrganization()
    await store.loadDocuments()

    store.setSearchText('售后')
    expect(store.filteredDocuments.map((item) => item.document_id)).toEqual(['doc-1'])
    // 按 document_id 搜索
    store.setSearchText('doc-2')
    expect(store.filteredDocuments.map((item) => item.document_id)).toEqual(['doc-2'])
    expect(knowledgeApi.listKnowledgeDocuments).toHaveBeenCalledTimes(1)

    store.setSearchText('不存在的词')
    expect(store.filteredDocuments).toHaveLength(0)
  })

  it('状态筛选与来源类型筛选均为本地处理', async () => {
    // 保护行为：筛选作用于已加载列表，不发送参数
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([
      documentItem({ document_id: 'doc-1', status: 'active', source_type: 'markdown' }),
      documentItem({ document_id: 'doc-2', status: 'disabled', source_type: 'text' }),
      documentItem({ document_id: 'doc-3', status: 'active', source_type: 'text' }),
    ])
    const store = useKnowledgeStore()
    seedOrganization()
    await store.loadDocuments()

    store.setStatusFilter('disabled')
    expect(store.filteredDocuments.map((item) => item.document_id)).toEqual(['doc-2'])

    store.setStatusFilter('all')
    store.setSourceTypeFilter('text')
    expect(store.filteredDocuments.map((item) => item.document_id)).toEqual(['doc-2', 'doc-3'])

    store.setSourceTypeFilter('markdown')
    expect(store.filteredDocuments.map((item) => item.document_id)).toEqual(['doc-1'])
  })

  it('本地排序：默认最近更新，支持最早创建/最近创建/标题', async () => {
    // 保护行为：排序是纯前端处理（后端只保证创建时间升序）
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([
      documentItem({ document_id: 'b', title: 'B 文档', created_at: '2026-01-02T00:00:00+00:00', updated_at: '2026-03-02T00:00:00+00:00' }),
      documentItem({ document_id: 'a', title: 'A 文档', created_at: '2026-01-01T00:00:00+00:00', updated_at: '2026-03-01T00:00:00+00:00' }),
      documentItem({ document_id: 'c', title: 'C 文档', created_at: '2026-01-03T00:00:00+00:00', updated_at: '2026-02-01T00:00:00+00:00' }),
    ])
    const store = useKnowledgeStore()
    seedOrganization()
    await store.loadDocuments()

    // 默认「最近更新」：b(03-02) > a(03-01) > c(02-01)
    expect(store.filteredDocuments.map((item) => item.document_id)).toEqual(['b', 'a', 'c'])

    store.setSortBy('created-asc')
    expect(store.filteredDocuments.map((item) => item.document_id)).toEqual(['a', 'b', 'c'])

    store.setSortBy('created-desc')
    expect(store.filteredDocuments.map((item) => item.document_id)).toEqual(['c', 'b', 'a'])

    store.setSortBy('title-asc')
    expect(store.filteredDocuments.map((item) => item.document_id)).toEqual(['a', 'b', 'c'])
  })

  it('列表加载失败记录错误并可重试恢复', async () => {
    // 保护行为：列表 error/retry 状态完整
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockRejectedValueOnce(apiError(500))
    const store = useKnowledgeStore()
    seedOrganization()

    await store.loadDocuments()
    expect(store.documentsError).toBeTruthy()
    expect(store.documentsLoading).toBe(false)

    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValueOnce([documentItem()])
    await store.refreshDocuments()
    expect(store.documentsError).toBeNull()
    expect(store.documents).toHaveLength(1)
  })
})

// —— 详情 ——

describe('文档详情', () => {
  it('加载详情并同步 latest_job 为当前任务', async () => {
    // 保护行为：详情自带有效版本最近任务，可直接恢复任务状态（无任务列表接口）
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(
      documentDetail({ latest_job: jobInfo({ status: 'failed', error_code: 'INGESTION_FAILED' }) }),
    )
    const store = useKnowledgeStore()
    seedOrganization()

    const result = await store.loadDocumentDetail('doc-1')

    expect(result).toBe('ok')
    expect(store.documentDetail?.document_id).toBe('doc-1')
    expect(store.ingestionJob?.status).toBe('failed')
    expect(knowledgeApi.getKnowledgeDocumentDetail).toHaveBeenCalledWith('doc-1')
  })

  it('详情 404：清空详情并提示「文档不存在或已不可访问」', async () => {
    // 保护行为：404 是「不存在或不属于当前企业」的统一语义，提示后回列表
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockRejectedValueOnce(
      apiError(404, 'DOCUMENT_NOT_FOUND'),
    )
    const store = useKnowledgeStore()
    seedOrganization()
    store.documentDetail = documentDetail()

    const result = await store.loadDocumentDetail('doc-missing')

    expect(result).toBe('not-found')
    expect(store.documentDetail).toBeNull()
    expect(store.notice).toBe('文档不存在或已不可访问')
  })
})

// —— 上传新文档：回执分流 ——

describe('上传新文档：回执分流', () => {
  beforeEach(() => {
    seedOrganization()
    mockDefaultApis()
  })

  it('succeeded：提示成功、刷新列表并加载新文档详情', async () => {
    // 保护行为：成功回执必须刷新列表与详情（新文档上传后进入详情）
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockResolvedValue(
      receipt({ status: 'succeeded', deduplicated: false }),
    )
    const store = useKnowledgeStore()

    const outcome = await store.uploadNewDocument({ title: '新文档', file: new File(['内容'], 'a.md') })

    expect(outcome.result).toBe('succeeded')
    expect(store.notice).toBe('文档上传并入库成功。')
    expect(knowledgeApi.listKnowledgeDocuments).toHaveBeenCalled()
    expect(knowledgeApi.getKnowledgeDocumentDetail).toHaveBeenCalledWith('doc-1')
    expect(store.uploadReceipt).toEqual(
      expect.objectContaining({ status: 'succeeded', deduplicated: false }),
    )
  })

  it('deduplicated=true：明确提示未创建重复版本，不虚构新版本', async () => {
    // 保护行为：内容与当前有效版本相同 → 提示且不得产生任何新版本
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockResolvedValue(
      receipt({ status: 'succeeded', deduplicated: true }),
    )
    const store = useKnowledgeStore()

    const outcome = await store.uploadNewDocument({ title: '新文档', file: new File(['内容'], 'a.md') })

    expect(outcome.result).toBe('deduplicated')
    expect(store.notice).toBe('上传内容与当前有效版本相同，未创建重复版本。')
  })

  it('queued：保存 job_id、查询任务并启动轮询', async () => {
    // 保护行为：异步回执不得描述为已经成功，必须启动受控轮询
    vi.useFakeTimers()
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockResolvedValue(
      receipt({ status: 'queued', deduplicated: false }),
    )
    vi.mocked(knowledgeApi.getIngestionJob).mockResolvedValue(jobInfo({ status: 'queued' }))
    const store = useKnowledgeStore()

    const outcome = await store.uploadNewDocument({ title: '新文档', file: new File(['内容'], 'a.md') })
    await vi.advanceTimersByTimeAsync(0)

    expect(outcome.result).toBe('pending')
    expect(store.notice).toContain('后台入库')
    expect(knowledgeApi.getIngestionJob).toHaveBeenCalledWith('job-1')

    // 5 秒后触发轮询查询一次
    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS)
    expect(knowledgeApi.getIngestionJob).toHaveBeenCalledTimes(2)
    store.stopJobPolling()
  })

  it('failed：展示安全失败状态并查询任务详情，不描述为可检索', async () => {
    // 保护行为：failed 回执必须保留失败事实并查询任务详情（供错误码展示）
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockResolvedValue(
      receipt({ status: 'failed', deduplicated: false }),
    )
    vi.mocked(knowledgeApi.getIngestionJob).mockResolvedValue(
      jobInfo({ status: 'failed', error_code: 'EMBEDDING_UNAVAILABLE', error_message: 'embedding service unavailable' }),
    )
    const store = useKnowledgeStore()

    const outcome = await store.uploadNewDocument({ title: '新文档', file: new File(['内容'], 'a.md') })
    await Promise.resolve()

    expect(outcome.result).toBe('failed')
    expect(knowledgeApi.getIngestionJob).toHaveBeenCalledWith('job-1')
    expect(store.ingestionJob?.status).toBe('failed')
    expect(store.ingestionJob?.error_code).toBe('EMBEDDING_UNAVAILABLE')
  })

  it('上传期间再次调用被拦截（防重复提交）', async () => {
    // 保护行为：提交期间不得重复发起上传请求（可能产生重复文档/版本）
    let resolveUpload!: (value: IngestionReceipt) => void
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve
        }),
    )
    const store = useKnowledgeStore()

    const first = store.uploadNewDocument({ title: '新文档', file: new File(['内容'], 'a.md') })
    const second = await store.uploadNewDocument({ title: '新文档', file: new File(['内容'], 'a.md') })

    expect(second.result).toBe('error')
    expect(knowledgeApi.uploadKnowledgeDocument).toHaveBeenCalledTimes(1)
    resolveUpload(receipt())
    await first
  })
})

// —— 上传错误分类 ——

describe('上传错误分类', () => {
  beforeEach(() => {
    seedOrganization()
    mockDefaultApis()
  })

  it('403：刷新企业列表与角色并提示权限变化', async () => {
    // 保护行为：403 说明 admin 权限已经变化，必须刷新企业角色
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockRejectedValueOnce(
      apiError(403, null, 'admin role required'),
    )
    vi.mocked(organizationApi.listOrganizations).mockResolvedValue([])
    const store = useKnowledgeStore()

    const outcome = await store.uploadNewDocument({ title: '新文档', file: new File(['内容'], 'a.md') })

    expect(outcome.result).toBe('forbidden')
    expect(organizationApi.listOrganizations).toHaveBeenCalled()
    expect(store.notice).toContain('权限已经变化')
  })

  it('409：刷新最新状态并提示状态变化', async () => {
    // 保护行为：409 状态冲突必须重读服务端最新状态
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockRejectedValueOnce(
      apiError(409, 'DOCUMENT_DISABLED'),
    )
    const store = useKnowledgeStore()

    const outcome = await store.uploadNewDocument({ title: '新文档', file: new File(['内容'], 'a.md') })

    expect(outcome.result).toBe('conflict')
    expect(knowledgeApi.listKnowledgeDocuments).toHaveBeenCalled()
  })

  it('422：返回错误结果且不产生回执（表单由调用方保留）', async () => {
    // 保护行为：422 输入错误应原样返回，由表单映射字段并保留用户输入
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockRejectedValueOnce(
      apiError(422, 'INVALID_DOCUMENT', 'invalid document: title must be 1-200'),
    )
    const store = useKnowledgeStore()

    const outcome = await store.uploadNewDocument({ title: '新文档', file: new File(['内容'], 'a.md') })

    expect(outcome.result).toBe('error')
    expect(outcome.receipt).toBeNull()
    expect(store.uploadReceipt).toBeNull()
  })

  it('超时/断网（status 0）：提示结果状态不确定，且不自动重试', async () => {
    // 保护行为：网络中断后服务端可能已完成入库，必须提示不确定并禁止自动重试
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockRejectedValueOnce(
      apiError(0, null, 'timeout'),
    )
    const store = useKnowledgeStore()

    const outcome = await store.uploadNewDocument({ title: '新文档', file: new File(['内容'], 'a.md') })

    expect(outcome.result).toBe('timeout-uncertain')
    expect(store.notice).toBe('结果状态可能不确定，请先刷新文档列表，再决定是否重新上传。')
    expect(knowledgeApi.uploadKnowledgeDocument).toHaveBeenCalledTimes(1)
  })

  it('503：不描述为「没有知识」，提示基础设施失败可稍后重试', async () => {
    // 保护行为：503 入库基础设施失败不能显示成空知识库
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockRejectedValueOnce(
      apiError(503, 'VECTOR_STORE_UNAVAILABLE', 'vector store unavailable'),
    )
    const store = useKnowledgeStore()

    const outcome = await store.uploadNewDocument({ title: '新文档', file: new File(['内容'], 'a.md') })

    expect(outcome.result).toBe('error')
    expect(store.notice).toContain('入库服务暂时不可用')
  })
})

// —— 新版本上传 ——

describe('新版本上传', () => {
  beforeEach(() => {
    seedOrganization()
    mockDefaultApis()
  })

  it('成功后刷新详情与列表；deduplicated 时提示且不产生新版本', async () => {
    // 保护行为：新版本成功必须刷新详情（新版本成为 active）与列表
    vi.mocked(knowledgeApi.uploadKnowledgeDocumentVersion).mockResolvedValue(
      receipt({ status: 'succeeded', deduplicated: false }),
    )
    const store = useKnowledgeStore()

    await store.uploadNewVersion('doc-1', { title: '售后政策', file: new File(['新内容'], 'b.md') })

    expect(knowledgeApi.uploadKnowledgeDocumentVersion).toHaveBeenCalledWith('doc-1', {
      title: '售后政策',
      file: expect.any(File),
    })
    expect(knowledgeApi.getKnowledgeDocumentDetail).toHaveBeenCalledWith('doc-1')
    expect(knowledgeApi.listKnowledgeDocuments).toHaveBeenCalled()

    vi.mocked(knowledgeApi.uploadKnowledgeDocumentVersion).mockResolvedValue(
      receipt({ status: 'succeeded', deduplicated: true }),
    )
    const outcome = await store.uploadNewVersion('doc-1', { title: '售后政策', file: new File(['新内容'], 'b.md') })
    expect(outcome.result).toBe('deduplicated')
    expect(store.notice).toBe('上传内容与当前有效版本相同，未创建重复版本。')
  })

  it('超时/断网时先刷新详情（latest_job 恢复任务）再提示不确定', async () => {
    // 保护行为：结果不确定时必须重读详情，详情中的 latest_job 是任务状态唯一可信来源
    vi.mocked(knowledgeApi.uploadKnowledgeDocumentVersion).mockRejectedValueOnce(
      apiError(0, null, 'timeout'),
    )
    const store = useKnowledgeStore()

    const outcome = await store.uploadNewVersion('doc-1', { title: '售后政策', file: new File(['新内容'], 'b.md') })

    expect(outcome.result).toBe('timeout-uncertain')
    expect(knowledgeApi.getKnowledgeDocumentDetail).toHaveBeenCalledWith('doc-1')
  })
})

// —— 停用 / 启用 ——

describe('停用与启用', () => {
  beforeEach(() => {
    seedOrganization()
    mockDefaultApis()
  })

  it('停用成功：提示并刷新列表与详情', async () => {
    // 保护行为：停用后文档不再参与检索，界面必须同步最新状态
    vi.mocked(knowledgeApi.disableKnowledgeDocument).mockResolvedValue(
      documentItem({ status: 'disabled' }),
    )
    const store = useKnowledgeStore()
    store.currentDocumentId = 'doc-1'

    const result = await store.disableDocument('doc-1')

    expect(result).toBe('ok')
    expect(store.notice).toContain('已停用')
    expect(knowledgeApi.disableKnowledgeDocument).toHaveBeenCalledWith('doc-1')
    expect(knowledgeApi.listKnowledgeDocuments).toHaveBeenCalled()
    expect(knowledgeApi.getKnowledgeDocumentDetail).toHaveBeenCalledWith('doc-1')
  })

  it('启用成功：提示并刷新列表与详情', async () => {
    // 保护行为：启用后当前有效版本重新参与检索
    vi.mocked(knowledgeApi.enableKnowledgeDocument).mockResolvedValue(
      documentItem({ status: 'active' }),
    )
    const store = useKnowledgeStore()

    const result = await store.enableDocument('doc-1')

    expect(result).toBe('ok')
    expect(store.notice).toContain('已启用')
    expect(knowledgeApi.enableKnowledgeDocument).toHaveBeenCalledWith('doc-1')
  })

  it('同一文档操作进行中时再次调用被拦截（防重复提交）', async () => {
    // 边界情况：状态变更期间不得重复发送请求
    let resolveDisable!: (value: KnowledgeDocumentSummary) => void
    vi.mocked(knowledgeApi.disableKnowledgeDocument).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveDisable = resolve
        }),
    )
    const store = useKnowledgeStore()

    const first = store.disableDocument('doc-1')
    const second = await store.disableDocument('doc-1')

    expect(second).toBe('ignored')
    expect(knowledgeApi.disableKnowledgeDocument).toHaveBeenCalledTimes(1)
    resolveDisable(documentItem({ status: 'disabled' }))
    await first
  })

  it('503：保留服务端事实并提示稍后重试（不自动重试）', async () => {
    // 保护行为：503 状态下文档事实未变，提示稍后再试且不重复请求
    vi.mocked(knowledgeApi.disableKnowledgeDocument).mockRejectedValueOnce(
      apiError(503, 'VECTOR_STORE_UNAVAILABLE'),
    )
    const store = useKnowledgeStore()

    const result = await store.disableDocument('doc-1')

    expect(result).toBe('service-unavailable')
    expect(store.notice).toContain('稍后重试')
    expect(knowledgeApi.disableKnowledgeDocument).toHaveBeenCalledTimes(1)
  })

  it('404：提示不可访问并刷新列表', async () => {
    // 保护行为：文档已被删除/失去权限时提示并回列表
    vi.mocked(knowledgeApi.enableKnowledgeDocument).mockRejectedValueOnce(
      apiError(404, 'DOCUMENT_NOT_FOUND'),
    )
    const store = useKnowledgeStore()

    const result = await store.enableDocument('doc-missing')

    expect(result).toBe('not-found')
    expect(store.notice).toBe('文档不存在或已不可访问')
    expect(knowledgeApi.listKnowledgeDocuments).toHaveBeenCalled()
  })
})

// —— 入库任务轮询 ——

describe('入库任务轮询', () => {
  beforeEach(() => {
    seedOrganization()
    mockDefaultApis()
  })

  it('只有 queued/running 轮询；succeeded/failed 终态停止', async () => {
    // 保护行为：终态任务不得继续轮询（succeeded/failed 后停止定时器）
    vi.useFakeTimers()
    vi.mocked(knowledgeApi.getIngestionJob)
      .mockResolvedValueOnce(jobInfo({ status: 'running' }))
      .mockResolvedValueOnce(jobInfo({ status: 'succeeded' }))
    const store = useKnowledgeStore()
    store.ingestionJob = jobInfo({ status: 'queued' })
    store.startJobPolling('job-1')

    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS)
    expect(knowledgeApi.getIngestionJob).toHaveBeenCalledTimes(1)
    expect(store.ingestionJob?.status).toBe('running')

    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS)
    expect(store.ingestionJob?.status).toBe('succeeded')
    expect(store.notice).toContain('成功完成')

    // 终态后不再轮询：过多个周期不发起新查询
    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS * 3)
    expect(knowledgeApi.getIngestionJob).toHaveBeenCalledTimes(2)
  })

  it('failed 终态停止并给出失败提示', async () => {
    // 保护行为：failed 任务必须停止轮询并提示查看任务详情
    vi.useFakeTimers()
    vi.mocked(knowledgeApi.getIngestionJob)
      .mockResolvedValueOnce(jobInfo({ status: 'running' }))
      .mockResolvedValueOnce(jobInfo({ status: 'failed', error_code: 'INGESTION_FAILED' }))
    const store = useKnowledgeStore()
    store.ingestionJob = jobInfo({ status: 'running' })
    store.startJobPolling('job-1')

    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS * 2)

    expect(store.ingestionJob?.status).toBe('failed')
    expect(store.notice).toContain('失败')
    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS * 3)
    expect(knowledgeApi.getIngestionJob).toHaveBeenCalledTimes(2)
  })

  it('页面隐藏时暂停轮询，恢复可见后继续', async () => {
    // 保护行为：页面不可见时不得发起轮询请求（节省资源且避免过期写入）
    vi.useFakeTimers()
    vi.mocked(knowledgeApi.getIngestionJob).mockResolvedValue(jobInfo({ status: 'running' }))
    const store = useKnowledgeStore()
    store.ingestionJob = jobInfo({ status: 'running' })
    store.startJobPolling('job-1')

    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS * 2)
    expect(knowledgeApi.getIngestionJob).not.toHaveBeenCalled()

    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS)
    expect(knowledgeApi.getIngestionJob).toHaveBeenCalledTimes(1)
  })

  it('stopJobPolling 后不再轮询（页面卸载/企业切换路径）', async () => {
    // 保护行为：卸载或切换企业必须停止定时器，不允许后台继续请求
    vi.useFakeTimers()
    vi.mocked(knowledgeApi.getIngestionJob).mockResolvedValue(jobInfo({ status: 'running' }))
    const store = useKnowledgeStore()
    store.ingestionJob = jobInfo({ status: 'running' })
    store.startJobPolling('job-1')

    store.stopJobPolling()
    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS * 2)
    expect(knowledgeApi.getIngestionJob).not.toHaveBeenCalled()
  })

  it('轮询期间请求不重叠（上一个未完成时不发起新查询）', async () => {
    // 保护行为：轮询请求禁止重叠，避免任务状态乱序写入
    vi.useFakeTimers()
    let resolveJob!: (value: IngestionJobInfo) => void
    vi.mocked(knowledgeApi.getIngestionJob).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveJob = resolve
        }),
    )
    const store = useKnowledgeStore()
    store.ingestionJob = jobInfo({ status: 'running' })
    store.startJobPolling('job-1')

    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS)
    expect(knowledgeApi.getIngestionJob).toHaveBeenCalledTimes(1)

    // 请求未返回：后续两个周期必须跳过
    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS * 2)
    expect(knowledgeApi.getIngestionJob).toHaveBeenCalledTimes(1)

    resolveJob(jobInfo({ status: 'succeeded' }))
    await vi.advanceTimersByTimeAsync(0)
    expect(store.ingestionJob?.status).toBe('succeeded')
  })

  it('每次任务更新后同步刷新文档详情', async () => {
    // 保护行为：轮询期间任务变化必须同步刷新详情（详情 latest_job 是任务状态来源）
    vi.useFakeTimers()
    vi.mocked(knowledgeApi.getIngestionJob).mockResolvedValue(jobInfo({ status: 'running' }))
    const store = useKnowledgeStore()
    store.currentDocumentId = 'doc-1'
    store.ingestionJob = jobInfo({ status: 'running' })
    store.startJobPolling('job-1')

    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS)

    expect(knowledgeApi.getKnowledgeDocumentDetail).toHaveBeenCalledWith('doc-1')
  })
})

// —— 企业切换与迟到响应 ——

describe('企业切换与迟到响应隔离', () => {
  it('切换企业时清理全部知识库状态并停止轮询', async () => {
    // 保护行为：企业切换必须清空文档/详情/上传结果/任务并停止轮询
    vi.useFakeTimers()
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([documentItem()])
    vi.mocked(knowledgeApi.getIngestionJob).mockResolvedValue(jobInfo({ status: 'running' }))
    const store = useKnowledgeStore()
    seedOrganization('org-1', 'admin', [{ organization_id: 'org-2', name: '企业二', role: 'admin' }])
    await store.loadDocuments()
    store.documentDetail = documentDetail()
    store.uploadReceipt = receipt()
    store.ingestionJob = jobInfo()
    store.startJobPolling('job-1')

    const organizationStore = useOrganizationStore()
    await organizationStore.select('org-2')

    expect(store.documents).toHaveLength(0)
    expect(store.documentDetail).toBeNull()
    expect(store.uploadReceipt).toBeNull()
    expect(store.ingestionJob).toBeNull()
    expect(store.organizationId).toBeNull()
    expect(store.searchText).toBe('')
    // 轮询已停止
    await vi.advanceTimersByTimeAsync(KNOWLEDGE_JOB_POLL_MS * 2)
    expect(knowledgeApi.getIngestionJob).not.toHaveBeenCalled()
  })

  it('租户重置回调注册有效：runTenantResetHandlers 清空知识库状态', async () => {
    // 保护行为：knowledge store 必须注册到 tenantReset 机制（退出登录也走该入口）
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([documentItem()])
    const store = useKnowledgeStore()
    seedOrganization()
    await store.loadDocuments()
    expect(store.documents).toHaveLength(1)

    await runTenantResetHandlers()

    expect(store.documents).toHaveLength(0)
  })

  it('旧企业迟到响应不写入新企业', async () => {
    // 保护行为：企业切换后旧企业的列表响应返回必须被丢弃（epoch 校验）
    let resolveList!: (value: KnowledgeDocumentSummary[]) => void
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveList = resolve
        }),
    )
    const store = useKnowledgeStore()
    seedOrganization('org-1', 'admin', [{ organization_id: 'org-2', name: '企业二', role: 'admin' }])
    const request = store.loadDocuments()

    const organizationStore = useOrganizationStore()
    await organizationStore.select('org-2')

    resolveList([documentItem({ title: '旧企业文档' })])
    await request

    expect(store.documents).toHaveLength(0)
    expect(store.documentsError).toBeNull()
  })

  it('切换文档后旧文档详情迟到响应被丢弃', async () => {
    // 边界情况：详情请求目标切换后，旧响应不得覆盖新目标（currentDocumentId 校验）
    let resolveOld!: (value: KnowledgeDocumentDetail) => void
    let resolveNew!: (value: KnowledgeDocumentDetail) => void
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveOld = resolve
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveNew = resolve
          }),
      )
    const store = useKnowledgeStore()
    seedOrganization()

    const requestOld = store.loadDocumentDetail('doc-old')
    const requestNew = store.loadDocumentDetail('doc-new')

    resolveOld(documentDetail({ document_id: 'doc-old' }))
    await requestOld
    // 旧响应被丢弃：不写入旧文档数据（新目标尚未返回，详情保持空）
    expect(store.documentDetail).toBeNull()
    expect(store.currentDocumentId).toBe('doc-new')

    resolveNew(documentDetail({ document_id: 'doc-new' }))
    await requestNew
    expect(store.documentDetail?.document_id).toBe('doc-new')
  })
})
