/*
 * 会话与聊天模块 API 封装：/conversations/ 的列表、创建、历史、
 * 系统提示词更新与单轮聊天。
 *
 * 聊天 POST 特性（与其它接口不同，前端必须遵守）：
 *  - 使用独立、可配置的长超时（默认 180 秒，见 getChatTimeoutMs）；
 *  - 不允许自动重试：该请求可能实际执行了工具调用与提案创建，
 *    自动重试会造成重复用户消息或重复提案，失败结果交由用户
 *    刷新历史后自行决定是否重发（不在这里做任何 retry 逻辑）。
 */

import { httpClient } from './http'
import type {
  ChatRequest,
  ConversationCreated,
  ConversationHistoryResponse,
  ConversationListItem,
  CreateConversationRequest,
  LLMResponse,
  SystemPromptUpdated,
  UpdateSystemPromptRequest,
} from './types'

/** 会话列表默认每页条数（与后端默认一致，避免一次拉取过多）。 */
export const DEFAULT_CONVERSATION_LIST_LIMIT = 50

/** 读取聊天请求超时毫秒数：优先使用 VITE_CHAT_TIMEOUT_MS 环境变量。 */
export function getChatTimeoutMs(): number {
  const raw = import.meta.env.VITE_CHAT_TIMEOUT_MS
  const parsed = raw ? Number.parseInt(raw, 10) : Number.NaN
  // 环境变量非法或未设置时使用默认长超时（180 秒）
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 180_000
}

/** 会话列表分页参数（后端 GET /conversations/）。 */
export interface ConversationListQuery {
  /** 每页条数：1–100，默认 50 */
  limit?: number
  /** 跳过的条数：≥0，默认 0（加载更多时传当前已加载条数） */
  offset?: number
}

/** 拉取当前企业、当前用户自己的会话列表（按更新时间倒序）。 */
export async function listConversations(
  query: ConversationListQuery = {},
): Promise<ConversationListItem[]> {
  const response = await httpClient.get<ConversationListItem[]>('/conversations/', {
    params: { limit: query.limit ?? DEFAULT_CONVERSATION_LIST_LIMIT, offset: query.offset ?? 0 },
  })
  return response.data
}

/** 新建会话：可选附带会话系统提示词；返回服务端会话 id。 */
export async function createConversation(
  payload: CreateConversationRequest,
): Promise<ConversationCreated> {
  const response = await httpClient.post<ConversationCreated>('/conversations/', payload)
  return response.data
}

/** 读取单个会话的安全历史消息（用户问题与 Agent 最终回答）。 */
export async function getConversationHistory(
  conversationId: string,
): Promise<ConversationHistoryResponse> {
  const response = await httpClient.get<ConversationHistoryResponse>(
    `/conversations/${encodeURIComponent(conversationId)}/messages/`,
  )
  return response.data
}

/** 更新会话系统提示词（会话附加偏好，不能覆盖服务器规则）。 */
export async function updateSystemPrompt(
  conversationId: string,
  payload: UpdateSystemPromptRequest,
): Promise<SystemPromptUpdated> {
  const response = await httpClient.put<SystemPromptUpdated>(
    `/conversations/${encodeURIComponent(conversationId)}/system-prompt/`,
    payload,
  )
  return response.data
}

/**
 * 发送一条聊天问题并等待 Agent 完整回答（同步长请求）。
 *
 * - 独立长超时：只对本请求生效，不影响全局短超时（如列表/历史）；
 * - 绝不自动重试：网络超时/中断后由界面提示「结果状态可能不确定」，
 *   用户刷新历史确认后再决定是否重新发送（见 Chat Store 处理逻辑）。
 */
export async function sendChatMessage(
  conversationId: string,
  question: string,
): Promise<LLMResponse> {
  const payload: ChatRequest = { question }
  const response = await httpClient.post<LLMResponse>(
    `/conversations/${encodeURIComponent(conversationId)}/chat/`,
    payload,
    {
      // 覆盖实例默认 60 秒超时：Agent 多轮工具调用可能耗时数十秒以上
      timeout: getChatTimeoutMs(),
    },
  )
  return response.data
}
