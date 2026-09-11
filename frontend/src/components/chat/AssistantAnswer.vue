<script setup lang="ts">
/*
 * Agent 最终回答展示组件。
 *
 * 事实与安全说明：
 *  - 回答正文按 Markdown 渲染（标题/列表/加粗/代码块/表格等），
 *    源码内联 HTML 被禁用、链接协议被校验、图片降级为文本，
 *    渲染细节与安全约束统一由 utils/markdown 负责；
 *  - 正文中的 [C1] 样式标记渲染为可点击标记，点击可滚动定位到引用卡片；
 *  - 默认不展示 llm_reasoning_content（模型内部推理不作为普通客服内容）；
 *  - 空回答给出降级提示；answer_incomplete=true 时给出谨慎采纳警告。
 */
import { computed } from 'vue'
import { NAlert } from 'naive-ui'

import MarkdownContent from '@/components/common/MarkdownContent.vue'

const props = defineProps<{
  /** Agent 最终自然语言回答（Markdown 文本）；null/空串时展示降级提示 */
  content: string | null
  /** 证据引用是否可能不完整（answer_incomplete） */
  answerIncomplete?: boolean
  /** 是否启用正文引用标记（仅当该回答携带结构化 citations 时启用） */
  citationLinks?: boolean
}>()

/** 回答是否为空（null/空串/全空白）。 */
const isEmptyAnswer = computed(() => !props.content || props.content.trim().length === 0)
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
    <!-- Markdown 渲染：HTML 由 markdown-it 生成（已禁用源码 HTML 并校验链接） -->
    <MarkdownContent
      v-else
      class="answer-text"
      :content="content ?? ''"
      :citations="citationLinks === true"
    />
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
</style>
