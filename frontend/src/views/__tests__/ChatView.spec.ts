/*
 * ChatView 页面集成测试：进入会话加载历史、404 会话失效回列表并提示、
 * 空态引导、发送消息成功渲染 Agent 回答、菜单激活与新建会话入口可达。
 * mock API 层与登录/企业上下文，不触达网络。
 */

import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'
import { createAppRouter } from '@/router'
import ChatView from '@/views/ChatView.vue'
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
vi.mock('@/api/chat', () => ({
  listConversations: vi.fn(),
  createConversation: vi.fn(),
  getConversationHistory: vi.fn(),
  updateSystemPrompt: vi.fn(),
  sendChatMessage: vi.fn(),
}))
vi.mock('@/api/action', () => ({
  listApprovals: vi.fn(),
  getApprovalDetail: vi.fn(),
  decideApproval: vi.fn(),
  getRunStatus: vi.fn(),
  resumeRun: vi.fn(),
}))
vi.mock('@/api/knowledge', () => ({
  getDocumentVersionContent: vi.fn(),
  getKnowledgeDocumentDetail: vi.fn(),
}))

import * as authApi from '@/api/auth'
import * as chatApi from '@/api/chat'
import * as organizationApi from '@/api/organization'
import * as knowledgeApi from '@/api/knowledge'
import type { ConversationHistoryResponse, LLMResponse } from '@/api/types'
import type { DocumentContentResponse } from '@/api/knowledgeTypes'
import { clearDocumentContentCache } from '@/utils/documentContentCache'
const USER = { user_id: 'u-1', username: 'alice', created_at: '2026-09-01 00:00:00' }
const ORG_LIST = [{ organization_id: 'org-1', name: '示例企业', role: 'admin' as const }]
const CONVERSATIONS = [
  {
    conversation_id: 'conv-1',
    title: '订单咨询',
    created_at: '2026-09-01 08:00:00',
    updated_at: '2026-09-02 08:00:00',
  },
]

/** 一条含两条问答的历史（回答级结构化字段必填，按升级前语义给空值）。 */
function historyResponse(): ConversationHistoryResponse {
  return {
    conversation_id: 'conv-1',
    system_prompt: null,
    created_at: '2026-09-01 08:00:00',
    updated_at: '2026-09-02 08:00:00',
    messages: [
      {
        sequence: 1,
        role: 'user',
        content: '用户历史问题',
        created_at: '2026-09-01 08:01:00',
        citations: [],
        answer_incomplete: false,
      },
      {
        sequence: 2,
        role: 'assistant',
        content: 'Agent 历史回答',
        created_at: '2026-09-01 08:02:00',
        citations: [],
        answer_incomplete: false,
      },
    ],
  }
}

