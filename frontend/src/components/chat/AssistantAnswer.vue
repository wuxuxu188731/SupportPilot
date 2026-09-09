<script setup lang="ts">
/*
 * Agent 最终回答展示组件。
 *
 * 事实与安全说明：
 *  - 纯文本渲染（white-space: pre-line 保留换行），不使用 v-html；
 *  - 正文中的 [C1] 样式标记按安全分段高亮，点击可滚动定位到引用卡片；
 *  - 默认不展示 llm_reasoning_content（模型内部推理不作为普通客服内容）；
 *  - 空回答给出降级提示；answer_incomplete=true 时给出谨慎采纳警告。
 */
import { computed } from 'vue'
import { NAlert } from 'naive-ui'

const props = defineProps<{
  /** Agent 最终自然语言回答；null/空串时展示降级提示 */
  content: string | null
  /** 证据引用是否可能不完整（answer_incomplete） */
  answerIncomplete?: boolean
  /** 是否启用正文引用标记分段（仅当该回答携带结构化 citations 时启用） */
  citationLinks?: boolean
}>()

/** 回答是否为空（null/空串/全空白）。 */
const isEmptyAnswer = computed(() => !props.content || props.content.trim().length === 0)

/** 安全文本分段结果：普通文本段 + 引用标记段（[C1] 等）。 */
interface AnswerSegment {
  /** 是否为引用标记段 */
  isCitation: boolean
  /** 分段文本（引用标记段为完整的 [C1] 标记文本） */
  text: string
  /** 引用标记对应的 citation_id（仅 isCitation 为 true） */
  citationId?: string
}

const CITATION_PATTERN = /(\[[Cc]\d+\])/g

/** 按引用标记切分回答正文（纯文本分段，不解析正文内容）。 */
const segments = computed<AnswerSegment[]>(() => {
  if (!props.citationLinks) return [{ isCitation: false, text: props.content ?? '' }]
  const answer = props.content ?? ''
  const parts = answer.split(CITATION_PATTERN)
  return parts
    .filter((part) => part.length > 0)
    .map((part) => {
      const match = /^\[([Cc])(\d+)\]$/.exec(part)
      if (match) {
        return { isCitation: true, text: part, citationId: `C${match[2]}` }
      }
      return { isCitation: false, text: part }
    })
})

/** 点击引用标记：滚动/聚焦到对应引用卡片（无卡片时忽略）。 */
function scrollToCitation(citationId: string | undefined): void {
  if (!citationId) return
  const target = document.getElementById(`citation-${citationId}`)
  target?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  target?.focus({ preventScroll: true })
}
</script>

<template>
  <div class="assistant-answer">
    <n-alert
      v-if="answerIncomplete"
      type="warning"
      :show-icon="true"
      class="incomplete-warning"
      title="引用完整性提示"
    >
      当前回答的证据引用可能不完整，请谨慎采用。
    </n-alert>

    <p v-if="isEmptyAnswer" class="empty-answer">（本次未生成可展示的回答文本）</p>
    <!-- 纯文本分段渲染：保留换行与基本结构，禁止 v-html -->
    <p v-else class="answer-text">
      <template v-for="(segment, index) in segments" :key="`${segment.text}-${index}`">
        <span
          v-if="segment.isCitation"
          class="citation-ref"
          tabindex="0"
          role="link"
          :aria-label="`引用 ${segment.citationId}，点击定位到引用卡片`"
          @click="scrollToCitation(segment.citationId)"
          @keydown.enter="scrollToCitation(segment.citationId)"
        >
          [{{ segment.citationId }}]
        </span>
        <template v-else>{{ segment.text }}</template>
      </template>
    </p>
  </div>
</template>

<style scoped>
.assistant-answer {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-3);
}

.incomplete-warning {
  margin: 0;
}

.empty-answer {
  margin: 0;
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.answer-text {
  margin: 0;
  white-space: pre-line;
  word-break: break-word;
  color: var(--sp-color-text-1);
  line-height: 1.7;
}

.citation-ref {
  display: inline-block;
  padding: 0 2px;
  border-radius: var(--sp-radius-sm);
  background: var(--sp-color-primary-weak);
  color: var(--sp-color-primary);
  font-size: 0.85em;
  font-weight: 600;
  cursor: pointer;
}

.citation-ref:focus-visible {
  outline: 2px solid var(--sp-color-primary);
  outline-offset: 1px;
}
</style>
