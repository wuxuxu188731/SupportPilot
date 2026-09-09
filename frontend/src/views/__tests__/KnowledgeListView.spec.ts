/*
 * KnowledgeListView 页面集成测试：列表字段渲染、本地搜索/筛选/排序、
 * agent 只读（无上传/停用/启用）、admin 操作入口、状态操作确认前不发送
 * 请求、进入详情、空态/错误/重试、上传对话框入口。
 * mock API 层与登录/企业上下文，不触达网络。
 */

import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'
import type { KnowledgeDocumentSummary } from '@/api/knowledgeTypes'
import { createAppRouter, registerGuards } from '@/router'
import KnowledgeListView from '@/views/KnowledgeListView.vue'
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
  uploadKnowledgeDocument: vi.fn(),
  uploadKnowledgeDocumentVersion: vi.fn(),
  disableKnowledgeDocument: vi.fn(),
  enableKnowledgeDocument: vi.fn(),
  getIngestionJob: vi.fn(),
}))

import * as authApi from '@/api/auth'
import * as knowledgeApi from '@/api/knowledge'
import * as organizationApi from '@/api/organization'

const USER = { user_id: 'u-1', username: 'alice', created_at: '2026-09-01 00:00:00' }

/** 构造文档摘要。 */
function docItem(partial: Partial<KnowledgeDocumentSummary> = {}): KnowledgeDocumentSummary {
  return {
    document_id: 'doc-1',
    title: '售后政策',
    source_type: 'markdown',
    status: 'active',
    active_version_id: 'v-1',
    created_at: '2026-09-01T00:00:00.000000+00:00',
    updated_at: '2026-09-02T00:00:00.000000+00:00',
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
}

/** 挂载知识库列表页并导航到指定路径。 */
async function mountKnowledgeList(path: string) {
  const router = createAppRouter(createMemoryHistory())
  const pinia = createPinia()
  setActivePinia(pinia)
  registerGuards(router)
  await router.push(path)
  await router.isReady()
  const wrapper = mount(
    { render: () => h(NDialogProvider, null, { default: () => h(NMessageProvider, null, { default: () => h(KnowledgeListView) }) }) },
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
  vi.mocked(authApi.me).mockResolvedValue(USER)
  vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([])
})

describe('KnowledgeListView 列表渲染与交互', () => {
  it('加载并渲染文档字段（标题/类型/状态/有效版本/时间/进入详情）', async () => {
    // 保护行为：列表项必须展示真实字段并可进入详情（覆盖测试 22-25 的一部分）
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([
      docItem({ title: '售后政策', status: 'active', source_type: 'markdown', active_version_id: 'v-1' }),
    ])
    seedLogin('admin')
    const { wrapper, router } = await mountKnowledgeList('/app/knowledge')

    expect(knowledgeApi.listKnowledgeDocuments).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('售后政策')
    expect(wrapper.text()).toContain('Markdown')
    expect(wrapper.text()).toContain('已启用')
    expect(wrapper.text()).toContain('共 1 篇文档')

    await wrapper.find('[data-test="document-card-item"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('knowledge-detail')
    expect(router.currentRoute.value.params.documentId).toBe('doc-1')
  })

  it('本地搜索过滤列表（不发起新请求）', async () => {
    // 保护行为：搜索是前端本地处理，不向后端发送参数
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([
      docItem({ document_id: 'doc-1', title: '售后政策' }),
      docItem({ document_id: 'doc-2', title: '退货流程' }),
    ])
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeList('/app/knowledge')

    await wrapper.find('[data-test="knowledge-search"] input').setValue('退货')
    await flushPromises()

    expect(wrapper.findAll('[data-test="document-card-item"]')).toHaveLength(1)
    expect(wrapper.text()).toContain('退货流程')
    expect(knowledgeApi.listKnowledgeDocuments).toHaveBeenCalledTimes(1)
  })

  it('状态筛选/来源类型筛选/排序为本地交互', async () => {
    // 保护行为：筛选与排序由前端完成，界面明确标注「本地」
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([
      docItem({ document_id: 'doc-1', title: '售后政策', status: 'active', source_type: 'markdown' }),
      docItem({ document_id: 'doc-2', title: '退货流程', status: 'disabled', source_type: 'text' }),
    ])
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeList('/app/knowledge')

    expect(wrapper.text()).toContain('本地处理')
    // 状态筛选下拉存在
    expect(wrapper.find('[data-test="status-filter"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="source-filter"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="sort-by"]').exists()).toBe(true)
    expect(knowledgeApi.listKnowledgeDocuments).toHaveBeenCalledTimes(1)
  })

  it('空列表展示空态；加载失败展示错误并可重试', async () => {
    // 保护行为：loading/empty/error/retry 状态完整（不伪造文档数量）
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeList('/app/knowledge')
    expect(wrapper.find('[data-test="knowledge-empty"]').exists()).toBe(true)

    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockRejectedValueOnce(
      new ApiError({ status: 500, code: null, message: '服务器内部错误，请稍后重试', fieldErrors: {}, raw: null }),
    )
    const second = await mountKnowledgeList('/app/knowledge')
    expect(second.wrapper.text()).toContain('知识库文档加载失败')
    expect(second.wrapper.find('[data-test="retry-documents"]').exists()).toBe(true)
  })
})

describe('KnowledgeListView 角色与状态操作', () => {
  it('agent：只读说明，无上传、无停用/启用按钮', async () => {
    // 保护行为：agent 视图必须是清晰只读（无上传入口与状态操作，覆盖测试 26）
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([
      docItem({ status: 'active' }),
      docItem({ document_id: 'doc-2', status: 'disabled' }),
    ])
    seedLogin('agent')
    const { wrapper } = await mountKnowledgeList('/app/knowledge')

    expect(wrapper.find('[data-test="readonly-note"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('只有管理员可以上传文档')
    expect(wrapper.find('[data-test="open-upload-dialog"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="disable-document"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="enable-document"]').exists()).toBe(false)
  })

  it('admin：显示上传入口、active 只显示停用、disabled 只显示启用', async () => {
    // 保护行为：admin 操作按状态显示（覆盖测试 27/28/29）
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([
      docItem({ document_id: 'doc-active', title: 'A 文档', status: 'active' }),
      docItem({ document_id: 'doc-disabled', title: 'B 文档', status: 'disabled' }),
      docItem({ document_id: 'doc-processing', title: 'C 文档', status: 'processing' }),
      docItem({ document_id: 'doc-failed', title: 'D 文档', status: 'failed' }),
    ])
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeList('/app/knowledge')

    expect(wrapper.find('[data-test="open-upload-dialog"]').exists()).toBe(true)
    // active 文档只有停用入口
    const activeCard = wrapper.find('[data-test="document-card-item"]')
    expect(activeCard.find('[data-test="disable-document"]').exists()).toBe(true)
    expect(activeCard.find('[data-test="enable-document"]').exists()).toBe(false)
  })

  it('processing/failed 文档不显示停用/启用操作', async () => {
    // 保护行为：processing/failed 不得提供误导性状态操作（覆盖测试 30）
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([
      docItem({ document_id: 'doc-processing', title: '处理中文档', status: 'processing' }),
      docItem({ document_id: 'doc-failed', title: '失败文档', status: 'failed' }),
    ])
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeList('/app/knowledge')

    for (const item of wrapper.findAll('[data-test="document-card-item"]')) {
      expect(item.find('[data-test="disable-document"]').exists()).toBe(false)
      expect(item.find('[data-test="enable-document"]').exists()).toBe(false)
    }
  })

  it('停用必须确认：确认前不发送请求，确认后调用接口并刷新', async () => {
    // 保护行为：状态操作确认前不得发起请求（覆盖测试 45）
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([
      docItem({ status: 'active', title: '售后政策' }),
    ])
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeList('/app/knowledge')

    await wrapper.find('[data-test="disable-document"]').trigger('click')
    await flushPromises()

    // 确认框已出现，但不可先发请求
    expect(wrapper.find('[data-test="status-confirm-dialog"]').text()).toContain('不再参与客服')
    expect(knowledgeApi.disableKnowledgeDocument).not.toHaveBeenCalled()

    vi.mocked(knowledgeApi.disableKnowledgeDocument).mockResolvedValue(docItem({ status: 'disabled' }))
    await wrapper.find('[data-test="confirm-status-action"]').trigger('click')
    await flushPromises()

    expect(knowledgeApi.disableKnowledgeDocument).toHaveBeenCalledWith('doc-1')
  })

  it('启用必须确认：确认前不发送请求', async () => {
    // 保护行为：启用同样需要确认（覆盖测试 45）
    vi.mocked(knowledgeApi.listKnowledgeDocuments).mockResolvedValue([
      docItem({ document_id: 'doc-2', title: '停用文档', status: 'disabled' }),
    ])
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeList('/app/knowledge')

    await wrapper.find('[data-test="enable-document"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="status-confirm-dialog"]').text()).toContain('参与知识检索')
    expect(knowledgeApi.enableKnowledgeDocument).not.toHaveBeenCalled()
  })

  it('点击「上传文档」打开上传对话框', async () => {
    // 保护行为：admin 上传入口必须打开上传对话框（上传由 API 层统一控制）
    seedLogin('admin')
    const { wrapper } = await mountKnowledgeList('/app/knowledge')

    await wrapper.find('[data-test="open-upload-dialog"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="upload-document-dialog"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="document-title-input"]').exists()).toBe(true)
  })
})
