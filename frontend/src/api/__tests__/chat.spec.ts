/*
 * 聊天 API 封装测试：路径、查询参数、请求体与聊天专用长超时。
 * 通过 mock httpClient 进行，不触达网络。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

// mock 统一 HTTP 客户端：验证调用形态
vi.mock('@/api/http', () => ({
  httpClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}))

import * as chatApi from '@/api/chat'
import { httpClient } from '@/api/http'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('会话列表 API', () => {
  it('GET /conversations/ 携带默认 limit/offset 参数', async () => {
    // 保护行为：未显式传分页参数时应带默认 limit=50、offset=0
    vi.mocked(httpClient.get).mockResolvedValue({ data: [] })

    const result = await chatApi.listConversations()

    expect(result).toEqual([])
    expect(httpClient.get).toHaveBeenCalledWith('/conversations/', {
      params: { limit: 50, offset: 0 },
    })
  })

  it('分页参数原样透传（加载更多场景）', async () => {
    // 保护行为：调用方传入的 limit/offset 必须出现在查询串中
    vi.mocked(httpClient.get).mockResolvedValue({ data: [] })

    await chatApi.listConversations({ limit: 30, offset: 90 })

    expect(httpClient.get).toHaveBeenCalledWith('/conversations/', {
      params: { limit: 30, offset: 90 },
    })
  })
})

describe('历史消息与创建/更新 API', () => {
  it('GET /conversations/{id}/messages/ 按会话 id 请求', async () => {
    // 保护行为：历史读取路径必须包含会话 id，供后端做归属校验
    vi.mocked(httpClient.get).mockResolvedValue({ data: { messages: [] } })

    await chatApi.getConversationHistory('conv-1')

    expect(httpClient.get).toHaveBeenCalledWith('/conversations/conv-1/messages/')
  })

  it('POST /conversations/ 发送创建请求体', async () => {
    // 保护行为：创建会话只提交 system_prompt（可选），不伪造其它字段
    vi.mocked(httpClient.post).mockResolvedValue({ data: { conversation_id: 'conv-2' } })

    const result = await chatApi.createConversation({ system_prompt: '偏好' })

    expect(result.conversation_id).toBe('conv-2')
    expect(httpClient.post).toHaveBeenCalledWith('/conversations/', { system_prompt: '偏好' })
  })

  it('PUT /conversations/{id}/system-prompt/ 更新会话提示词', async () => {
    // 保护行为：系统提示词更新走 PUT 且请求体含 system_prompt
    vi.mocked(httpClient.put).mockResolvedValue({ data: { updated: true } })

    await chatApi.updateSystemPrompt('conv-1', { system_prompt: '新偏好' })

    expect(httpClient.put).toHaveBeenCalledWith('/conversations/conv-1/system-prompt/', {
      system_prompt: '新偏好',
    })
  })
})

describe('聊天发送 API（长超时且不自动重试）', () => {
  it('POST /conversations/{id}/chat/ 使用独立长超时（默认 180 秒）', async () => {
    // 保护行为：聊天请求必须携带独立的长超时配置（毫秒），
    // 与列表/历史等普通请求的 60 秒默认超时区分开
    vi.mocked(httpClient.post).mockResolvedValue({ data: { llm_answer: '回答' } })

    const result = await chatApi.sendChatMessage('conv-1', '问题')

    expect(result.llm_answer).toBe('回答')
    expect(httpClient.post).toHaveBeenCalledWith(
      '/conversations/conv-1/chat/',
      { question: '问题' },
      { timeout: 180_000 },
    )
  })

  it('VITE_CHAT_TIMEOUT_MS 可覆盖聊天超时', async () => {
    // 保护行为：部署环境可通过 VITE_CHAT_TIMEOUT_MS 覆盖默认 180 秒
    vi.stubEnv('VITE_CHAT_TIMEOUT_MS', '60000')
    try {
      expect(chatApi.getChatTimeoutMs()).toBe(60_000)
    } finally {
      vi.unstubAllEnvs()
    }
  })

  it('非法 VITE_CHAT_TIMEOUT_MS 回退默认长超时', async () => {
    // 边界情况：环境变量非数字或为负数时不允许产生无效超时，回退 180 秒
    vi.stubEnv('VITE_CHAT_TIMEOUT_MS', 'not-a-number')
    try {
      expect(chatApi.getChatTimeoutMs()).toBe(180_000)
    } finally {
      vi.unstubAllEnvs()
    }
  })

  it('聊天请求失败后不会自动重试（只调用一次）', async () => {
    // 保护行为：聊天 POST 绝不自动重试——自动重试可能造成重复用户消息
    // 或重复提案；失败由用户刷新历史后自行决定是否重发
    vi.mocked(httpClient.post).mockRejectedValueOnce(new Error('network down'))

    await expect(chatApi.sendChatMessage('conv-1', '问题')).rejects.toThrow('network down')
    expect(httpClient.post).toHaveBeenCalledTimes(1)
  })
})
