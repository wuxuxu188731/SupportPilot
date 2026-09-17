<script setup lang="ts">
/*
 * Markdown 内容展示组件：把 Agent 消息的 Markdown 文本渲染为排版好的富文本。
 *
 * 安全说明：
 *  - HTML 由 utils/markdown 的 markdown-it 实例生成，源码内联 HTML 已禁用、
 *    链接协议已校验、图片已降级为文本，因此可安全使用 v-html；
 *  - 引用标记 [C1] 由 markdown-it 行内规则生成，点击/回车通过本组件容器上的
 *    事件委托处理，定位到对应的引用卡片（CitationList 中的
 *    `${citationAnchorPrefix}-citation-C1`，前缀为空时退化为 citation-C1）。
 *    前缀即「回答作用域」：同一页面多条回答都含 C1 时，必须靠前缀各自归位。
 */
import { computed } from 'vue'

import { renderMarkdown } from '@/utils/markdown'

const props = defineProps<{
  /** Markdown 源文本（Agent 回答正文） */
  content: string
  /** 是否把正文中的 [C1] 渲染为可点击引用标记（仅该回答携带结构化 citations 时启用） */
  citations?: boolean
  /**
   * 引用卡片 id 的作用域前缀，必须与宿主传给 CitationList 的 anchorPrefix 一致；
   * 空串/不传表示不加前缀（退化为 citation-C1，兼容既有用法）。
   */
  citationAnchorPrefix?: string
}>()

/** 渲染后的 HTML；空文本返回空串，由父组件决定降级文案。 */
const html = computed(() => renderMarkdown(props.content, { citations: props.citations === true }))

/** 引用卡片 id：与 CitationList 的规则保持一致（带前缀时按回答作用域拼接）。 */
function citationCardId(citationId: string): string {
  const prefix = props.citationAnchorPrefix ?? ''
  return prefix ? `${prefix}-citation-${citationId}` : `citation-${citationId}`
}

/** 引用标记点击/回车：滚动并聚焦到对应引用卡片（无对应卡片时忽略）。 */
function handleCitationActivate(event: Event): void {
  const target = (event.target as HTMLElement | null)?.closest('.citation-ref')
  if (!(target instanceof HTMLElement)) return
  const citationId = target.dataset.citationId
  if (!citationId) return
  const card = document.getElementById(citationCardId(citationId))
  card?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  card?.focus({ preventScroll: true })
}
</script>

<template>
  <!-- vue/no-v-html 在本组件内关闭：HTML 由 utils/markdown 的 markdown-it 安全配置生成 -->
  <!-- eslint-disable vue/no-v-html -->
  <div
    class="markdown-body"
    data-test="markdown-body"
    @click="handleCitationActivate"
    @keydown.enter="handleCitationActivate"
    v-html="html"
  />
</template>

<style scoped>
/*
 * Markdown 排版：样式作用于 v-html 生成的子节点，因此必须使用 :deep()。
 * 视觉基调与消息气泡保持一致（正文 14px / 行高 1.7，标题层级紧凑）。
 */
.markdown-body {
  color: var(--sp-color-text-1);
  font-size: var(--sp-font-size-base);
  line-height: 1.7;
  word-break: break-word;
  overflow-wrap: anywhere;
}

/* 首尾块级元素去掉外边距，避免气泡内出现多余留白 */
.markdown-body :deep(> *:first-child) {
  margin-top: 0;
}

.markdown-body :deep(> *:last-child) {
  margin-bottom: 0;
}

.markdown-body :deep(p) {
  margin: 0 0 var(--sp-space-3);
}

.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4),
.markdown-body :deep(h5),
.markdown-body :deep(h6) {
  margin: var(--sp-space-4) 0 var(--sp-space-2);
  font-weight: 600;
  line-height: 1.4;
}

.markdown-body :deep(h1) {
  font-size: var(--sp-font-size-xl);
}

.markdown-body :deep(h2) {
  font-size: var(--sp-font-size-lg);
}

.markdown-body :deep(h3),
.markdown-body :deep(h4),
.markdown-body :deep(h5),
.markdown-body :deep(h6) {
  font-size: var(--sp-font-size-base);
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 0 0 var(--sp-space-3);
  padding-left: var(--sp-space-5);
}

.markdown-body :deep(li) {
  margin: var(--sp-space-1) 0;
}

/* 嵌套列表不额外撑开间距，保持层级紧凑 */
.markdown-body :deep(li > ul),
.markdown-body :deep(li > ol) {
  margin-bottom: 0;
}

.markdown-body :deep(blockquote) {
  margin: 0 0 var(--sp-space-3);
  padding: var(--sp-space-2) var(--sp-space-3);
  border-left: 3px solid var(--sp-color-primary);
  border-radius: 0 var(--sp-radius-sm) var(--sp-radius-sm) 0;
  background: var(--sp-color-bg-hover);
  color: var(--sp-color-text-2);
}

.markdown-body :deep(code) {
  font-family: var(--sp-font-family-mono);
  font-size: 0.9em;
}

/* 行内代码：弱底色 + 主色文字，避免与正文混淆 */
.markdown-body :deep(p code),
.markdown-body :deep(li code),
.markdown-body :deep(td code),
.markdown-body :deep(blockquote code) {
  padding: 1px 5px;
  border-radius: var(--sp-radius-sm);
  background: var(--sp-color-bg-hover);
  color: var(--sp-color-primary);
}

.markdown-body :deep(pre) {
  margin: 0 0 var(--sp-space-3);
  padding: var(--sp-space-3);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-md);
  background: var(--sp-color-bg-hover);
  overflow-x: auto;
  line-height: 1.6;
}

.markdown-body :deep(pre code) {
  padding: 0;
  background: transparent;
  color: var(--sp-color-text-1);
}

.markdown-body :deep(a) {
  color: var(--sp-color-primary);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.markdown-body :deep(hr) {
  margin: var(--sp-space-4) 0;
  border: none;
  border-top: 1px solid var(--sp-color-border);
}

.markdown-body :deep(table) {
  margin: 0 0 var(--sp-space-3);
  border-collapse: collapse;
  width: 100%;
  font-size: var(--sp-font-size-sm);
  display: block;
  overflow-x: auto;
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  padding: var(--sp-space-2) var(--sp-space-3);
  border: 1px solid var(--sp-color-border);
  text-align: left;
}

.markdown-body :deep(th) {
  background: var(--sp-color-bg-hover);
  font-weight: 600;
}

/* 引用标记：与引用卡片联动，可聚焦、可点击 */
.markdown-body :deep(.citation-ref) {
  display: inline-block;
  padding: 0 2px;
  border-radius: var(--sp-radius-sm);
  background: var(--sp-color-primary-weak);
  color: var(--sp-color-primary);
  font-size: 0.85em;
  font-weight: 600;
  cursor: pointer;
}

.markdown-body :deep(.citation-ref:focus-visible) {
  outline: 2px solid var(--sp-color-primary);
  outline-offset: 1px;
}
</style>
