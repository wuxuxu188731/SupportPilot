/*
 * 消息列表组件测试：用户/助手消息渲染、实时结构化展示（引用/审批/事件/
 * 检索摘要）、历史消息不伪造结构化内容、默认不展示推理内容。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import MessageList from '@/components/chat/MessageList.vue'
import type { ChatMessageView } from '@/stores/chat'
import type { LLMResponse } from '@/api/types'

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
  }
}

function assistantHistoryMessage(content: string): ChatMessageView {
  return {
    id: 2,
    role: 'assistant',
    content,
    createdAt: '2026-09-06 08:01:00',
    source: 'history',
    sendState: null,
    errorMessage: null,
    structured: null,
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
    ...overrides,
  }
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

describe('MessageList 历史消息安全性', () => {  it('历史消息不伪造引用与审批卡片', async () => {
    // 安全边界：服务端历史不含结构化信息，界面不得从文本猜测并伪造
    // 引用卡片/审批卡片（覆盖测试 25）
    const wrapper = mountList([
      userMessage('问题'),
      assistantHistoryMessage('回答里出现 [C1] 字样和审批 approval-1 字样也无卡片'),
    ])
    await flushPromises()

    expect(wrapper.find('[data-test="pending-approval-card"]').exists()).toBe(false)
    expect(wrapper.find('.citation-ref').exists()).toBe(false)
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
