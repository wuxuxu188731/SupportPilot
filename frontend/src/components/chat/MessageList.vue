<script setup lang="ts">
/*
 * 消息列表组件：渲染当前会话的历史与实时消息。
 *
 * 展示规则：
 *  - 用户消息：右侧气泡，纯文本展示（用户输入按原文回显）；
 *  - Agent 历史消息（source=history）：左侧气泡，正文按 Markdown 渲染，并展示
 *    **服务端持久化**的回答级结构化信息——引用卡片（citations）与完整性提示
 *    （answer_incomplete），两者与实时回答同形；历史仍不保存 events /
 *    retrieval_summary / pending_approvals，因此**不会**伪造处理过程或审批卡片；
 *  - Agent 实时消息（source=live）：先显示「处理中」占位，成功后展示
 *    回答与结构化内容（引用、检索摘要、处理过程、待审批提案）；
 *  - 引用锚点按回答作用域（`answer-${message.id}`）：同一页面多条带引用的回答
 *    都从 C1 开始编号，只有给卡片 id 加回答前缀，正文 [C1] 才能定位到自己那张卡片；
 *  - 发送失败显示失败原因；网络/超时提示结果不确定，引导刷新历史；
 *  - 默认不展示 llm_reasoning_content（模型推理不作为普通内容展示）。
 *
 * 滚动策略：新消息到达且用户本就在底部附近时自动滚到底；用户主动
 * 向上阅读历史时不强行抢占滚动位置。
 */
import { nextTick, ref, watch } from 'vue'
import { NSpin, NEmpty } from 'naive-ui'

import type { ChatMessageView } from '@/stores/chat'
import type { CitationTarget, RetrievalSummary } from '@/api/types'
import { formatMessageTime } from '@/utils/time'
import AgentEventTimeline from '@/components/chat/AgentEventTimeline.vue'
import AnswerWithCitations from '@/components/chat/AnswerWithCitations.vue'
import PendingApprovalCard from '@/components/chat/PendingApprovalCard.vue'

const props = defineProps<{
  /** 当前会话消息（历史 + 实时混合） */
  messages: ChatMessageView[]
  /** 历史消息是否仍在加载中 */
  historyLoading: boolean
  /** 当前会话是否完全为空（无历史消息且无本页新消息） */
  emptyConversation: boolean
}>()

const emit = defineEmits<{
  /** 待审批卡片「查看审批」：上抛给 ChatView 进行路由跳转 */
  'open-approval': [approvalId: string]
  /** 引用卡片「查看原文位置」：上抛结构化定位信息给 ChatView */
  'open-document': [payload: CitationTarget]
}>()

/** 消息滚动容器。 */
const scroller = ref<HTMLElement | null>(null)

/** 是否接近底部（差 120px 内视为在底部附近）。 */
function isNearBottom(): boolean {
  const element = scroller.value
  if (!element) return true
  return element.scrollHeight - element.scrollTop - element.clientHeight < 120
}

/** 滚动到底部（新消息或主动发送后使用）。 */
function scrollToBottom(): void {
  const element = scroller.value
  if (element) {
    element.scrollTop = element.scrollHeight
  }
}

/** 记录上一个消息数量：数量增长时评估是否需要自动滚动。 */
let previousLength = 0

watch(
  () => props.messages.length,
  async (length) => {
    if (length > previousLength) {
      const newest = props.messages[length - 1]
      // 自己刚发送的消息要滚动；新增 agent 占位/完成时若在底部也滚动
      const force = newest?.role === 'user'
      await nextTick()
      if (force || isNearBottom()) {
        scrollToBottom()
      }
    }
    previousLength = length
  },
)

// 历史加载完成后若在底部则滚到底（例如进入会话先看到最新消息）
watch(
  () => props.historyLoading,
  async (loading, was) => {
    if (was && !loading) {
      await nextTick()
      scrollToBottom()
    }
  },
)

/** 检索摘要的紧凑展示文本。 */
function retrievalSummaryText(summary: RetrievalSummary): string {
  return `检索：${summary.strategy} · ${summary.round_count} 轮 · 证据 ${summary.evidence_status} · ${summary.latency_ms} ms`
}

/** 消息时间展示文本。 */
function messageTimeText(createdAt: string): string {
  return formatMessageTime(createdAt)
}

/** 回答锚点前缀：用消息自身的稳定标识，保证同页不同回答的引用卡片 id 不冲突（设计 5.4）。 */
function answerAnchorPrefix(message: ChatMessageView): string {
  return `answer-${message.id}`
}
</script>

