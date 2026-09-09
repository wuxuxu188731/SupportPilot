/*
 * 知识库 API 封装测试：路径、上传 FormData 字段（只含 title+file）、
 * 不自设 multipart Content-Type、独立长超时与不自动重试、停用/启用、
 * 入库任务查询。通过 mock httpClient 进行，不触达网络。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

// mock 统一 HTTP 客户端：验证调用形态
vi.mock('@/api/http', () => ({
  httpClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import * as knowledgeApi from '@/api/knowledge'
import { httpClient } from '@/api/http'
import type { IngestionReceipt } from '@/api/knowledgeTypes'

beforeEach(() => {
  vi.clearAllMocks()
})

/** 构造一个测试用的 File（jsdom 支持 Blob/File）。 */
function createFile(name: string, content = '知识内容'): File {
  return new File([content], name, { type: 'text/plain' })
}

/** 断言 FormData 字段恰好为 title + file（无 organization_id/source_type 等）。 */
function expectFormOnlyTitleAndFile(form: unknown): void {
  expect(form).toBeInstanceOf(FormData)
  const fd = form as FormData
  const keys = [...fd.keys()]
  expect([...new Set(keys)].sort()).toEqual(['file', 'title'])
  expect(keys).toHaveLength(2)
  // file 字段必须是 Blob/File 实例（浏览器 FormData 附加方式）
  const fileEntry = fd.get('file')
  expect(fileEntry).toBeInstanceOf(Blob)
  expect(fd.get('title')).toBe('测试文档')
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

describe('知识库列表与详情 API', () => {
  it('GET /knowledge/documents/ 拉取当前企业全部文档', async () => {
    // 保护行为：列表接口不带任何查询参数（后端无分页/搜索）
    vi.mocked(httpClient.get).mockResolvedValue({ data: [{ document_id: 'doc-1' }] })

    const result = await knowledgeApi.listKnowledgeDocuments()

    expect(result).toHaveLength(1)
    expect(httpClient.get).toHaveBeenCalledWith('/knowledge/documents/')
  })

  it('GET /knowledge/documents/{id}/ 按文档 id 请求详情', async () => {
    // 保护行为：详情路径必须包含文档 id，供后端做归属校验
    vi.mocked(httpClient.get).mockResolvedValue({
      data: { document_id: 'doc-1', versions: [], latest_job: null },
    })

    const result = await knowledgeApi.getKnowledgeDocumentDetail('doc-1')

    expect(result.document_id).toBe('doc-1')
    expect(httpClient.get).toHaveBeenCalledWith('/knowledge/documents/doc-1/')
  })

  it('文档详情 id 中的特殊字符会被 URL 编码', async () => {
    // 边界情况：文档 id 可能含特殊字符，路径必须编码避免破坏路由
    vi.mocked(httpClient.get).mockResolvedValue({ data: null })

    await knowledgeApi.getKnowledgeDocumentDetail('doc/1')

    expect(httpClient.get).toHaveBeenCalledWith('/knowledge/documents/doc%2F1/')
  })
})

describe('上传新文档 API（FormData 契约）', () => {
  it('POST /knowledge/documents/ 的 FormData 只有 title 与 file', async () => {
    // 保护行为：上传表单必须恰好包含 title 与 file；
    // 不得携带 organization_id / source_type / 任何其它字段（后端 422）
    vi.mocked(httpClient.post).mockResolvedValue({ data: receipt() })

    await knowledgeApi.uploadKnowledgeDocument({ title: '测试文档', file: createFile('a.md') })

    expect(httpClient.post).toHaveBeenCalledTimes(1)
    const [url, form, config] = vi.mocked(httpClient.post).mock.calls[0]
    expect(url).toBe('/knowledge/documents/')
    expectFormOnlyTitleAndFile(form)
    // 配置中只允许 timeout；不得手动设置 multipart Content-Type（由浏览器生成）
    expect(config).toEqual({ timeout: 180_000 })
    expect((config as { headers?: unknown }).headers).toBeUndefined()
  })

  it('VITE_KNOWLEDGE_UPLOAD_TIMEOUT_MS 可覆盖上传超时', () => {
    // 保护行为：部署环境可通过 VITE_KNOWLEDGE_UPLOAD_TIMEOUT_MS 覆盖默认 180 秒
    vi.stubEnv('VITE_KNOWLEDGE_UPLOAD_TIMEOUT_MS', '60000')
    try {
      expect(knowledgeApi.getKnowledgeUploadTimeoutMs()).toBe(60_000)
    } finally {
      vi.unstubAllEnvs()
    }
  })

  it('非法 VITE_KNOWLEDGE_UPLOAD_TIMEOUT_MS 回退默认长超时', () => {
    // 边界情况：环境变量非数字或非正数时回退默认 180 秒
    vi.stubEnv('VITE_KNOWLEDGE_UPLOAD_TIMEOUT_MS', 'not-a-number')
    try {
      expect(knowledgeApi.getKnowledgeUploadTimeoutMs()).toBe(180_000)
    } finally {
      vi.unstubAllEnvs()
    }
  })

  it('上传请求失败后不会自动重试（只调用一次）', async () => {
    // 保护行为：上传 POST 绝不自动重试——服务端可能已完成入库与版本激活，
    // 自动重试会造成重复文档或重复版本；失败由用户刷新列表后自行决定
    vi.mocked(httpClient.post).mockRejectedValueOnce(new Error('network down'))

    await expect(
      knowledgeApi.uploadKnowledgeDocument({ title: '测试文档', file: createFile('a.md') }),
    ).rejects.toThrow('network down')
    expect(httpClient.post).toHaveBeenCalledTimes(1)
  })

  it('上传成功返回完整回执（status/deduplicated 供分流）', async () => {
    // 保护行为：回执字段必须完整透传，调用方按 status 与 deduplicated 分流
    vi.mocked(httpClient.post).mockResolvedValue({
      data: receipt({ status: 'queued', deduplicated: false }),
    })

    const result = await knowledgeApi.uploadKnowledgeDocument({
      title: '测试文档',
      file: createFile('a.md'),
    })

    expect(result.status).toBe('queued')
    expect(result.deduplicated).toBe(false)
  })
})

describe('上传新版本 API（FormData 契约）', () => {
  it('POST /knowledge/documents/{id}/versions/ 的 FormData 只有 title 与 file', async () => {
    // 保护行为：新版本上传同样必须只含 title+file 两字段，禁止附加其它字段
    vi.mocked(httpClient.post).mockResolvedValue({ data: receipt() })

    await knowledgeApi.uploadKnowledgeDocumentVersion('doc-1', {
      title: '原文档标题',
      file: createFile('b.markdown'),
    })

    expect(httpClient.post).toHaveBeenCalledTimes(1)
    const [url, form, config] = vi.mocked(httpClient.post).mock.calls[0]
    expect(url).toBe('/knowledge/documents/doc-1/versions/')
    expect(form).toBeInstanceOf(FormData)
    const fd = form as FormData
    expect([...new Set([...fd.keys()])].sort()).toEqual(['file', 'title'])
    expect(fd.get('title')).toBe('原文档标题')
    expect(config).toEqual({ timeout: 180_000 })
  })

  it('新版本上传失败后不会自动重试（只调用一次）', async () => {
    // 保护行为：新版本上传同样不自动重试，避免产生重复版本
    vi.mocked(httpClient.post).mockRejectedValueOnce(new Error('timeout'))

    await expect(
      knowledgeApi.uploadKnowledgeDocumentVersion('doc-1', {
        title: '原文档标题',
        file: createFile('b.md'),
      }),
    ).rejects.toThrow('timeout')
    expect(httpClient.post).toHaveBeenCalledTimes(1)
  })
})

describe('停用 / 启用 / 入库任务查询 API', () => {
  it('POST /knowledge/documents/{id}/disable/ 停用文档', async () => {
    // 保护行为：停用必须是 POST 且路径含文档 id
    vi.mocked(httpClient.post).mockResolvedValue({ data: { document_id: 'doc-1' } })

    const result = await knowledgeApi.disableKnowledgeDocument('doc-1')

    expect(result.document_id).toBe('doc-1')
    expect(httpClient.post).toHaveBeenCalledWith('/knowledge/documents/doc-1/disable/')
  })

  it('POST /knowledge/documents/{id}/enable/ 启用文档', async () => {
    // 保护行为：启用必须是 POST 且路径含文档 id
    vi.mocked(httpClient.post).mockResolvedValue({ data: { document_id: 'doc-1' } })

    const result = await knowledgeApi.enableKnowledgeDocument('doc-1')

    expect(result.document_id).toBe('doc-1')
    expect(httpClient.post).toHaveBeenCalledWith('/knowledge/documents/doc-1/enable/')
  })

  it('GET /knowledge/ingestion-jobs/{id}/ 查询入库任务', async () => {
    // 保护行为：任务查询路径必须包含 job id，供后端做归属校验
    vi.mocked(httpClient.get).mockResolvedValue({
      data: { job_id: 'job-1', status: 'running', attempt_count: 1 },
    })

    const result = await knowledgeApi.getIngestionJob('job-1')

    expect(result.job_id).toBe('job-1')
    expect(httpClient.get).toHaveBeenCalledWith('/knowledge/ingestion-jobs/job-1/')
  })
})
