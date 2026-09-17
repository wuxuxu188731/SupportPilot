/*
 * 聊天 Store 测试：列表加载、创建后选中、历史加载、发送成功/失败、
 * 超时不确认提示、防重复提交、404 会话失效、企业切换清理与迟到响应隔离。
 * 全程 mock API 层，不依赖任何真实模型或外部服务。
 */

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ORGANIZATION_STORAGE_KEY } from '@/stores/persistence'
import { runTenantResetHandlers } from '@/stores/tenantReset'
import { ApiError } from '@/api/errors'

vi.mock('@/api/chat', () => ({
  listConversations: vi.fn(),
  createConversation: vi.fn(),
  getConversationHistory: vi.fn(),
  updateSystemPrompt: vi.fn(),
  sendChatMessage: vi.fn(),
}))

import * as chatApi from '@/api/chat'
import { useChatStore } from '@/stores/chat'
import type {
  Citation,
  ConversationHistoryResponse,
  ConversationListItem,
  LLMResponse,
} from '@/api/types'

/** 构造一条会话列表项。 */
function conversation(id: string, title = '会话标题'): ConversationListItem {
  return {
    conversation_id: id,
    title,
    created_at: '2026-09-01 08:00:00',
    updated_at: '2026-09-01 08:00:00',
  }
}

/** 构造空历史响应。 */
function emptyHistory(id: string): ConversationHistoryResponse {
  return {
    conversation_id: id,
    system_prompt: null,
    created_at: '2026-09-01 08:00:00',
    updated_at: '2026-09-01 08:00:00',
    messages: [],
  }
}

/** 构造包含问答文本的历史响应（回答级结构化字段为空值，不含 events/审批）。 */
function textHistory(id: string): ConversationHistoryResponse {
  return {
    conversation_id: id,
    system_prompt: '企业偏好',
    created_at: '2026-09-01 08:00:00',
    updated_at: '2026-09-01 09:00:00',
    messages: [
      {
        sequence: 1,
        role: 'user',
        content: '第一个问题',
        created_at: '2026-09-01 08:01:00',
        citations: [],
        answer_incomplete: false,
      },
      {
        sequence: 2,
        role: 'assistant',
        content: '第一个回答',
        created_at: '2026-09-01 08:02:00',
        citations: [],
        answer_incomplete: false,
      },
    ],
  }
}

/** 构造一条历史引用（与实时响应同形：含 content 与偏移）。 */
function historyCitation(citationId: string): Citation {
  return {
    citation_id: citationId,
    document_id: `doc-${citationId}`,
    version_id: `ver-${citationId}`,
    chunk_id: `chunk-${citationId}`,
    title: `引用文档 ${citationId}`,
    heading_path: null,
    content: `引用正文 ${citationId}`,
    start_offset: 0,
    end_offset: 4,
  }
}

/** 构造带引用的历史响应：引用顺序为回答中首次出现的顺序（C2 先于 C1）。 */
function citedHistory(id: string, answerIncomplete = false): ConversationHistoryResponse {
  return {
    conversation_id: id,
    system_prompt: null,
    created_at: '2026-09-01 08:00:00',
    updated_at: '2026-09-01 09:00:00',
    messages: [
      {
        sequence: 1,
        role: 'user',
        content: '带引用的问题',
        created_at: '2026-09-01 08:01:00',
        citations: [],
        answer_incomplete: false,
      },
      {
        sequence: 2,
        role: 'assistant',
        content: '带引用的回答 [C2] 与 [C1]',
        created_at: '2026-09-01 08:02:00',
        citations: [historyCitation('C2'), historyCitation('C1')],
        answer_incomplete: answerIncomplete,
      },
    ],
  }
}

/** 构造「老后端/升级前」的历史响应：消息缺少 citations / answer_incomplete 字段。 */
function legacyHistory(id: string): ConversationHistoryResponse {
  return {
    conversation_id: id,
    system_prompt: null,
    created_at: '2026-09-01 08:00:00',
    updated_at: '2026-09-01 09:00:00',
    messages: [
      { sequence: 1, role: 'user', content: '旧问题', created_at: '2026-09-01 08:01:00' },
      { sequence: 2, role: 'assistant', content: '旧回答', created_at: '2026-09-01 08:02:00' },
    ],
  } as unknown as ConversationHistoryResponse
}