<template>
  <div ref="scroller" class="message-scroller" data-test="message-scroller">
    <!-- 历史加载中 -->
    <div v-if="historyLoading" class="message-center-state" aria-live="polite">
      <n-spin size="small" />
      <span>正在加载历史消息…</span>
    </div>

    <!-- 空会话欢迎态（尚未发送任何消息） -->
    <div v-else-if="emptyConversation" class="message-center-state">
      <n-empty description="这是一个新会话" size="small">
        <template #extra>
          <p class="welcome-hint">输入下方问题开始对话；回答会展示知识引用与处理过程。</p>
        </template>
      </n-empty>
    </div>

    <!-- 消息流 -->
    <ol v-else class="message-list">
      <li
        v-for="message in messages"
        :key="message.id"
        class="message-row"
        :class="message.role === 'user' ? 'from-user' : 'from-agent'"
        :data-message-role="message.role"
      >
        <div class="bubble-meta">
          <span class="bubble-author">{{ message.role === 'user' ? '我' : 'Agent' }}</span>
          <span class="bubble-time">{{ messageTimeText(message.createdAt) }}</span>
        </div>

        <!-- 用户消息：纯文本气泡 -->
        <div v-if="message.role === 'user'" class="bubble user-bubble" data-test="user-message">
          <p class="user-text">{{ message.content }}</p>
        </div>

        <!-- Agent 实时占位：处理中 -->
        <div v-else-if="message.source === 'live' && message.sendState === 'sending'" class="bubble agent-bubble sending" aria-live="polite">
          <n-spin size="small" />
          <span>Agent 处理中…</span>
        </div>

        <!-- Agent 实时失败 -->
        <div v-else-if="message.source === 'live' && message.sendState === 'error'" class="bubble agent-bubble failed" role="alert" data-test="agent-failed">
          <p class="failed-title">消息发送失败</p>
          <p v-if="message.errorMessage" class="failed-detail">{{ message.errorMessage }}</p>
        </div>

        <!-- Agent 消息主体：历史（正文 + 持久化引用）/ 实时（结构化展示） -->
        <div v-else class="bubble agent-bubble" data-test="agent-message">
          <template v-if="message.structured">
            <!-- 实时结构化结果：回答 + 引用 + 检索摘要 + 处理过程 + 待审批 -->
            <AnswerWithCitations
              :content="message.structured.llm_answer ?? message.content"
              :citations="message.structured.citations"
              :answer-incomplete="message.structured.answer_incomplete"
              :anchor-prefix="answerAnchorPrefix(message)"
              @open-document="(payload) => emit('open-document', payload)"
            >
              <p v-if="message.structured.retrieval_summary" class="retrieval-summary" data-test="retrieval-summary">
                {{ retrievalSummaryText(message.structured.retrieval_summary) }}
              </p>
            </AnswerWithCitations>
            <AgentEventTimeline v-if="message.structured.events.length > 0" :events="message.structured.events" />
            <div v-if="message.structured.pending_approvals.length > 0" class="pending-approvals" data-test="pending-approvals">
              <p class="pending-heading">待审批提案</p>
              <PendingApprovalCard
                v-for="approval in message.structured.pending_approvals"
                :key="approval.approval_id"
                :approval="approval"
                @open-detail="(approvalId: string) => emit('open-approval', approvalId)"
              />
            </div>
          </template>
          <template v-else>
            <!-- 历史恢复的消息：正文 + 服务端持久化的引用卡片（不伪造处理过程与审批卡片） -->
            <AnswerWithCitations
              class="agent-text"
              :content="message.content"
              :citations="message.structuredAnswer?.citations ?? []"
              :answer-incomplete="message.structuredAnswer?.answerIncomplete ?? false"
              :anchor-prefix="answerAnchorPrefix(message)"
              @open-document="(payload) => emit('open-document', payload)"
            />
          </template>
        </div>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.message-scroller {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--sp-space-4);
}

.message-center-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-space-3);
  min-height: 220px;
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.welcome-hint {
  margin: 0;
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.message-list {
  list-style: none;
  margin: 0 auto;
  padding: 0;
  max-width: 860px;
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-5);
}

.message-row {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--sp-space-1);
}

.message-row.from-user {
  align-items: flex-end;
}

.bubble-meta {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.from-user .bubble-author {
  order: 2;
}

.bubble {
  border-radius: var(--sp-radius-lg);
  padding: var(--sp-space-3) var(--sp-space-4);
  max-width: 86%;
  min-width: 60px;
}

.user-bubble {
  background: var(--sp-color-primary);
  color: #ffffff;
}

.user-text {
  margin: 0;
  white-space: pre-line;
  word-break: break-word;
  line-height: 1.7;
}

.agent-bubble {
  background: var(--sp-color-bg-card);
  border: 1px solid var(--sp-color-border);
  box-shadow: var(--sp-shadow-card);
}

.agent-text {
  /* 历史 Agent 消息的正文+引用排版由 AnswerWithCitations 组件负责，这里仅限制宽度基准 */
  max-width: 100%;
}

.sending {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-space-2);
  color: var(--sp-color-text-2);
  font-size: var(--sp-font-size-sm);
}

.failed-title {
  margin: 0 0 var(--sp-space-1);
  font-weight: 600;
  color: var(--sp-color-error);
}

.failed-detail {
  margin: 0;
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-error);
  word-break: break-word;
}

.retrieval-summary {
  margin: var(--sp-space-3) 0 0;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.pending-approvals {
  margin-top: var(--sp-space-3);
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-3);
}

.pending-heading {
  margin: 0;
  font-weight: 600;
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-warning);
}
</style>
