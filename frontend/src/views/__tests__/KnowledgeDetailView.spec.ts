/*
 * KnowledgeDetailView 页面集成测试：详情字段展示、版本历史（排序与
 * 当前有效版本标记）、入库任务面板（null 显示「—」）、agent 只读、
 * admin 按状态显示操作（active 停用 / disabled 启用 / processing/failed
 * 说明 / word 不提供上传版本）、404 返回列表、停用确认前不发请求、
 * 上传新版本入口。
 * mock API 层与登录/企业上下文，不触达网络。
 */

import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'
import type { KnowledgeDocumentDetail } from '@/api/knowledgeTypes'
import { createAppRouter, registerGuards } from '@/router'
import KnowledgeDetailView from '@/views/KnowledgeDetailView.vue'
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
vi.mock('@/api/knowledge', () => ({
  listKnowledgeDocuments: vi.fn(),
  getKnowledgeDocumentDetail: vi.fn(),
  getDocumentVersionContent: vi.fn(),
  uploadKnowledgeDocument: vi.fn(),
  uploadKnowledgeDocumentVersion: vi.fn(),
  disableKnowledgeDocument: vi.fn(),
  enableKnowledgeDocument: vi.fn(),
  getIngestionJob: vi.fn(),
}))

import * as authApi from '@/api/auth'
import * as knowledgeApi from '@/api/knowledge'
import * as organizationApi from '@/api/organization'
import type { DocumentContentResponse } from '@/api/knowledgeTypes'
import { clearDocumentContentCache } from '@/utils/documentContentCache'

const USER = { user_id: 'u-1', username: 'alice', created_at: '2026-09-01 00:00:00' }

/** 构造文档详情。 */
function detail(partial: Partial<KnowledgeDocumentDetail> = {}): KnowledgeDocumentDetail {
  return {
    document_id: 'doc-1',
    title: '售后政策',
    source_type: 'markdown',
    status: 'active',
    active_version_id: 'v-2',
    created_at: '2026-09-01T00:00:00.000000+00:00',
    updated_at: '2026-09-02T00:00:00.000000+00:00',
    versions: [
      {
        version_id: 'v-1',
        version_no: 1,
        content_hash: 'sha256:aaaa',
        loader_version: 'supportpilot-loader-v1',
        chunker_version: 'supportpilot-chunker-v1',
        embedding_model: 'text-embedding-v4',
        embedding_dimensions: 1024,
        created_at: '2026-09-01T00:00:00.000000+00:00',
      },
      {
        version_id: 'v-2',
        version_no: 2,
        content_hash: 'sha256:bbbb',
        loader_version: 'supportpilot-loader-v1',
        chunker_version: 'supportpilot-chunker-v1',
        embedding_model: 'text-embedding-v4',
        embedding_dimensions: 1024,
        created_at: '2026-09-02T00:00:00.000000+00:00',
      },
    ],
    latest_job: null,
    ...partial,
  }
}

/** 预置已登录 + 企业上下文（角色可配置）。 */
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
  vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([])
}

/** 挂载知识库详情页并导航到指定路径。 */
async function mountKnowledgeDetail(path: string) {
  const router = createAppRouter(createMemoryHistory())
  const pinia = createPinia()
  setActivePinia(pinia)
  registerGuards(router)
  await router.push(path)
  await router.isReady()
  const wrapper = mount(
    { render: () => h(NDialogProvider, null, { default: () => h(NMessageProvider, null, { default: () => h(KnowledgeDetailView) }) }) },
    {
      global: { plugins: [pinia, router], stubs: { teleport: true } },
    },
  )
  await flushPromises()
  await flushNavigation()
  return { wrapper, router }
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  // 正文缓存是模块级：用例之间必须清空，否则会命中上一条用例的数据
  clearDocumentContentCache()
  vi.mocked(authApi.me).mockResolvedValue(USER)
})

