/*
 * 消息列表组件测试：用户/助手消息渲染、实时结构化展示（引用/审批/事件/
 * 检索摘要）、历史回答的引用与完整性提示、无结构化数据时不伪造内容、
 * 引用锚点按回答作用域、默认不展示推理内容。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import MessageList from '@/components/chat/MessageList.vue'
import type { ChatMessageView } from '@/stores/chat'
import type { Citation, LLMResponse } from '@/api/types'

function userMessage(content: string, source: 'history' | 'live' = 'history'): ChatMessageView {
  return {
    id: 1,
    role: 'user',
    content,
    createdAt: '2026-09-06 08:00:00',
    source,
    sendState: 'ok',
    errorMessage: null,
    structured: null,
    // 用户消息没有回答级结构化信息，恒为 null
    structuredAnswer: null,
  }
}

function assistantHistoryMessage(
  content: string,
  overrides: Partial<ChatMessageView> = {},
): ChatMessageView {
  return {
    id: 2,
    role: 'assistant',
    content,
    createdAt: '2026-09-06 08:01:00',
    source: 'history',
    sendState: null,
    errorMessage: null,
    structured: null,
    // 历史回答的回答级信息来自服务端持久化；默认无引用、无完整性异常
    structuredAnswer: { citations: [], answerIncomplete: false },
    ...overrides,
  }
}

function assistantLiveMessage(overrides: Partial<ChatMessageView> = {}): ChatMessageView {
  return {
    id: 3,
    role: 'assistant',
    content: '',
    createdAt: '2026-09-06 08:02:00',
    source: 'live',
    sendState: 'sending',
    errorMessage: null,
    structured: null,
    // 发送中/失败的占位消息没有回答级信息
    structuredAnswer: null,
    ...overrides,
  }
}

/** 历史回答携带的结构化引用（与实时响应同形：含 content 与偏移）。 */
const HISTORY_CITATIONS: Citation[] = [
  {
    citation_id: 'C1',
    document_id: 'doc-1',
    version_id: 'ver-1',
    chunk_id: 'chunk-1',
    title: '引用文档',
    heading_path: null,
    content: '引用正文内容',
    start_offset: 3,
    end_offset: 11,
  },
]

/** 构造一条历史引用（默认与实时用例中的引用同形，可覆盖定位字段）。 */
function citation(partial: Partial<Citation> = {}): Citation {
  return { ...HISTORY_CITATIONS[0], ...partial }
}

const STRUCTURED: LLMResponse = {
  llm_answer: '答案正文 [C1]',
  llm_reasoning_content: '这是模型内部推理内容，不得展示',
  events: [
    {
      type: 'tool_call.completed',
      timestamp: '2026-09-06T08:02:00.000000',
      tool_call_id: 'call-1',
      tool_call_name: 'search_knowledge',
      tool_call_arguments: {},
      result: null,
      error: null,
      duration_ms: 20,
    },
  ],
  citations: [
    {
      citation_id: 'C1',
      document_id: 'doc-1',
      version_id: 'ver-1',
      chunk_id: 'chunk-1',
      title: '引用文档',
      heading_path: null,
      content: '引用正文内容',
      start_offset: 3,
      end_offset: 11,
    },
  ],
  retrieval_summary: {
    strategy: 'adaptive',
    round_count: 1,
    evidence_status: 'SUFFICIENT',
    latency_ms: 10,
  },
  answer_incomplete: false,
  pending_approvals: [
    {
      run_id: 'run-1',
      proposal_id: 'proposal-1',
      approval_id: 'approval-1',
      action_type: 'refund',
      status: 'awaiting_approval',
      amount_cents: 1000,
      currency: 'CNY',
      resume_required: false,
      error_code: null,
    },
  ],
}

/** 挂载消息列表。 */
function mountList(messages: ChatMessageView[], options: { empty?: boolean } = {}) {
  return mount(MessageList, {
    props: {
      messages,
      historyLoading: false,
      emptyConversation: options.empty ?? false,
    },
  })
}