/** 带结构化内容的实时回答（用于发送流程断言）。 */
function chatResponse(): LLMResponse {
  return {
    llm_answer: '实时回答正文',
    llm_reasoning_content: '不应展示的推理',
    events: [],
    citations: [],
    retrieval_summary: null,
    answer_incomplete: false,
    pending_approvals: [],
  }
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

/** 正文响应（覆盖引用跳转所需字段）。 */
const DOC_TEXT = '退货政策：签收后 7 日内可申请退货。\n'

function docContent(
  partial: Partial<DocumentContentResponse> = {},
): DocumentContentResponse {
  return {
    document_id: 'doc-1',
    version_id: 'ver-1',
    version_no: 1,
    title: '退货与换货政策',
    source_type: 'markdown',
    status: 'active',
    active_version_id: 'ver-1',
    loader_version: 'loader-v1',
    chunker_version: 'chunker-v1',
    content_hash: 'sha256:abc',
    text: DOC_TEXT,
    text_length: DOC_TEXT.length,
    outline: [],
    ...partial,
  }
}

/** 带一条引用的实时回答（偏移覆盖「签收后 7 日内可申请退货」）。 */
function chatResponseWithCitation(versionId = 'ver-1'): LLMResponse {
  return {
    ...chatResponse(),
    citations: [
      {
        citation_id: 'C1',
        document_id: 'doc-1',
        version_id: versionId,
        chunk_id: 'chunk-1',
        title: '退货与换货政策',
        heading_path: '退货政策',
        content: '签收后 7 日内可申请退货',
        start_offset: 5,
        end_offset: 5 + '签收后 7 日内可申请退货'.length,
      },
    ],
  }
}

/**
 * 固定窗口宽度匹配结果：true 表示允许同屏并排（>=1280px），false 走窄屏整页降级。
 * 测试环境默认 matchMedia 一律不匹配，因此需要显式指定。
 */
function stubViewport(isWide: boolean): void {
  window.matchMedia = ((query: string) => ({
    matches: isWide,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

/** 在会话内发送一条带引用的消息，返回可点击的引用入口。 */
async function sendMessageWithCitation(
  wrapper: Awaited<ReturnType<typeof mountChatView>>['wrapper'],
): Promise<void> {
  await wrapper.find('[data-test="chat-input"] textarea').setValue('退货政策是什么')
  await wrapper.find('[data-test="chat-send"]').trigger('click')
  await flushNavigation()
  await flushPromises()
}

/** 挂载 ChatView（含消息/对话框提供者），并导航到指定路径。 */
async function mountChatView(path: string) {
  const router = createAppRouter(createMemoryHistory())
  const pinia = createPinia()
  await router.push(path)
  await router.isReady()
  const wrapper = mount(
    { render: () => h(NDialogProvider, null, { default: () => h(NMessageProvider, null, { default: () => h(ChatView) }) }) },
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
  // 正文缓存是模块级：用例之间必须显式清空，否则会命中上一条用例的数据
  clearDocumentContentCache()
  vi.mocked(authApi.me).mockResolvedValue(USER)
  vi.mocked(organizationApi.listOrganizations).mockResolvedValue(ORG_LIST)
  vi.mocked(knowledgeApi.getDocumentVersionContent).mockResolvedValue(docContent())
  vi.mocked(knowledgeApi.getKnowledgeDocumentDetail).mockResolvedValue({
    document_id: 'doc-1',
    title: '退货与换货政策',
    source_type: 'markdown',
    status: 'active',
    active_version_id: 'ver-1',
    created_at: '2026-01-01T00:00:00+00:00',
    updated_at: '2026-01-01T00:00:00+00:00',
    versions: [],
    latest_job: null,
  } as never)
  // 默认按桌面宽度处理；窄屏用例自行覆盖
  stubViewport(true)
})

describe('ChatView 进入会话', () => {
  it('空列表时展示空态与新建引导', async () => {
    // 保护行为：/app/chat 无会话时应展示空状态或引导（覆盖测试 7 与页面空态）
    vi.mocked(chatApi.listConversations).mockResolvedValue([])
    seedLogin()

    const { wrapper } = await mountChatView('/app/chat')
    await flushPromises()

    expect(wrapper.find('[data-test="chat-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('新建会话')
  })

  it('加载历史并渲染问答消息（含会话列表标题）', async () => {
    // 保护行为：带会话 id 的路径必须从服务端加载历史并渲染
    vi.mocked(chatApi.listConversations).mockResolvedValue(CONVERSATIONS)
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(historyResponse())
    seedLogin()

    const { wrapper } = await mountChatView('/app/chat/conv-1')
    await flushNavigation()
    await flushPromises()

    expect(wrapper.text()).toContain('用户历史问题')
    expect(wrapper.text()).toContain('Agent 历史回答')
    expect(wrapper.text()).toContain('订单咨询')
  })

  it('不存在的会话回到空态并提示「会话不存在或已不可访问」', async () => {
    // 保护行为：404 会话必须清理失效选择、刷新列表、回到 /app/chat 并提示
    vi.mocked(chatApi.listConversations).mockResolvedValue([])
    vi.mocked(chatApi.getConversationHistory).mockRejectedValue(
      new ApiError({ status: 404, code: null, message: '会话不存在或已失效', fieldErrors: {}, raw: null }),
    )
    seedLogin()

    const { wrapper, router } = await mountChatView('/app/chat/ghost')
    await flushNavigation()
    await flushPromises()

    expect(router.currentRoute.value.name).toBe('chat')
    expect(wrapper.text()).toContain('会话不存在或已不可访问')
  })

  it('发送消息成功：展示 Agent 回答并清空输入', async () => {
    // 保护行为：发送后应立即展示结构化回答，输入框清空（覆盖测试 10/13/14）
    vi.mocked(chatApi.listConversations).mockResolvedValue(CONVERSATIONS)
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(historyResponse())
    vi.mocked(chatApi.sendChatMessage).mockResolvedValue(chatResponse())
    seedLogin()

    const { wrapper } = await mountChatView('/app/chat/conv-1')
    await flushNavigation()
    await flushPromises()

    const textarea = wrapper.find('[data-test="chat-input"] textarea')
    await textarea.setValue('请问退款多久到账')
    await wrapper.find('[data-test="chat-send"]').trigger('click')
    await flushNavigation()
    await flushPromises()

    expect(chatApi.sendChatMessage).toHaveBeenCalledWith('conv-1', '请问退款多久到账')
    expect(wrapper.text()).toContain('实时回答正文')
    // 不展示内部推理内容
    expect(wrapper.text()).not.toContain('不应展示的推理')
    const inputElement = wrapper.find('[data-test="chat-input"] textarea')
      .element as HTMLTextAreaElement
    expect(inputElement.value).toBe('')
  })
})

describe('ChatView 会话列表交互', () => {
  it('会话列表出现时点击切换会话并加载其历史', async () => {
    // 保护行为：从列表选择会话应导航并重新加载历史
    vi.mocked(chatApi.listConversations).mockResolvedValue(CONVERSATIONS)
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(historyResponse())
    seedLogin()

    const { wrapper, router } = await mountChatView('/app/chat')
    await flushNavigation()
    await flushPromises()

    // 会话列表项已渲染
    expect(wrapper.text()).toContain('订单咨询')

    // 点击会话项 → 进入该会话
    const item = wrapper.find('.list-item')
    await item.trigger('click')
    await flushNavigation()
    await flushPromises()

    expect(router.currentRoute.value.name).toBe('chat-detail')
    expect(router.currentRoute.value.params.conversationId).toBe('conv-1')
  })
})

describe('ChatView 待审批跳转', () => {
  it('点击待审批卡片「查看审批」跳转审批详情路由', async () => {
    // 保护行为：pending_approvals 的 open-detail 必须跳转真实审批详情，
    // 只使用结构化 approval_id，不从自然语言解析（覆盖要求 34）
    vi.mocked(chatApi.listConversations).mockResolvedValue(CONVERSATIONS)
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(historyResponse())
    vi.mocked(chatApi.sendChatMessage).mockResolvedValue({
      ...chatResponse(),
      pending_approvals: [
        {
          run_id: 'run-1',
          proposal_id: 'proposal-1',
          approval_id: 'approval-123',
          action_type: 'refund',
          status: 'awaiting_approval',
          amount_cents: 6000,
          currency: 'CNY',
          resume_required: false,
          error_code: null,
        },
      ],
    })
    seedLogin()

    const { wrapper, router } = await mountChatView('/app/chat/conv-1')
    await flushNavigation()
    await flushPromises()

    await wrapper.find('[data-test="chat-input"] textarea').setValue('需要退款审批')
    await wrapper.find('[data-test="chat-send"]').trigger('click')
    await flushNavigation()
    await flushPromises()

    expect(wrapper.text()).toContain('待审批提案')
    await wrapper.find('[data-test="open-approval"]').trigger('click')
    await flushNavigation()
    await flushPromises()

    expect(router.currentRoute.value.name).toBe('approval-detail')
    expect(router.currentRoute.value.params.approvalId).toBe('approval-123')
  })
})

describe('ChatView 引用跳转与正文面板', () => {
  it('桌面端点引用在右侧打开正文面板并高亮对应区间', async () => {
    // 保护行为：面板与对话同屏并排，打开时按引用偏移定位（不改变会话路由）
    vi.mocked(chatApi.listConversations).mockResolvedValue(CONVERSATIONS)
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(historyResponse())
    vi.mocked(chatApi.sendChatMessage).mockResolvedValue(chatResponseWithCitation())
    seedLogin()

    const { wrapper, router } = await mountChatView('/app/chat/conv-1')
    await flushNavigation()
    await flushPromises()
    await sendMessageWithCitation(wrapper)

    expect(wrapper.find('[data-test="document-content-panel"]').exists()).toBe(false)
    await wrapper.find('[data-test="open-document-C1"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="document-content-panel"]').exists()).toBe(true)
    expect(knowledgeApi.getDocumentVersionContent).toHaveBeenCalledWith('doc-1', 'ver-1')
    expect(wrapper.find('.sp-range-highlight').text()).toContain('签收后 7 日内可申请退货')
    // 面板是内嵌并排而不是路由跳转
    expect(router.currentRoute.value.name).toBe('chat-detail')
    // 对话区消息不因打开面板而丢失
    expect(wrapper.text()).toContain('实时回答正文')
  })

  it('关闭面板后对话仍在，且不重新加载历史', async () => {
    // 保护行为：关闭面板只收起面板——不重载会话、不丢消息、不改路由
    vi.mocked(chatApi.listConversations).mockResolvedValue(CONVERSATIONS)
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(historyResponse())
    vi.mocked(chatApi.sendChatMessage).mockResolvedValue(chatResponseWithCitation())
    seedLogin()

    const { wrapper, router } = await mountChatView('/app/chat/conv-1')
    await flushNavigation()
    await flushPromises()
    await sendMessageWithCitation(wrapper)

    await wrapper.find('[data-test="open-document-C1"]').trigger('click')
    await flushPromises()
    const historyCalls = vi.mocked(chatApi.getConversationHistory).mock.calls.length

    await wrapper.find('[data-test="close-document-panel"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="document-content-panel"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('实时回答正文')
    expect(router.currentRoute.value.name).toBe('chat-detail')
    expect(vi.mocked(chatApi.getConversationHistory).mock.calls.length).toBe(historyCalls)
  })

  it('同一版本内连续打开不同引用只重新定位，不重复请求正文', async () => {
    // 保护行为：面板缓存以 (document_id, version_id) 为粒度，重复打开同版本只定位
    vi.mocked(chatApi.listConversations).mockResolvedValue(CONVERSATIONS)
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(historyResponse())
    vi.mocked(chatApi.sendChatMessage).mockResolvedValue(chatResponseWithCitation())
    seedLogin()

    const { wrapper } = await mountChatView('/app/chat/conv-1')
    await flushNavigation()
    await flushPromises()
    await sendMessageWithCitation(wrapper)

    await wrapper.find('[data-test="open-document-C1"]').trigger('click')
    await flushPromises()
    expect(knowledgeApi.getDocumentVersionContent).toHaveBeenCalledTimes(1)

    // 关闭再打开同一条引用：命中组件内缓存，不产生新请求
    await wrapper.find('[data-test="close-document-panel"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-test="open-document-C1"]').trigger('click')
    await flushPromises()

    expect(knowledgeApi.getDocumentVersionContent).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.sp-range-highlight').exists()).toBe(true)
  })

  it('正文接口失败时面板内展示错误与重试，对话区不受影响', async () => {
    // 边界情况：正文加载失败不能影响对话区（引用卡片仍在），面板内可重试
    vi.mocked(chatApi.listConversations).mockResolvedValue(CONVERSATIONS)
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(historyResponse())
    vi.mocked(chatApi.sendChatMessage).mockResolvedValue(chatResponseWithCitation())
    vi.mocked(knowledgeApi.getDocumentVersionContent).mockRejectedValue(new Error('503'))
    seedLogin()

    const { wrapper } = await mountChatView('/app/chat/conv-1')
    await flushNavigation()
    await flushPromises()
    await sendMessageWithCitation(wrapper)

    await wrapper.find('[data-test="open-document-C1"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="viewer-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="viewer-retry"]').exists()).toBe(true)
    // 对话区完全不受影响：回答与引用卡片都还在
    expect(wrapper.text()).toContain('实时回答正文')
    expect(wrapper.find('[data-test="open-document-C1"]').exists()).toBe(true)
  })

  it('窄屏点击引用改为整页跳转文档详情，并带上版本与偏移', async () => {
    // 边界情况：窄屏并排会把对话压坏，必须降级为整页跳转（设计稿 4.4）
    stubViewport(false)
    vi.mocked(chatApi.listConversations).mockResolvedValue(CONVERSATIONS)
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(historyResponse())
    vi.mocked(chatApi.sendChatMessage).mockResolvedValue(chatResponseWithCitation())
    seedLogin()

    const { wrapper, router } = await mountChatView('/app/chat/conv-1')
    await flushNavigation()
    await flushPromises()
    await sendMessageWithCitation(wrapper)

    await wrapper.find('[data-test="open-document-C1"]').trigger('click')
    await flushNavigation()
    await flushPromises()

    expect(wrapper.find('[data-test="document-content-panel"]').exists()).toBe(false)
    expect(router.currentRoute.value.name).toBe('knowledge-detail')
    expect(router.currentRoute.value.params.documentId).toBe('doc-1')
    expect(router.currentRoute.value.query.versionId).toBe('ver-1')
    expect(router.currentRoute.value.query.start).toBe('5')
    expect(router.currentRoute.value.query.end).toBe(String(5 + '签收后 7 日内可申请退货'.length))
    // 来源标记：详情页据此给出「返回对话」，回到这条会话
    expect(router.currentRoute.value.query.from).toBe('chat')
    expect(router.currentRoute.value.query.conversationId).toBe('conv-1')
  })

  it('切换企业时关闭正文面板，不残留旧企业文档', async () => {
    // 保护行为：企业切换后文档正文不可继续展示（租户隔离的界面要求）
    vi.mocked(chatApi.listConversations).mockResolvedValue(CONVERSATIONS)
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(historyResponse())
    vi.mocked(chatApi.sendChatMessage).mockResolvedValue(chatResponseWithCitation())
    seedLogin()

    const { wrapper } = await mountChatView('/app/chat/conv-1')
    await flushNavigation()
    await flushPromises()
    await sendMessageWithCitation(wrapper)

    await wrapper.find('[data-test="open-document-C1"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="document-content-panel"]').exists()).toBe(true)

    const { useOrganizationStore } = await import('@/stores/organization')
    useOrganizationStore().$patch({ currentOrganizationId: 'org-2' })
    await flushNavigation()
    await flushPromises()

    expect(wrapper.find('[data-test="document-content-panel"]').exists()).toBe(false)
  })
})