/** 构造一个带结构化字段的 LLMResponse。 */
function fullResponse(): LLMResponse {
  return {
    llm_answer: '这是最终回答 [C1]',
    llm_reasoning_content: '内部推理内容',
    events: [
      {
        type: 'tool_call.completed',
        timestamp: '2026-09-01T09:00:00.000000',
        tool_call_id: 'call-1',
        tool_call_name: 'search_knowledge',
        tool_call_arguments: {},
        result: null,
        error: null,
        duration_ms: 12,
      },
    ],
    citations: [
      {
        citation_id: 'C1',
        document_id: 'doc-1',
        version_id: 'ver-1',
        chunk_id: 'chunk-1',
        title: '退货政策',
        heading_path: null,
        content: '引用正文',
      },
    ],
    retrieval_summary: {
      strategy: 'adaptive',
      round_count: 1,
      evidence_status: 'SUFFICIENT',
      latency_ms: 88,
    },
    answer_incomplete: false,
    pending_approvals: [
      {
        run_id: 'run-1',
        proposal_id: 'proposal-1',
        approval_id: 'approval-1',
        action_type: 'refund',
        status: 'awaiting_approval',
        amount_cents: 123456,
        currency: 'CNY',
        resume_required: false,
        error_code: null,
      },
    ],
  }
}

/** 预置当前企业（等价于用户已选择企业）。 */
function seedOrganization(organizationId = 'org-1'): void {
  window.localStorage.setItem(
    ORGANIZATION_STORAGE_KEY,
    JSON.stringify({ organizationId }),
  )
}

/** 生成带状态码的 ApiError（模拟后端响应）。 */
function apiError(status: number, message = 'failed'): ApiError {
  return new ApiError({ status, code: null, message, fieldErrors: {}, raw: null })
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('会话列表加载', () => {
  it('加载成功：填充列表并结束加载态', async () => {
    // 保护行为：列表必须来自服务端并进入已加载状态（覆盖：会话列表加载）
    vi.mocked(chatApi.listConversations).mockResolvedValue([conversation('conv-1')])
    const store = useChatStore()
    seedOrganization()

    await store.loadConversations()

    expect(store.conversations).toHaveLength(1)
    expect(store.conversations[0].conversation_id).toBe('conv-1')
    expect(store.conversationsLoaded).toBe(true)
    expect(store.listLoading).toBe(false)
    expect(store.listError).toBeNull()
  })

  it('加载失败：记录错误且不进入已加载状态', async () => {
    // 保护行为：列表加载失败必须可被页面感知并提供重试
    vi.mocked(chatApi.listConversations).mockRejectedValue(new Error('服务器不可用'))
    const store = useChatStore()
    seedOrganization()

    await store.loadConversations()

    expect(store.conversationsLoaded).toBe(false)
    expect(store.listError).toContain('服务器不可用')
  })
})

describe('创建会话', () => {
  it('创建成功：自动选中新会话并刷新列表', async () => {
    // 保护行为：创建后 store 应自动选中并准备导航；同时刷新列表
    vi.mocked(chatApi.createConversation).mockResolvedValue({ conversation_id: 'conv-new' })
    vi.mocked(chatApi.listConversations).mockResolvedValue([conversation('conv-new', '新会话')])
    const store = useChatStore()
    seedOrganization()

    const id = await store.createConversation('偏好提示词')

    expect(id).toBe('conv-new')
    expect(store.currentConversationId).toBe('conv-new')
    expect(store.systemPrompt).toBe('偏好提示词')
    expect(store.creating).toBe(false)
    // 创建成功后应刷新过列表
    expect(chatApi.listConversations).toHaveBeenCalled()
  })

  it('创建期间重复调用被拦截', async () => {
    // 边界情况：store 层防重复创建兜底（按钮 loading 之外的保障）
    let resolveCreate!: (value: { conversation_id: string }) => void
    vi.mocked(chatApi.createConversation).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCreate = resolve
        }),
    )
    vi.mocked(chatApi.listConversations).mockResolvedValue([])
    const store = useChatStore()
    seedOrganization()

    const first = store.createConversation()
    await store.createConversation() // 进行中：直接忽略
    expect(chatApi.createConversation).toHaveBeenCalledTimes(1)

    resolveCreate({ conversation_id: 'conv-new' })
    await first
  })
})

