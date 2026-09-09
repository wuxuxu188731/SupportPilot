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

import * as authApi from '@/api/auth'
import * as chatApi from '@/api/chat'
import * as organizationApi from '@/api/organization'
import type { ConversationHistoryResponse, LLMResponse } from '@/api/types'
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

/** 一条含两条问答的历史。 */
function historyResponse(): ConversationHistoryResponse {
  return {
    conversation_id: 'conv-1',
    system_prompt: null,
    created_at: '2026-09-01 08:00:00',
    updated_at: '2026-09-02 08:00:00',
    messages: [
      { sequence: 1, role: 'user', content: '用户历史问题', created_at: '2026-09-01 08:01:00' },
      { sequence: 2, role: 'assistant', content: 'Agent 历史回答', created_at: '2026-09-01 08:02:00' },
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
  vi.mocked(authApi.me).mockResolvedValue(USER)
  vi.mocked(organizationApi.listOrganizations).mockResolvedValue(ORG_LIST)
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
