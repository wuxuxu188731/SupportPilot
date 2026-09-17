/*
 * 聊天 Store（Pinia）：会话列表、当前会话、消息、发送与系统提示词。
 *
 * 事实与安全说明（与后端行为对齐）：
 *  - 会话属于「用户 + 企业」二元组：所有读取只返回当前企业、当前登录
 *    用户自己的会话；无权访问的会话后端统一返回 404；
 *  - 服务端历史接口保存安全的问答文本，以及**回答级**的结构化展示信息
 *    （citations 引用与 answer_incomplete 完整性标记，来自服务端持久化，
 *    与实时响应同形）；events / pending_approvals / retrieval_summary
 *    仍只存在于「本次页面收到的新响应」中，刷新后不会恢复——本 Store
 *    不伪造历史的处理过程与审批卡片；
 *  - 发送聊天请求使用独立长超时且不自动重试；超时/断网提示
 *    「结果状态可能不确定」，由用户刷新历史后自行决定是否重发；
 *  - 切换企业/退出登录时由 tenantReset 机制清理全部聊天状态；
 *    旧企业尚未完成的请求返回后被 epoch 失效机制丢弃，不写入新企业。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import * as chatApi from '@/api/chat'
import type {
  Citation,
  ConversationHistoryResponse,
  ConversationListItem,
  LLMResponse,
} from '@/api/types'
import { ApiError } from '@/api/errors'
import { useOrganizationStore } from '@/stores/organization'
import { registerTenantResetHandler } from '@/stores/tenantReset'

/** 本页会话列表每页大小（加载更多时使用；后端不返回总数，靠页大小推断是否到底） */
export const CONVERSATION_PAGE_SIZE = 50

/** 单条可展示消息（内存视图，不直接等价于服务端存储结构）。 */
export interface ChatMessageView {
  /** 本地唯一标识（自增），用于消息更新与滚动定位 */
  id: number
  /** 消息角色：用户问题或助手消息 */
  role: 'user' | 'assistant'
  /** 消息正文：assistant 发送中为空字符串 */
  content: string
  /** 展示时间文本：历史消息为服务端 UTC 文本，本地消息为 ISO 时间 */
  createdAt: string
  /** 消息来源：history=服务端历史恢复；live=当前页面刚收到的往返 */
  source: 'history' | 'live'
  /** 助手消息的发送状态：sending=等待中；error=失败；ok=成功（历史消息为 null） */
  sendState: 'sending' | 'error' | 'ok' | null
  /** 失败提示（仅 sendState=error 时非空） */
  errorMessage: string | null
  /**
   * 结构化聊天结果（仅 live assistant 成功时有；历史消息恒为 null）：
   * 处理过程（events）、检索摘要与待审批提案等。服务端历史不保存这些内容，
   * 本 Store 不伪造历史处理过程或审批卡片。
   */
  structured: LLMResponse | null
  /**
   * 回答随附的结构化展示信息：实时来自本轮响应，历史来自服务端持久化结果。
   *
   * citations 与 answerIncomplete 都是「回答级」信息，统一在这里读取，
   * 渲染层无需区分实时/历史；citations 为空数组时也填
   * `{ citations: [], answerIncomplete: false }`，便于模板统一判断。
   * 用户消息、以及发送中/失败的占位助手消息恒为 null（没有回答级信息可填）。
   */
  structuredAnswer: { citations: Citation[]; answerIncomplete: boolean } | null
}

/** 会话操作结果码，供视图决定路由去向与提示。 */
export type ConversationOpenResult = 'ok' | 'not-found' | 'error' | 'loaded'
export type SendResult = 'sent' | 'error' | 'not-found' | 'idle'

/** 本地消息 id 自增计数器。 */
let messageSequence = 1

/** 生成本地唯一消息 id。 */
function nextMessageId(): number {
  const value = messageSequence
  messageSequence += 1
  return value
}