describe('历史消息加载', () => {
  it('加载成功：消息按角色转换，只带回答级结构化信息', async () => {
    // 保护行为：历史恢复得到安全问答文本 + 回答级信息（引用与完整性标记），
    // 但不得伪造 events/pending_approvals（structured 恒为 null，服务端不保存它们）
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(textHistory('conv-1'))
    const store = useChatStore()
    seedOrganization()
    store.currentConversationId = 'conv-1'

    const result = await store.openConversation('conv-1')

    expect(result).toBe('ok')
    expect(store.messages).toHaveLength(2)
    expect(store.messages[0]).toMatchObject({
      role: 'user',
      content: '第一个问题',
      source: 'history',
      structured: null,
      structuredAnswer: null,
    })
    expect(store.messages[1]).toMatchObject({
      role: 'assistant',
      content: '第一个回答',
      source: 'history',
      structured: null,
      sendState: null,
      // 引用为空数组时也填充，便于渲染层统一判断
      structuredAnswer: { citations: [], answerIncomplete: false },
    })
    expect(store.systemPrompt).toBe('企业偏好')
  })

  it('同一会话已加载后重复打开直接返回 loaded，不发请求', async () => {
    // 边界情况：避免路由重复触发重复的历史请求
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(emptyHistory('conv-1'))
    const store = useChatStore()
    seedOrganization()

    await store.openConversation('conv-1')
    const second = await store.openConversation('conv-1')

    expect(second).toBe('loaded')
    expect(chatApi.getConversationHistory).toHaveBeenCalledTimes(1)
  })
})

describe('历史消息的引用与完整性标记', () => {
  it('历史响应带 citations：视图消息带引用，且不伪造结构化过程与审批', async () => {
    // 保护行为：历史引用来自服务端持久化结果，必须填进 structuredAnswer；
    // 同时 structured 仍为 null——不得凭空渲染处理过程与审批卡片（store 不伪造约定）
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(citedHistory('conv-1'))
    const store = useChatStore()
    seedOrganization()

    await store.openConversation('conv-1')

    const assistant = store.messages[1]
    expect(assistant.structuredAnswer?.citations.map((item) => item.citation_id)).toEqual(['C2', 'C1'])
    // 历史引用与实时同形：正文与偏移都要带过来
    expect(assistant.structuredAnswer?.citations[0].content).toBe('引用正文 C2')
    expect(assistant.structuredAnswer?.citations[0].start_offset).toBe(0)
    expect(assistant.structuredAnswer?.answerIncomplete).toBe(false)
    expect(assistant.structured).toBeNull()
  })

  it('历史响应缺 citations 字段（老后端/升级前数据）：兜底为空数组且不报错', async () => {
    // 边界情况：契约要求后端每次都返回新字段，但老后端/升级前的数据可能缺失；
    // 缺失时必须得到 { citations: [], answerIncomplete: false }，而不是 undefined 或抛错
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(legacyHistory('conv-1'))
    const store = useChatStore()
    seedOrganization()

    const result = await store.openConversation('conv-1')

    expect(result).toBe('ok')
    expect(store.messages).toHaveLength(2)
    expect(store.messages[1].structuredAnswer).toEqual({ citations: [], answerIncomplete: false })
  })

  it('历史响应 answer_incomplete=true：完整性标记原样带进视图', async () => {
    // 保护行为：完整性标记是回答级信号，必须原样透出（渲染层据此显示「谨慎采用」）
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(citedHistory('conv-1', true))
    const store = useChatStore()
    seedOrganization()

    await store.openConversation('conv-1')

    expect(store.messages[1].structuredAnswer?.answerIncomplete).toBe(true)
    expect(store.messages[1].structuredAnswer?.citations).toHaveLength(2)
  })

  it('用户消息的 structuredAnswer 恒为 null（历史与实时都不填）', async () => {
    // 边界情况：引用与完整性标记都是「回答级」信息，用户消息无论来自历史
    // 还是本页实时发送都不得填充
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(citedHistory('conv-1'))
    vi.mocked(chatApi.sendChatMessage).mockResolvedValue(fullResponse())
    const store = useChatStore()
    seedOrganization()

    await store.openConversation('conv-1')
    await store.sendMessage('后续问题')

    const userMessages = store.messages.filter((message) => message.role === 'user')
    expect(userMessages).toHaveLength(2)
    for (const message of userMessages) {
      expect(message.structuredAnswer).toBeNull()
    }
  })
})