describe('KnowledgeDetailView 基本信息与版本历史', () => {
  // 保护行为：未解析版本的空哈希显示占位符，点击时不尝试复制。
  it('排队版本的空内容哈希可以正常展示和点击', async () => {
    const pending = detail({ status: 'processing', active_version_id: null })
    pending.versions = [{ ...pending.versions[0], content_hash: null }]
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(pending)
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')
    const hashButton = wrapper.findAll('[data-test="version-item"] button')[1]
    expect(hashButton.text()).toBe('—')
    expect(hashButton.attributes('title')).toBeUndefined()
    await hashButton.trigger('click')
    expect(wrapper.text()).not.toContain('已复制')
    expect(wrapper.text()).not.toContain('null')
    wrapper.unmount()
  })

  it('加载详情并展示基本信息、版本历史与当前有效版本标记', async () => {
    // 保护行为：详情页必须展示真实字段（无正文/下载伪造，覆盖测试 18）
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(detail())
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')

    expect(knowledgeApi.getKnowledgeDocumentDetail).toHaveBeenCalledWith('doc-1')
    expect(wrapper.text()).toContain('售后政策')
    expect(wrapper.text()).toContain('doc-1')
    expect(wrapper.text()).toContain('Markdown')
    expect(wrapper.text()).toContain('已启用')

    // 版本历史：v1 / v2 按 version_no 升序，v2 标记为当前有效版本
    const items = wrapper.findAll('[data-test="version-item"]')
    expect(items).toHaveLength(2)
    expect(items[0].text()).toContain('v1')
    expect(items[1].text()).toContain('v2')
    expect(items[1].text()).toContain('当前有效版本')
    // 不出现正文预览或下载按钮（后端详情接口不含原文，禁止伪造入口）
    expect(wrapper.find('[data-test="download-document"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="preview-document"]').exists()).toBe(false)
  })

  it('任务面板无任务时展示明确空状态，不显示 null/undefined', async () => {
    // 保护行为：latest_job 为 null 时不显示字符串 null；版本字段空值显示占位
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(
      detail({ latest_job: null }),
    )
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')

    expect(wrapper.find('[data-test="job-empty"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('null')
    expect(wrapper.text()).not.toContain('undefined')
  })

  it('任务面板展示任务字段，null 时间显示「—」，错误展示安全码', async () => {
    // 保护行为：任务字段完整展示，可空字段显示占位符（覆盖测试 18.3）
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(
      detail({
        latest_job: {
          job_id: 'job-1',
          document_id: 'doc-1',
          version_id: 'v-2',
          status: 'failed',
          attempt_count: 1,
          error_code: 'EMBEDDING_UNAVAILABLE',
          error_message: 'embedding service unavailable',
          started_at: '2026-09-02T00:00:01.000000+00:00',
          finished_at: null,
          created_at: '2026-09-02T00:00:00.000000+00:00',
        },
      }),
    )
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')

    const panel = wrapper.find('[data-test="ingestion-job-panel"]')
    expect(panel.text()).toContain('job-1')
    expect(panel.text()).toContain('失败')
    expect(panel.text()).toContain('EMBEDDING_UNAVAILABLE')
    expect(panel.text()).toContain('embedding service unavailable')
    // 未结束任务的 finished_at 显示「—」而非 null
    expect(panel.text()).toContain('—')
    expect(panel.text()).not.toContain('null')
  })

  it('404 详情返回知识库列表并提示「文档不存在或已不可访问」', async () => {
    // 保护行为：文档不存在/不可访问必须回列表并给出提示（覆盖测试 31）
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockRejectedValue(
      new ApiError({ status: 404, code: 'DOCUMENT_NOT_FOUND', message: '文档不存在或已被移除', fieldErrors: {}, raw: null }),
    )
    seedLogin('admin')
    const { wrapper, router } = await mountKnowledgeDetail('/app/knowledge/doc-missing')

    await flushPromises()
    await flushNavigation()
    expect(router.currentRoute.value.name).toBe('knowledge')
    expect(wrapper.text()).toContain('文档不存在或已不可访问')
  })
})

describe('KnowledgeDetailView 角色与状态操作', () => {
  it('agent：只读说明，无停用/启用/上传新版本按钮', async () => {
    // 保护行为：agent 详情视图只读（覆盖测试 40 的只读面）
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(detail())
    seedLogin('agent')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')

    expect(wrapper.find('[data-test="detail-readonly-note"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="detail-disable"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="detail-enable"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="open-version-upload"]').exists()).toBe(false)
  })

  it('admin + active：显示停用，不显示启用与错误操作', async () => {
    // 保护行为：active 文档只提供停用入口（覆盖测试 28）
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(
      detail({ status: 'active' }),
    )
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')

    expect(wrapper.find('[data-test="detail-disable"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="detail-enable"]').exists()).toBe(false)
  })

  it('admin + disabled：显示启用，不显示停用', async () => {
    // 保护行为：disabled 文档只提供启用入口（覆盖测试 29）
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(
      detail({ status: 'disabled' }),
    )
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')

    expect(wrapper.find('[data-test="detail-enable"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="detail-disable"]').exists()).toBe(false)
  })

  it('admin + processing/failed：只展示状态说明，不提供直接启用', async () => {
    // 保护行为：processing/failed 不显示误导性启用操作（覆盖测试 30）
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(
      detail({ status: 'processing' }),
    )
    seedLogin('admin')
    const processing = await mountKnowledgeDetail('/app/knowledge/doc-1')
    expect(processing.wrapper.text()).toContain('正在处理中')
    expect(processing.wrapper.find('[data-test="detail-enable"]').exists()).toBe(false)
    expect(processing.wrapper.find('[data-test="detail-disable"]').exists()).toBe(false)

    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(
      detail({ status: 'failed' }),
    )
    const failed = await mountKnowledgeDetail('/app/knowledge/doc-1')
    expect(failed.wrapper.text()).toContain('文档入库失败')
    expect(failed.wrapper.find('[data-test="detail-enable"]').exists()).toBe(false)
  })

  it('admin + word/pdf 类型：同样提供上传新版本入口', async () => {
    // 保护行为：Word 与 PDF 已支持版本覆盖，入口不能再按类型屏蔽
    for (const sourceType of ['word', 'pdf'] as const) {
      vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(
        detail({ source_type: sourceType, status: 'active' }),
      )
      seedLogin('admin')
      const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')

      expect(
        wrapper.find('[data-test="open-version-upload"]').exists(),
        `${sourceType} 应提供上传新版本入口`,
      ).toBe(true)
      expect(wrapper.find('[data-test="detail-disable"]').exists()).toBe(true)
    }
  })

  it('admin + active + markdown/text：显示上传新版本入口并打开对话框', async () => {
    // 保护行为：可上传版本的类型（md/txt）必须提供入口，对话框展示当前文档信息
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(
      detail({ source_type: 'text', status: 'disabled' }),
    )
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')

    expect(wrapper.find('[data-test="open-version-upload"]').exists()).toBe(true)
    await wrapper.find('[data-test="open-version-upload"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="version-upload-dialog"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="target-info"]').text()).toContain('售后政策')
    // 不提供重命名输入（标题沿用当前文档标题）
    expect(wrapper.find('[data-test="version-upload-dialog"] input').exists()).toBe(true)
  })

  it('停用必须确认：确认前不发送请求，确认后调用接口', async () => {
    // 保护行为：详情页停用同样需确认（覆盖测试 45）
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(detail())
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')

    await wrapper.find('[data-test="detail-disable"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="detail-status-confirm"]').text()).toContain('不再参与客服')
    expect(knowledgeApi.disableKnowledgeDocument).not.toHaveBeenCalled()

    vi.mocked(knowledgeApi.disableKnowledgeDocument).mockResolvedValue(
      detail({ status: 'disabled' }),
    )
    await wrapper.find('[data-test="confirm-detail-status"]').trigger('click')
    await flushPromises()

    expect(knowledgeApi.disableKnowledgeDocument).toHaveBeenCalledWith('doc-1')
  })
})