describe('MessageList 基础消息渲染', () => {
  it('渲染用户消息与历史助手纯文本', async () => {
    // 保护行为：用户问题与历史问答都必须可读
    const wrapper = mountList([userMessage('我的问题'), assistantHistoryMessage('历史回答')])
    await flushPromises()

    expect(wrapper.text()).toContain('我的问题')
    expect(wrapper.text()).toContain('历史回答')
  })

  it('空会话显示欢迎空态', async () => {
    // 边界情况：无任何消息的会话展示引导而非空白
    const wrapper = mountList([], { empty: true })
    await flushPromises()
    expect(wrapper.text()).toContain('新会话')
  })
})

describe('MessageList 引用跳转透传', () => {
  it('引用卡片的查看入口会把结构化载荷上抛给 ChatView', async () => {
    // 保护行为：MessageList 只做透传，不解析也不改写引用定位信息
    const wrapper = mountList([
      userMessage('问题', 'live'),
      assistantLiveMessage({ sendState: 'ok', structured: STRUCTURED }),
    ])
    await flushPromises()

    await wrapper.find('[data-test="open-document-C1"]').trigger('click')

    const emitted = wrapper.emitted('open-document')
    expect(emitted).toHaveLength(1)
    expect(emitted?.[0]?.[0]).toMatchObject({
      documentId: 'doc-1',
      versionId: 'ver-1',
      startOffset: 3,
      endOffset: 11,
    })
  })
})