describe('发送消息', () => {
  it('发送成功：展示回答与结构化结果', async () => {
    // 保护行为：成功后用 llm_answer 填充助手消息，并保留结构化内容
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(emptyHistory('conv-1'))
    vi.mocked(chatApi.sendChatMessage).mockResolvedValue(fullResponse())
    const store = useChatStore()
    seedOrganization()

    await store.openConversation('conv-1')
    const result = await store.sendMessage('新的问题')

    expect(result).toBe('sent')
    expect(store.messages).toHaveLength(2)
    const assistant = store.messages[1]
    expect(assistant.role).toBe('assistant')
    expect(assistant.content).toBe('这是最终回答 [C1]')
    expect(assistant.sendState).toBe('ok')
    expect(assistant.structured?.citations).toHaveLength(1)
    expect(assistant.structured?.pending_approvals).toHaveLength(1)
    expect(store.sending).toBe(false)
  })

  it('发送成功：structuredAnswer 由本轮响应填充（实时与历史同一读取口径）', async () => {
    // 保护行为：实时回答的引用与完整性标记来自本轮响应，必须填充 structuredAnswer，
    // 供渲染层与历史消息走同一套读取逻辑
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(emptyHistory('conv-1'))
    vi.mocked(chatApi.sendChatMessage).mockResolvedValue(fullResponse())
    const store = useChatStore()
    seedOrganization()

    await store.openConversation('conv-1')
    await store.sendMessage('新的问题')

    const assistant = store.messages[1]
    expect(assistant.structuredAnswer).toEqual({
      citations: fullResponse().citations,
      answerIncomplete: false,
    })
  })

  it('发送失败：assistant 占位消息的 structuredAnswer 保持 null', async () => {
    // 边界情况：失败的消息没有回答级信息，不得留下空对象或旧值（避免渲染层误判有引用）
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(emptyHistory('conv-1'))
    vi.mocked(chatApi.sendChatMessage).mockRejectedValue(apiError(500, '模型暂时不可用'))
    const store = useChatStore()
    seedOrganization()

    await store.openConversation('conv-1')
    await store.sendMessage('会失败的问题')

    expect(store.messages[1].sendState).toBe('error')
    expect(store.messages[1].structuredAnswer).toBeNull()
  })

  it('发送失败：助手消息标记失败且保留用户输入', async () => {
    // 保护行为：失败时用户问题保留在列表中并给出明确失败标记
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(emptyHistory('conv-1'))
    vi.mocked(chatApi.sendChatMessage).mockRejectedValue(apiError(500, '模型暂时不可用'))
    const store = useChatStore()
    seedOrganization()

    await store.openConversation('conv-1')
    const result = await store.sendMessage('会失败的问题')

    expect(result).toBe('error')
    expect(store.messages[0]).toMatchObject({ role: 'user', content: '会失败的问题' })
    expect(store.messages[1].sendState).toBe('error')
    expect(store.messages[1].errorMessage).toContain('模型暂时不可用')
    expect(store.sending).toBe(false)
  })

  it('网络超时/中断：提示结果状态可能不确定', async () => {
    // 保护行为：status=0（超时/断网）必须提示不确定状态并引导刷新历史
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(emptyHistory('conv-1'))
    vi.mocked(chatApi.sendChatMessage).mockRejectedValue(apiError(0))
    const store = useChatStore()
    seedOrganization()

    await store.openConversation('conv-1')
    await store.sendMessage('超时的问题')

    const message = store.messages[1]
    expect(message.sendState).toBe('error')
    expect(message.errorMessage).toContain('结果状态可能不确定')
    expect(message.errorMessage).toContain('先刷新历史')
  })

  it('发送期间重复提交被拦截', async () => {
    // 边界情况：同一会话发送中再次调用必须被忽略（防重复用户消息）
    let resolveSend!: (value: LLMResponse) => void
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(emptyHistory('conv-1'))
    vi.mocked(chatApi.sendChatMessage).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSend = resolve
        }),
    )
    const store = useChatStore()
    seedOrganization()

    await store.openConversation('conv-1')
    const first = store.sendMessage('第一次')
    const second = await store.sendMessage('第二次（应被忽略）')

    expect(second).toBe('idle')
    expect(chatApi.sendChatMessage).toHaveBeenCalledTimes(1)
    expect(store.messages).toHaveLength(2) // 只追加了一次用户消息+占位

    resolveSend(fullResponse())
    await first
    expect(store.messages).toHaveLength(2)
  })

  it('空白输入不发送', async () => {
    // 边界情况：去空白后为空的内容不允许发起请求
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(emptyHistory('conv-1'))
    const store = useChatStore()
    seedOrganization()

    await store.openConversation('conv-1')
    const result = await store.sendMessage('    ')

    expect(result).toBe('idle')
    expect(chatApi.sendChatMessage).not.toHaveBeenCalled()
  })
})