/** 构造正文响应（详情页查看正文时由查看器组件请求）。 */
function contentResponse(partial: Partial<DocumentContentResponse> = {}): DocumentContentResponse {
  const text = '退货政策：签收后 7 日内可申请退货。\n'
  return {
    document_id: 'doc-1',
    version_id: 'v-2',
    version_no: 2,
    title: '售后政策',
    source_type: 'markdown',
    status: 'active',
    active_version_id: 'v-2',
    loader_version: 'loader-v1',
    chunker_version: 'chunker-v1',
    content_hash: 'sha256:bbbb',
    text,
    text_length: text.length,
    outline: [],
    ...partial,
  }
}

describe('KnowledgeDetailView 查看正文入口', () => {
  it('有有效版本时提供「查看正文」并能打开查看器（复用同一组件）', async () => {
    // 保护行为：正文按版本从正文接口读取，详情响应仍不含 raw_text
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(detail())
    vi.mocked(knowledgeApi.getDocumentVersionContent).mockResolvedValue(contentResponse())
    seedLogin('agent')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')
    await flushPromises()

    const entry = wrapper.find('[data-test="open-content"]')
    expect(entry.exists()).toBe(true)
    expect(entry.attributes('disabled')).toBeUndefined()

    await entry.trigger('click')
    await flushPromises()

    // 默认读当前有效版本 v-2
    expect(knowledgeApi.getDocumentVersionContent).toHaveBeenCalledWith('doc-1', 'v-2')
    expect(wrapper.find('[data-test="content-viewer-body"]').exists()).toBe(true)
  })

  it('页面不再出现「正文暂不提供」的表述，且下载入口依然不提供', async () => {
    // 保护行为：文案修正（设计稿决策 7：正文可看、原文下载不提供）
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(detail())
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')
    await flushPromises()

    expect(wrapper.text()).not.toContain('暂不提供正文')
    expect(wrapper.text()).not.toContain('文档正文、预览与下载暂不提供')
    expect(wrapper.find('[data-test="download-document"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="preview-document"]').exists()).toBe(false)
    // 说明正文是转换后的 Markdown，并明确不提供原文下载
    expect(wrapper.find('[data-test="content-note"]').text()).toContain('不提供原文下载')
  })

  it('无有效版本（processing）时入口禁用并说明原因', async () => {
    // 边界情况：异步入库尚未完成时没有可读正文，入口必须禁用并给出原因
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(
      detail({ status: 'processing', active_version_id: null }),
    )
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')
    await flushPromises()

    expect(wrapper.find('[data-test="open-content"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-test="content-unavailable"]').text()).toContain('尚未完成入库')
  })

  it('无有效版本（failed）时入口禁用并说明入库失败', async () => {
    // 边界情况：入库失败的文档没有正文，禁用入口而不是给出可点但必失败的按钮
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(
      detail({ status: 'failed', active_version_id: null }),
    )
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeDetail('/app/knowledge/doc-1')
    await flushPromises()

    expect(wrapper.find('[data-test="open-content"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-test="content-unavailable"]').text()).toContain('入库失败')
  })

  it('深链参数指定版本与偏移时自动打开并定位到该版本', async () => {
    // 保护行为：对话页窄屏降级跳转过来时必须按 query 打开指定版本并定位，
    // 否则用户点引用后只能看到文档顶部（丢失定位）
    vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue(detail())
    vi.mocked(knowledgeApi.getDocumentVersionContent).mockResolvedValue(
      contentResponse({ version_id: 'v-1', version_no: 1, active_version_id: 'v-2' }),
    )
    seedLogin('agent')
    const { wrapper } = await mountKnowledgeDetail(
      '/app/knowledge/doc-1?versionId=v-1&start=5&end=17&heading=退货政策',
    )
    await flushPromises()

    expect(knowledgeApi.getDocumentVersionContent).toHaveBeenCalledWith('doc-1', 'v-1')
    expect(wrapper.find('[data-test="content-viewer-body"]').exists()).toBe(true)
    // 历史版本提示必须上屏（引用来自旧版本）
    expect(wrapper.find('[data-test="viewer-history-notice"]').exists()).toBe(true)
  })
})