describe('MessageList 历史消息安全性', () => {
  it('历史消息没有结构化数据时不渲染引用与审批卡片', async () => {
    // 安全边界：历史消息的 structuredAnswer 为 null（或 citations 为空数组）时，
    // 界面不得从正文文本猜测并伪造引用卡片/审批卡片（覆盖测试 25，按新语义改写）
    const wrapper = mountList([
      userMessage('问题'),
      assistantHistoryMessage('回答里出现 [C1] 字样和审批 approval-1 字样也无卡片', {
        structuredAnswer: null,
      }),
      assistantHistoryMessage('引用为空数组的历史回答同样不渲染卡片', { id: 3, source: 'history' }),
    ])
    await flushPromises()

    expect(wrapper.find('[data-test="pending-approval-card"]').exists()).toBe(false)
    expect(wrapper.findAll('.citation-card')).toHaveLength(0)
    expect(wrapper.find('.citation-ref').exists()).toBe(false)
  })

  it('历史回答带 citations 时渲染引用卡片（引用来自服务端持久化）', async () => {
    // 保护行为：历史回答的引用与实时同形，卡片必须渲染出编号、标题与正文，
    // 但仍不渲染处理过程与审批卡片（structured 恒为 null，不伪造）
    const wrapper = mountList([
      userMessage('问题'),
      assistantHistoryMessage('历史回答 [C1]', {
        structuredAnswer: { citations: HISTORY_CITATIONS, answerIncomplete: false },
      }),
    ])
    await flushPromises()

    expect(wrapper.findAll('.citation-card')).toHaveLength(1)
    expect(wrapper.text()).toContain('[C1] 引用文档')
    expect(wrapper.find('[data-test="open-document-C1"]').exists()).toBe(true)
    // citations 非空时正文 [C1] 才是可点击的引用标记
    expect(wrapper.find('.citation-ref').exists()).toBe(true)
    expect(wrapper.find('[data-test="agent-timeline"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="pending-approval-card"]').exists()).toBe(false)
  })

  it('历史回答 answer_incomplete=true 时显示「谨慎采用」提示', async () => {
    // 保护行为：完整性标记来自服务端持久化结果，true 必须提示证据可能不完整
    const wrapper = mountList([
      userMessage('问题'),
      assistantHistoryMessage('证据不足的历史回答', {
        structuredAnswer: { citations: HISTORY_CITATIONS, answerIncomplete: true },
      }),
    ])
    await flushPromises()

    expect(wrapper.text()).toContain('证据引用可能不完整')
    expect(wrapper.text()).toContain('谨慎采用')
  })

  it('历史回答 answer_incomplete=false 时不显示「谨慎采用」提示', async () => {
    // 边界情况：正常的完整回答不得出现不完整警告（避免噪音干扰）
    const wrapper = mountList([
      userMessage('问题'),
      assistantHistoryMessage('证据完整的历史回答', {
        structuredAnswer: { citations: HISTORY_CITATIONS, answerIncomplete: false },
      }),
    ])
    await flushPromises()

    expect(wrapper.text()).not.toContain('证据引用可能不完整')
  })

  it('历史引用的证据原文渲染成 blockquote，且与实时展示一致', async () => {
    // 保护行为：片段正文随回答入库，刷新前后的证据展示不得有差异
    const live = mountList([
      userMessage('问题', 'live'),
      assistantLiveMessage({ content: '答案正文 [C1]', sendState: 'ok', structured: STRUCTURED }),
    ])
    await flushPromises()

    const history = mountList([
      userMessage('问题'),
      assistantHistoryMessage('历史回答 [C1]', {
        structuredAnswer: { citations: HISTORY_CITATIONS, answerIncomplete: false },
      }),
    ])
    await flushPromises()

    const historyQuote = history.find('.citation-content')
    expect(historyQuote.element.tagName).toBe('BLOCKQUOTE')
    expect(historyQuote.text()).toBe('引用正文内容')
    // 与实时回答的证据引用块逐字一致
    expect(historyQuote.text()).toBe(live.find('.citation-content').text())
  })

  it('历史引用的「查看原文位置」emit 结构化载荷（不解析回答文本）', async () => {
    // 保护行为：历史引用同样能跳转来源，载荷逐字段取自结构化 citations
    const wrapper = mountList([
      userMessage('问题'),
      assistantHistoryMessage('历史回答 [C1]', {
        structuredAnswer: { citations: HISTORY_CITATIONS, answerIncomplete: false },
      }),
    ])
    await flushPromises()

    await wrapper.find('[data-test="open-document-C1"]').trigger('click')

    const emitted = wrapper.emitted('open-document')
    expect(emitted).toHaveLength(1)
    expect(emitted?.[0]?.[0]).toEqual({
      documentId: 'doc-1',
      versionId: 'ver-1',
      startOffset: 3,
      endOffset: 11,
      headingPath: null,
      title: '引用文档',
    })
  })

  it('历史 Agent 消息按 Markdown 渲染', async () => {
    // 保护行为：历史恢复的回答同样是 Markdown 文本，必须渲染富文本而非语法源码
    const wrapper = mountList([
      userMessage('问题'),
      assistantHistoryMessage('### 退货政策\n\n- 7 天无理由\n- 需保持完好'),
    ])
    await flushPromises()

    expect(wrapper.find('h3').text()).toBe('退货政策')
    expect(wrapper.find('[data-test="agent-message"]').findAll('li')).toHaveLength(2)
    expect(wrapper.text()).not.toContain('###')
  })

  it('历史 Agent 消息中的 HTML 以纯文本展示', async () => {
    // 安全边界：Markdown 渲染不得执行历史消息里的标签
    const wrapper = mountList([
      userMessage('问题'),
      assistantHistoryMessage('回答 <script>alert(1)</script>'),
    ])
    await flushPromises()

    expect(wrapper.text()).toContain('<script>alert(1)</script>')
    expect(wrapper.find('.agent-bubble').element.innerHTML).not.toContain('<script>')
  })

  it('用户消息保持纯文本回显（不按 Markdown 渲染）', async () => {
    // 边界情况：用户输入按原文回显，避免把用户问题里的符号当成排版语法
    const wrapper = mountList([userMessage('请问 **这个** 怎么处理')])
    await flushPromises()

    expect(wrapper.find('[data-test="user-message"]').element.innerHTML).toContain('**这个**')
  })
})

