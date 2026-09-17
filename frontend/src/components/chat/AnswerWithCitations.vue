<script setup lang="ts">
/*
 * 「回答正文 + 引用卡片」展示片段：实时回答与历史回答共用。
 *
 * 为什么抽出来：刷新前（实时响应）与刷新后（服务端持久化结果）的回答，
 * 在正文与引用这两部分必须完全一致——同一套引用渲染（标签/标题/章节/
 * 证据原文/查看入口）与同一套锚点规则；两个分支各写一遍迟早会漂移。
 *
 * 参数口径（实时与历史的差异由宿主决定，本组件不区分来源）：
 *  - content / citations / answerIncomplete：实时取本轮响应，历史取服务端持久化结果；
 *  - anchorPrefix：回答作用域前缀（如 answer-12），同时用于引用卡片 id 与正文
 *    [C1] 的定位，保证同一页面多条回答各自归位；
 *  - 默认插槽：宿主专有内容（如实时回答的检索摘要）插在回答与引用卡片之间。
 *
 * 安全边界：引用只来自结构化 citations 数组，禁止从回答文本解析；
 * 历史回答不渲染处理过程与审批卡片（那部分由宿主按 structured 是否存在决定）。
 */
import type { Citation, CitationTarget } from '@/api/types'
import AssistantAnswer from '@/components/chat/AssistantAnswer.vue'
import CitationList from '@/components/chat/CitationList.vue'

defineProps<{
  /** 回答正文（Markdown 文本）：实时取 llm_answer，历史取消息正文；null/空串走降级提示 */
  content: string | null
  /** 该回答的结构化引用；为空数组时不渲染引用卡片，正文 [C1] 也不作为链接 */
  citations: Citation[]
  /** 该回答的引用完整性标记（answer_incomplete），true 时显示「谨慎采用」提示 */
  answerIncomplete: boolean
  /** 回答锚点前缀（回答作用域）：引用卡片 id 与正文 [C1] 定位共用该前缀 */
  anchorPrefix: string
}>()

const emit = defineEmits<{
  /** 引用卡片「查看原文位置」：原样上抛结构化定位信息给宿主 */
  openDocument: [payload: CitationTarget]
}>()
</script>

<template>
  <div class="answer-with-citations">
    <AssistantAnswer
      :content="content"
      :answer-incomplete="answerIncomplete"
      :citation-links="citations.length > 0"
      :citation-anchor-prefix="anchorPrefix"
    />
    <!-- 宿主专有内容（如实时回答的检索摘要）：插在回答与引用卡片之间 -->
    <slot />
    <CitationList
      v-if="citations.length > 0"
      :citations="citations"
      :anchor-prefix="anchorPrefix"
      @open-document="(payload) => emit('openDocument', payload)"
    />
  </div>
</template>