/** 把服务端历史消息转换成内存消息视图。
 *
 *  - assistant 消息：填充回答级结构化信息（引用与完整性标记），它们来自服务端
 *    持久化结果，与实时响应同形；citations 为空数组时也填
 *    `{ citations: [], answerIncomplete: false }`，便于渲染层统一判断；
 *    老后端/升级前的响应缺这两个字段时按空值兜底（不报错、不渲染卡片）；
 *  - user 消息：structuredAnswer 恒为 null（用户消息没有回答级信息）；
 *  - 两类消息的 structured 均恒为 null：历史不保存 events / 审批，禁止伪造。
 */
function historyToView(history: ConversationHistoryResponse): ChatMessageView[] {
  return history.messages.map((message) => ({
    id: nextMessageId(),
    role: message.role,
    content: message.content,
    createdAt: message.created_at,
    source: 'history' as const,
    sendState: null,
    errorMessage: null,
    structured: null,
    structuredAnswer:
      message.role === 'assistant'
        ? {
            citations: message.citations ?? [],
            answerIncomplete: message.answer_incomplete ?? false,
          }
        : null,
  }))
}

/** 判断错误是否为 ApiError。 */
function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

export const useChatStore = defineStore('chat', () => {
  // —— 状态 ——

  /** 数据所属企业 id：与 organization store 同步捕获，用于迟到响应校验 */
  const organizationId = ref<string | null>(null)
  /** 会话列表（当前企业、当前用户自己的会话，更新时间倒序） */
  const conversations = ref<ConversationListItem[]>([])
  /** 会话列表加载中 */
  const listLoading = ref(false)
  /** 会话列表是否成功加载过（成功后避免重复请求） */
  const conversationsLoaded = ref(false)
  /** 会话列表加载错误消息；null 表示无错误 */
  const listError = ref<string | null>(null)
  /** 会话列表是否还可能存在下一页（按最近一页是否拉满推断） */
  const listHasMore = ref(false)
  /** 当前会话 id；null 表示未选择会话 */
  const currentConversationId = ref<string | null>(null)
  /** 当前会话消息（历史 + 本页新往返的混合视图） */
  const messages = ref<ChatMessageView[]>([])
  /** 历史消息加载中 */
  const historyLoading = ref(false)
  /** 历史加载错误消息；null 表示无错误 */
  const historyError = ref<string | null>(null)
  /** 已完成历史加载的会话 id（避免同一会话重复请求） */
  const historyLoadedConversationId = ref<string | null>(null)
  /** 当前会话的附加系统提示词；未设置或未加载为 null */
  const systemPrompt = ref<string | null>(null)
  /** 消息发送中（防止重复提交） */
  const sending = ref(false)
  /** 可展示错误：发送/系统提示词等瞬时操作的最新错误消息 */
  const displayError = ref<string | null>(null)
  /** 新建会话请求进行中（防止重复提交） */
  const creating = ref(false)
  /** 系统提示词更新进行中（防止重复提交） */
  const promptUpdating = ref(false)
  /** 一次性提示消息（如「会话不存在或已不可访问」），视图展示后清除 */
  const notice = ref<string | null>(null)
  /** 请求失效标记：企业切换/退出/切换会话时递增，旧响应据此丢弃 */
  const requestEpoch = ref(0)

  // —— 派生状态 ——

  /** 当前会话对象（从列表查找；列表刷新前可能为 null，展示用列表标题兜底） */
  const currentConversation = computed(
    () =>
      conversations.value.find((item) => item.conversation_id === currentConversationId.value) ??
      null,
  )
  /** 当前会话标题；无列表对象时显示「会话」兜底 */
  const currentConversationTitle = computed(
    () => currentConversation.value?.title ?? '会话',
  )

  // —— 内部工具 ——

  /** 当前组织 store 快照：读取此刻的企业 id（无企业时为 null）。 */
  function captureOrganizationId(): string | null {
    const organizationStore = useOrganizationStore()
    return organizationStore.currentOrganizationId
  }

  /** 校验异步请求是否仍然有效：企业未切换、未退出、请求期未被标记失效。 */
  function isRequestCurrent(epoch: number, capturedOrganizationId: string | null): boolean {
    return (
      requestEpoch.value === epoch &&
      organizationId.value !== null &&
      organizationId.value === capturedOrganizationId &&
      captureOrganizationId() === capturedOrganizationId
    )
  }

  /** 根据 404 语义清理当前会话（会话不存在或已不可访问）。 */
  function handleConversationGone(): void {
    currentConversationId.value = null
    messages.value = []
    historyLoadedConversationId.value = null
    systemPrompt.value = null
    historyError.value = null
    sending.value = false
    displayError.value = null
    notice.value = '会话不存在或已不可访问'
  }

  /** 强制刷新会话列表（不进入加载态、失败静默，供创建/发送后更新排序与标题）。 */
  async function refreshConversationListSilently(): Promise<void> {
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) return
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    try {
      const items = await chatApi.listConversations({ limit: CONVERSATION_PAGE_SIZE, offset: 0 })
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return
      conversations.value = items
      listHasMore.value = items.length >= CONVERSATION_PAGE_SIZE
    } catch {
      // 静默刷新失败不影响聊天主流程；下次进入页面会重新加载
    }
  }

  // —— 对外动作 ——

  /** 切换企业/退出登录时的租户缓存清理：全部聊天状态归零并失效旧请求。 */
  function resetForTenantChange(): void {
    requestEpoch.value += 1
    organizationId.value = null
    conversations.value = []
    conversationsLoaded.value = false
    listLoading.value = false
    listError.value = null
    listHasMore.value = false
    currentConversationId.value = null
    messages.value = []
    historyLoading.value = false
    historyError.value = null
    historyLoadedConversationId.value = null
    systemPrompt.value = null
    sending.value = false
    displayError.value = null
    creating.value = false
    promptUpdating.value = false
    notice.value = null
  }

  // 注册到租户重置机制：企业切换或退出登录时自动清理聊天状态
  registerTenantResetHandler(resetForTenantChange)

  /** 加载会话列表（force=true 时忽略已加载标记强制刷新）。 */
  async function loadConversations(force = false): Promise<void> {
    if (conversationsLoaded.value && !force) return
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) {
      listError.value = '请先选择企业'
      return
    }
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    listLoading.value = true
    listError.value = null
    try {
      const items = await chatApi.listConversations({ limit: CONVERSATION_PAGE_SIZE, offset: 0 })
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return
      conversations.value = items
      conversationsLoaded.value = true
      listHasMore.value = items.length >= CONVERSATION_PAGE_SIZE
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return
      conversationsLoaded.value = false
      listError.value = error instanceof Error ? error.message : '会话列表加载失败'
    } finally {
      if (requestEpoch.value === epoch) {
        listLoading.value = false
      }
    }
  }

  /** 加载更多会话（追加到列表尾部，offset 取当前已加载条数）。 */
  async function loadMoreConversations(): Promise<void> {
    if (listLoading.value || !listHasMore.value) return
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) return
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    listLoading.value = true
    try {
      const items = await chatApi.listConversations({
        limit: CONVERSATION_PAGE_SIZE,
        offset: conversations.value.length,
      })
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return
      // 合并去重：避免极端情况下同一会话出现在两页
      const known = new Set(conversations.value.map((item) => item.conversation_id))
      const fresh = items.filter((item) => !known.has(item.conversation_id))
      conversations.value = [...conversations.value, ...fresh]
      listHasMore.value = items.length >= CONVERSATION_PAGE_SIZE
    } catch (error) {
      if (isRequestCurrent(epoch, capturedOrganizationId)) {
        listError.value = error instanceof Error ? error.message : '加载更多会话失败'
      }
    } finally {
      if (requestEpoch.value === epoch) {
        listLoading.value = false
      }
    }
  }

  /** 确保会话列表加载过一次（进入聊天页时调用，失败不吞错，由页面展示）。 */
  async function ensureConversationsLoaded(): Promise<void> {
    if (!conversationsLoaded.value) {
      await loadConversations()
    }
  }

  /** 新建会话：可选附带系统提示词；成功后刷新列表并选中新会话，返回会话 id。 */
  async function createConversation(systemPromptInput?: string): Promise<string | null> {
    if (creating.value) return null // 防重复提交兜底
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) {
      displayError.value = '请先选择企业'
      return null
    }
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    creating.value = true
    displayError.value = null
    try {
      const prompt = systemPromptInput?.trim()
      const created = await chatApi.createConversation({
        system_prompt: prompt ? prompt : null,
      })
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return null
      // 清空当前会话状态：新会话没有历史
      currentConversationId.value = created.conversation_id
      messages.value = []
      historyLoadedConversationId.value = created.conversation_id
      historyError.value = null
      systemPrompt.value = prompt ? prompt : null
      // 刷新列表让新会话出现在列表（标题「新会话」，排序在顶部）
      await refreshConversationListSilently()
      return created.conversation_id
    } catch (error) {
      if (isRequestCurrent(epoch, capturedOrganizationId)) {
        displayError.value = error instanceof Error ? error.message : '创建会话失败'
      }
      return null
    } finally {
      if (requestEpoch.value === epoch) {
        creating.value = false
      }
    }
  }

  /** 切换/进入某个会话：加载服务端历史；404 时清理选择并给出提示。 */
  async function openConversation(
    conversationId: string,
  ): Promise<ConversationOpenResult> {
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) return 'error'
    if (
      currentConversationId.value === conversationId &&
      historyLoadedConversationId.value === conversationId
    ) {
      return 'loaded' // 已加载过该会话历史，避免重复请求
    }
    // 切换会话：使该会话之前的在途请求失效
    requestEpoch.value += 1
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    currentConversationId.value = conversationId
    messages.value = []
    historyError.value = null
    historyLoadedConversationId.value = null
    systemPrompt.value = null
    sending.value = false
    displayError.value = null
    historyLoading.value = true
    try {
      const history = await chatApi.getConversationHistory(conversationId)
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'ok' // 过期响应丢弃
      messages.value = historyToView(history)
      historyLoadedConversationId.value = conversationId
      systemPrompt.value = history.system_prompt
      return 'ok'
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'ok'
      if (isApiError(error) && error.status === 404) {
        // 会话不存在或已无权访问：清理失效选择 + 刷新列表 + 提示
        handleConversationGone()
        void refreshConversationListSilently()
        return 'not-found'
      }
      historyError.value = error instanceof Error ? error.message : '历史消息加载失败'
      return 'error'
    } finally {
      if (requestEpoch.value === epoch) {
        historyLoading.value = false
      }
    }
  }

  /** 打开空会话状态（进入 /app/chat 无会话参数时调用）：清空当前会话选择。 */
  function showConversationList(): void {
    if (currentConversationId.value === null) return
    requestEpoch.value += 1
    currentConversationId.value = null
    messages.value = []
    historyLoadedConversationId.value = null
    historyError.value = null
    systemPrompt.value = null
    sending.value = false
    displayError.value = null
    historyLoading.value = false
  }

  /** 发送一条消息：立即展示用户问题与「处理中」占位，完成后填充结果。 */
  async function sendMessage(questionText: string): Promise<SendResult> {
    const trimmed = questionText.trim()
    if (!trimmed) return 'idle'
    if (sending.value) return 'idle' // 防重复提交兜底
    const capturedOrganizationId = captureOrganizationId()
    const conversationId = currentConversationId.value
    if (!capturedOrganizationId || !conversationId) return 'idle'
    if (historyLoadedConversationId.value !== conversationId) {
      // 历史尚未加载完成时不允许发送（避免覆盖服务端顺序）
      displayError.value = '历史消息加载中，请稍候再发送'
      return 'idle'
    }
    const epoch = requestEpoch.value
    organizationId.value = capturedOrganizationId
    sending.value = true
    displayError.value = null

    const userMessage: ChatMessageView = {
      id: nextMessageId(),
      role: 'user',
      content: trimmed,
      createdAt: new Date().toISOString(),
      source: 'live',
      sendState: 'ok',
      errorMessage: null,
      structured: null,
      // 用户消息没有回答级结构化信息，恒为 null
      structuredAnswer: null,
    }
    const pendingAssistantId = nextMessageId()
    const assistantMessage: ChatMessageView = {
      id: pendingAssistantId,
      role: 'assistant',
      content: '',
      createdAt: new Date().toISOString(),
      source: 'live',
      sendState: 'sending',
      errorMessage: null,
      structured: null,
      // 发送中还没有结果：与 structured 一致保持 null（失败时同样保持 null）
      structuredAnswer: null,
    }
    messages.value.push(userMessage, assistantMessage)

    try {
      const response = await chatApi.sendChatMessage(conversationId, trimmed)
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'sent' // 迟到响应丢弃
      const target = messages.value.find((message) => message.id === pendingAssistantId)
      if (target) {
        target.content = response.llm_answer ?? ''
        target.sendState = 'ok'
        target.errorMessage = null
        target.structured = response
        // 回答级结构化信息：引用与完整性标记直接取自本轮响应（与历史同一读取口径）
        target.structuredAnswer = {
          citations: response.citations,
          answerIncomplete: response.answer_incomplete,
        }
      }
      // 本页收到了结构化结果：随后静默刷新列表以更新标题/排序（不重新加载历史，
      // 避免把内存中的结构化信息替换为纯文本，历史恢复由刷新页面触发）
      void refreshConversationListSilently()
      return 'sent'
    } catch (error) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return 'sent' // 旧请求失败不污染
      const target = messages.value.find((message) => message.id === pendingAssistantId)
      if (isApiError(error) && error.status === 404) {
        // 会话在发送期间被删除/失去权限
        handleConversationGone()
        void refreshConversationListSilently()
        return 'not-found'
      }
      const uncertain =
        isApiError(error) && error.status === 0 // 网络中断/超时：结果状态不确定
      const message = uncertain
        ? '网络中断或请求超时，结果状态可能不确定，请先刷新历史再决定是否重新发送'
        : error instanceof Error
          ? error.message
          : '消息发送失败，请稍后重试'
      if (target) {
        target.sendState = 'error'
        target.errorMessage = message
      } else {
        displayError.value = message
      }
      return 'error'
    } finally {
      if (requestEpoch.value === epoch) {
        sending.value = false
      }
    }
  }

  /** 更新当前会话的系统提示词（空白输入不提交；调用方负责 UI 反馈）。 */
  async function updateSystemPrompt(promptText: string): Promise<boolean> {
    const trimmed = promptText.trim()
    if (!trimmed) {
      displayError.value = '系统提示词不能为空'
      return false
    }
    if (promptUpdating.value) return false // 防重复提交
    const capturedOrganizationId = captureOrganizationId()
    const conversationId = currentConversationId.value
    if (!capturedOrganizationId || !conversationId) return false
    const epoch = requestEpoch.value
    promptUpdating.value = true
    displayError.value = null
    try {
      await chatApi.updateSystemPrompt(conversationId, { system_prompt: trimmed })
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return false
      systemPrompt.value = trimmed
      return true
    } catch (error) {
      if (isRequestCurrent(epoch, capturedOrganizationId)) {
        if (isApiError(error) && error.status === 404) {
          handleConversationGone()
          void refreshConversationListSilently()
        } else {
          displayError.value = error instanceof Error ? error.message : '系统提示词更新失败'
        }
      }
      return false
    } finally {
      if (requestEpoch.value === epoch) {
        promptUpdating.value = false
      }
    }
  }

  /** 视图展示完一次性提示后调用（如「会话不存在」提示条关闭）。 */
  function clearNotice(): void {
    notice.value = null
  }

  /** 测试与调试用：整体复位（含失效标记递增）。 */
  function reset(): void {
    resetForTenantChange()
  }

  return {
    // 状态
    organizationId,
    conversations,
    listLoading,
    conversationsLoaded,
    listError,
    listHasMore,
    currentConversationId,
    messages,
    historyLoading,
    historyError,
    historyLoadedConversationId,
    systemPrompt,
    sending,
    displayError,
    creating,
    promptUpdating,
    notice,
    // 派生
    currentConversation,
    currentConversationTitle,
    // 动作
    loadConversations,
    loadMoreConversations,
    ensureConversationsLoaded,
    createConversation,
    openConversation,
    showConversationList,
    sendMessage,
    updateSystemPrompt,
    clearNotice,
    reset,
  }
})