describe('会话失效（404）处理', () => {
  it('打开不存在的会话：清理选择、刷新列表并给出提示', async () => {
    // 保护行为：404 时必须清理当前失效选择、刷新列表（覆盖测试 19）
    vi.mocked(chatApi.getConversationHistory).mockRejectedValue(apiError(404))
    vi.mocked(chatApi.listConversations).mockResolvedValue([])
    const store = useChatStore()
    seedOrganization()
    store.currentConversationId = 'ghost'

    const result = await store.openConversation('ghost')

    expect(result).toBe('not-found')
    expect(store.currentConversationId).toBeNull()
    expect(store.messages).toEqual([])
    expect(store.notice).toBe('会话不存在或已不可访问')
    expect(chatApi.listConversations).toHaveBeenCalled()
  })

  it('发送时会话被删除：按 404 处理会话失效', async () => {
    // 保护行为：发送途中会话 404（被删除/失去权限）时同样走失效清理
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(emptyHistory('conv-1'))
    vi.mocked(chatApi.sendChatMessage).mockRejectedValue(apiError(404))
    vi.mocked(chatApi.listConversations).mockResolvedValue([])
    const store = useChatStore()
    seedOrganization()

    await store.openConversation('conv-1')
    const result = await store.sendMessage('问题')

    expect(result).toBe('not-found')
    expect(store.currentConversationId).toBeNull()
    expect(store.notice).toBe('会话不存在或已不可访问')
  })
})

describe('企业切换与迟到响应隔离', () => {
  it('切换企业：聊天状态被全部清理（tenantReset 机制）', async () => {
    // 保护行为：切换企业时通过 tenantReset 清空列表/会话/消息/错误
    vi.mocked(chatApi.listConversations).mockResolvedValue([conversation('conv-1')])
    const store = useChatStore()
    seedOrganization()
    await store.loadConversations()
    store.currentConversationId = 'conv-1'
    store.systemPrompt = '旧企业提示词'

    await runTenantResetHandlers()

    expect(store.conversations).toEqual([])
    expect(store.currentConversationId).toBeNull()
    expect(store.messages).toEqual([])
    expect(store.systemPrompt).toBeNull()
    expect(store.organizationId).toBeNull()
    expect(store.notice).toBeNull()
  })

  it('旧企业迟到响应不污染新企业状态', async () => {
    // 保护行为：旧企业尚未完成的请求返回后必须被丢弃，
    // 不得写入新企业的消息列表或发送状态（覆盖测试 18）
    let resolveSend!: (value: LLMResponse) => void
    vi.mocked(chatApi.getConversationHistory).mockResolvedValue(emptyHistory('conv-1'))
    vi.mocked(chatApi.sendChatMessage).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSend = resolve
        }),
    )
    const store = useChatStore()
    seedOrganization('org-old')
    await store.openConversation('conv-1')

    const pending = store.sendMessage('旧企业的问题')

    // 请求进行中切换企业：状态被清空
    seedOrganization('org-new')
    await runTenantResetHandlers()

    // 迟到响应到达：不得污染（新企业没有消息、没有发送态）
    resolveSend(fullResponse())
    await pending

    expect(store.organizationId).toBeNull()
    expect(store.messages).toEqual([])
    expect(store.sending).toBe(false)
  })
})