describe('MessageList 引用锚点作用域', () => {
  it('同一页面两条回答都含 C1 时，各自正文的 [C1] 只定位到自己的引用卡片', async () => {
    // 保护行为（设计 5.4）：引用卡片 id 必须按回答作用域加前缀。
    // 若沿用全局 id，两条回答的卡片同为 citation-C1，点第二条回答的 [C1]
    // 会被 getElementById 带到第一条回答的卡片（今天实时多轮即可复现）
    const wrapper = mount(MessageList, {
      attachTo: document.body,
      props: {
        messages: [
          assistantHistoryMessage('第一条回答 [C1]', {
            id: 11,
            structuredAnswer: {
              citations: [citation({ document_id: 'doc-1' })],
              answerIncomplete: false,
            },
          }),
          assistantHistoryMessage('第二条回答 [C1]', {
            id: 22,
            structuredAnswer: {
              citations: [citation({ document_id: 'doc-2' })],
              answerIncomplete: false,
            },
          }),
        ],
        historyLoading: false,
        emptyConversation: false,
      },
    })
    await flushPromises()

    const cards = wrapper.findAll('.citation-card')
    expect(cards).toHaveLength(2)
    // 两条回答的卡片 id 互不相同，且分别带自己的回答前缀
    expect(cards[0].attributes('id')).toBe('answer-11-citation-C1')
    expect(cards[1].attributes('id')).toBe('answer-22-citation-C1')
    expect(cards[0].attributes('id')).not.toBe(cards[1].attributes('id'))
    expect(document.getElementById('answer-11-citation-C1')).toBe(cards[0].element)
    expect(document.getElementById('answer-22-citation-C1')).toBe(cards[1].element)

    // jsdom 未实现 scrollIntoView：为两张卡片分别装桩，断言各自归位
    const firstScroll = vi.fn()
    const secondScroll = vi.fn()
    cards[0].element.scrollIntoView = firstScroll
    cards[1].element.scrollIntoView = secondScroll

    const refs = wrapper.findAll('.citation-ref')
    expect(refs).toHaveLength(2)
    await refs[1].trigger('click')

    expect(secondScroll).toHaveBeenCalledTimes(1)
    expect(firstScroll).not.toHaveBeenCalled()
  })
})

describe('MessageList 实时结构化展示', () => {
  it('实时助手消息展示回答、引用、审批与检索摘要', async () => {
    // 保护行为：本页收到的新响应应完整展示结构化内容（覆盖测试 20/22）
    const wrapper = mountList([
      userMessage('问题', 'live'),
      assistantLiveMessage({
        content: '答案正文 [C1]',
        sendState: 'ok',
        structured: STRUCTURED,
      }),
    ])
    await flushPromises()

    expect(wrapper.text()).toContain('答案正文')
    expect(wrapper.find('[data-test="pending-approval-card"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="agent-timeline"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('引用文档')
    expect(wrapper.text()).toContain('¥10.00')
    expect(wrapper.text()).toContain('adaptive')
  })

  it('实时回答默认不展示 llm_reasoning_content', async () => {
    // 安全边界：模型推理内容不得作为普通客服内容展示（覆盖测试 24）
    const wrapper = mountList([
      userMessage('问题', 'live'),
      assistantLiveMessage({
        content: '答案正文',
        sendState: 'ok',
        structured: STRUCTURED,
      }),
    ])
    await flushPromises()

    expect(wrapper.text()).not.toContain('模型内部推理内容')
  })

  it('发送中的助手消息显示处理中占位', async () => {
    // 边界情况：等待 Agent 时必须给出明确的进行中状态
    const wrapper = mountList([userMessage('问题', 'live'), assistantLiveMessage({})])
    await flushPromises()

    expect(wrapper.text()).toContain('Agent 处理中')
  })

  it('失败的助手消息显示失败原因', async () => {
    // 保护行为：失败消息必须展示原因（含不确定状态提示），方便用户处理
    const wrapper = mountList([
      userMessage('问题', 'live'),
      assistantLiveMessage({
        sendState: 'error',
        errorMessage: '网络中断或请求超时，结果状态可能不确定，请先刷新历史再决定是否重新发送',
      }),
    ])
    await flushPromises()

    expect(wrapper.find('[data-test="agent-failed"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('结果状态可能不确定')
  })
})
