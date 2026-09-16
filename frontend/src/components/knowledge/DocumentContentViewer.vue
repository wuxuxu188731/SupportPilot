<script setup lang="ts">
/*
 * 知识库正文查看器：按版本加载归一化 Markdown 正文、渲染、目录导航与偏移高亮。
 *
 * 事实与边界（与设计稿 4.6 / 4.7 / 4.8 一致）：
 *  - 正文只从 `GET /knowledge/documents/{id}/versions/{id}/content/` 读取，
 *    渲染的是入库时已转换好的 Markdown（PDF/Word 也已转换），**不渲染原文排版**；
 *  - 本组件是纯展示组件：**不关心自己出现在对话右侧面板还是整页**，
 *    宽度与关闭按钮由外壳（DocumentContentPanel / 知识库详情页）决定；
 *  - 正文渲染复用 utils/markdown 的安全渲染器（html:false、链接协议校验、
 *    图片降级为文本），不新增任何 v-html 直出；
 *  - 同一 (document_id, version_id) 只请求一次；切到同一版本只重新定位不重新请求；
 *  - 偏移为空/越界/对不齐一律降级：能按 heading_path 找到目录项就滚到该章节，
 *    否则滚到顶部并上屏「该引用未记录精确位置，已为你打开来源文档」；
 *  - 失败不影响调用方（对话区仍然保留引用卡片），组件内提供重试。
 */
import { NAlert, NButton, NSpin, NText } from 'naive-ui'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { getDocumentVersionContent, getKnowledgeDocumentDetail } from '@/api/knowledge'
import type { DocumentContentOutlineItem, DocumentContentResponse } from '@/api/knowledgeTypes'
import {
  invalidateCachedContent,
  readCachedActiveVersionNo,
  readCachedContent,
  writeCachedActiveVersionNo,
  writeCachedContent,
} from '@/utils/documentContentCache'
import {
  clearHighlight,
  locateElementByOffset,
  locateRangeByOffsets,
  RANGE_HIGHLIGHT_CLASS,
} from '@/utils/markdownRange'
import { renderMarkdown } from '@/utils/markdown'

const props = defineProps<{
  /** 文档标识（与 versionId 一起决定请求哪一个版本的正文字） */
  documentId: string
  /** 版本标识：偏移相对该版本，因此必须显式指定，不能用「当前有效版本」代替 */
  versionId: string
  /** 引用片段在该版本文正中的起始字符偏移；null/undefined 表示无法精确定位（降级） */
  startOffset?: number | null
  /** 结束字符偏移（不含）；与 startOffset 同生同灭 */
  endOffset?: number | null
  /** 引用的标题路径：偏移不可用时用它回退到所属章节（来自结构化 citations） */
  headingPath?: string | null
}>()

/** 定位结果：exact=精确高亮，fallback=降级，none=无需定位。 */
type LocateMode = 'exact' | 'fallback' | 'none'

const content = ref<DocumentContentResponse | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)

const bodyRef = ref<HTMLElement | null>(null)
const locateMode = ref<LocateMode>('none')
/** 定位失败时的控制台诊断（不上屏，避免暴露内部细节） */
const locateDiagnostic = ref<string | null>(null)

/** 正文 HTML：由安全渲染器生成，空正文返回空串（空态由模板处理）。 */
const renderedHtml = computed(() => renderMarkdown(content.value?.text ?? ''))

/** 是否历史版本（引用版本不是当前有效版本）。 */
const isHistorical = computed(() => {
  const current = content.value
  if (current === null) return false
  return current.active_version_id !== null && current.active_version_id !== current.version_id
})

/** 当前有效版本的序号（历史版本提示需要「当前有效版本 vM」）；未知为 null。 */
const activeVersionNo = ref<number | null>(null)

/**
 * 读取当前有效版本的序号。
 *
 * 为什么需要额外一次请求：正文响应只带本次版本的 version_no，而历史版本提示要求
 * 上屏「当前有效版本 vM」，只能从文档详情的版本列表里取；只在确实是历史版本时才请求，
 * 且按文档缓存结果（同一文档内切换引用不重复请求详情）。
 */
async function loadActiveVersionNo(): Promise<void> {
  const current = content.value
  if (current === null || current.active_version_id === null) return
  const documentId = current.document_id
  const cached = readCachedActiveVersionNo(documentId)
  if (cached !== undefined) {
    activeVersionNo.value = cached
    return
  }
  try {
    const detail = await getKnowledgeDocumentDetail(documentId)
    // 企业切换或文档切换后丢弃迟到结果
    if (content.value?.document_id !== documentId) return
    const active = detail.versions.find(
      (version) => version.version_id === detail.active_version_id,
    )
    activeVersionNo.value = active?.version_no ?? null
    if (active !== undefined) writeCachedActiveVersionNo(documentId, active.version_no)
  } catch {
    // 取不到版本号不影响正文阅读：提示里退回「其它版本」
    activeVersionNo.value = null
  }
}

/** 目录是否可用。 */
const hasOutline = computed(() => (content.value?.outline.length ?? 0) > 0)

/**
 * 加载正文：命中缓存直接使用（同一版本不重复请求）。
 * 并发保护：只有最后一次请求的结果会写回状态。
 */
async function load(force = false): Promise<void> {
  const cached = readCachedContent(props.documentId, props.versionId)
  if (!force && cached !== undefined) {
    content.value = cached
    error.value = null
    loading.value = false
    await applyOffsets()
    await versionNoGuard()
    return
  }

  const documentId = props.documentId
  const versionId = props.versionId
  loading.value = true
  error.value = null
  try {
    const response = await getDocumentVersionContent(documentId, versionId)
    // 请求期间目标已切换：丢弃本次结果，避免把旧版本正文显示在新引用上
    if (props.documentId !== documentId || props.versionId !== versionId) return
    writeCachedContent(response)
    content.value = response
  } catch {
    if (props.documentId !== documentId || props.versionId !== versionId) return
    content.value = null
    error.value = '正文加载失败，可能是网络问题或该版本暂时不可用。'
  } finally {
    // 必须先落 loading=false：正文容器只在非加载态渲染，否则拿不到 bodyRef
    if (props.documentId === documentId && props.versionId === versionId) {
      loading.value = false
    }
  }
  await applyOffsets()
  await versionNoGuard()
}

/** 历史版本时才去补「当前有效版本」的序号。 */
async function versionNoGuard(): Promise<void> {
  activeVersionNo.value = null
  if (isHistorical.value) await loadActiveVersionNo()
}

/** 按当前偏移（或 heading_path）定位并高亮；任何失败都走降级，不抛异常。 */
async function applyOffsets(): Promise<void> {
  locateMode.value = 'none'
  locateDiagnostic.value = null
  await nextTick()
  const container = bodyRef.value
  const current = content.value
  if (container === null || current === null) return

  clearHighlight(container)

  const { startOffset, endOffset } = props
  if (startOffset == null || endOffset == null) {
    // 决策 2：偏移允许为空，此时降级为「只定位到文档」（能按 heading_path 落到章节更好）
    locateMode.value = 'fallback'
    if (scrollToHeadingPath() === null) scrollTop()
    return
  }

  const result = locateRangeByOffsets(container, current.text, startOffset, endOffset)
  if (result.ok) {
    locateMode.value = 'exact'
    scrollToHighlight()
    return
  }

  // 定位失败：留诊断日志（不上屏），并按 heading_path 回退到章节
  locateMode.value = 'fallback'
  locateDiagnostic.value = `偏移 ${startOffset}-${endOffset} 定位失败：${result.reason}`
  console.warn('[DocumentContentViewer] 偏移定位失败', {
    documentId: props.documentId,
    versionId: props.versionId,
    startOffset,
    endOffset,
    reason: result.reason,
  })
  if (scrollToHeadingPath() === null) scrollTop()
}

/** 滚动到高亮区间（一次即可，缓存结果直到切换引用或重新加载）。 */
function scrollToHighlight(): void {
  const container = bodyRef.value
  const highlight = container?.querySelector(`.${RANGE_HIGHLIGHT_CLASS}`)
  if (highlight === null || highlight === undefined) return
  // 滚动只是体验增强：JSdom 等环境未实现 scrollIntoView，缺失时必须静默跳过，
  // 不能因为"滚不动"就让整条正文加载路径变成未处理的 Promise 拒绝
  scrollElementIntoView(highlight, { block: 'center', behavior: 'smooth' })
}

/** 滚动到顶部（偏移不可用时的兜底）。 */
function scrollTop(): void {
  const container = bodyRef.value
  if (container === null) return
  if (typeof container.scrollTo === 'function') {
    container.scrollTo({ top: 0, behavior: 'auto' })
    return
  }
  container.scrollTop = 0
}

/** 按引用的 heading_path 在目录里找到对应标题并滚动过去；找不到返回 null。 */
function scrollToHeadingPath(): Element | null {
  const path = props.headingPath
  if (path == null || path.length === 0) return null
  const item = findOutlineByPath(path)
  if (item === undefined) return null
  scrollToOutlineItem(item)
  return bodyRef.value
}

/** 目录项匹配：优先完整路径，其次按最后一段标题兜底匹配。 */
function findOutlineByPath(path: string): DocumentContentOutlineItem | undefined {
  const outline = content.value?.outline ?? []
  const exact = outline.find((item) => item.heading_path === path)
  if (exact !== undefined) return exact
  const tail = path.split('/').pop()?.trim()
  if (tail === undefined || tail.length === 0) return undefined
  return outline.find((item) => item.title === tail)
}

/** 点击目录项：滚动到对应标题；`locateElementByOffset` 只读 DOM，不产生高亮。 */
function scrollToOutlineItem(item: DocumentContentOutlineItem): void {
  const container = bodyRef.value
  const current = content.value
  if (container === null || current === null) return
  const element = locateElementByOffset(container, current.text, item.char_offset)
  if (element === null) {
    scrollTop()
    return
  }
  scrollElementIntoView(element, { block: 'start', behavior: 'smooth' })
}

/** 元素滚动到视口（环境未实现时静默跳过，见 scrollToHighlight 的说明）。 */
function scrollElementIntoView(element: Element, options: { block: string; behavior: string }): void {
  const scrollable = element as Element & {
    scrollIntoView?: (options?: { block: string; behavior: string }) => void
  }
  if (typeof scrollable.scrollIntoView === 'function') scrollable.scrollIntoView(options)
}

/** 重试：绕过缓存强制重新请求。 */
function retry(): void {
  invalidateCachedContent(props.documentId, props.versionId)
  void load(true)
}

watch(
  () => [props.documentId, props.versionId] as const,
  () => {
    // 切换文档/版本：先清掉旧高亮，避免定位到已卸载的 DOM
    if (bodyRef.value !== null) clearHighlight(bodyRef.value)
    void load()
  },
  { immediate: true },
)

watch(
  () => [props.startOffset, props.endOffset, props.headingPath] as const,
  () => {
    if (content.value !== null) void applyOffsets()
  },
)

onBeforeUnmount(() => {
  if (bodyRef.value !== null) clearHighlight(bodyRef.value)
})
</script>

<template>
  <div class="doc-viewer" data-test="document-content-viewer">
    <!-- 加载态 -->
    <div v-if="loading" class="doc-state" data-test="viewer-loading">
      <n-spin size="small" />
      <span>正在加载正文…</span>
    </div>

    <!-- 错误态：失败不影响调用方，这里提供重试 -->
    <div v-else-if="error" class="doc-state" data-test="viewer-error">
      <n-alert type="error" :show-icon="true" :title="'正文加载失败'">
        <div class="doc-error-body">
          <span>{{ error }}</span>
          <n-button size="small" data-test="viewer-retry" @click="retry">重试</n-button>
        </div>
      </n-alert>
    </div>

    <!-- 空正文 -->
    <div v-else-if="content && content.text.trim().length === 0" class="doc-state" data-test="viewer-empty">
      <n-text depth="3">该版本文档暂无正文内容。</n-text>
    </div>

    <template v-else-if="content">
      <header class="doc-header">
        <h3 class="doc-title" :title="content.title">{{ content.title }}</h3>
        <n-text depth="3" class="doc-meta" data-test="viewer-version">
          版本 v{{ content.version_no }} · 正文 {{ content.text_length }} 字符
        </n-text>
      </header>

      <!-- 必须上屏的提示（设计稿 4.8）：历史版本、停用、无法精确定位。
           来源转换提示（PDF/Word → Markdown）已按要求移除：它只在 word/pdf
           文档上出现，属于用户已知的既有事实，不必每次查看正文都提示。 -->
      <div class="doc-notices">
        <n-alert
          v-if="isHistorical"
          type="warning"
          :show-icon="false"
          data-test="viewer-history-notice"
        >
          该引用来自历史版本 v{{ content.version_no }}，当前有效版本为{{
            activeVersionNo === null ? '其它版本' : ` v${activeVersionNo}`
          }}。
        </n-alert>
        <n-alert
          v-if="content.status === 'disabled'"
          type="default"
          :show-icon="false"
          data-test="viewer-disabled-notice"
        >
          该文档已停用，不再参与新的知识检索。
        </n-alert>
        <n-alert
          v-if="locateMode === 'fallback'"
          type="default"
          :show-icon="false"
          data-test="viewer-fallback-notice"
        >
          该引用未记录精确位置，已为你打开来源文档。
        </n-alert>
      </div>

      <!-- 目录：有标题时渲染，点击滚动到对应章节 -->
      <nav v-if="hasOutline" class="doc-outline" aria-label="正文目录" data-test="viewer-outline">
        <button
          v-for="item in content.outline"
          :key="`${item.char_offset}-${item.heading_path}`"
          type="button"
          class="doc-outline-item"
          :data-level="item.level"
          :title="item.heading_path"
          @click="scrollToOutlineItem(item)"
        >
          {{ item.title }}
        </button>
      </nav>

      <!-- 正文：HTML 由 utils/markdown 的安全渲染器生成（html:false，无源码直出） -->
      <div class="doc-body-wrap">
        <!-- eslint-disable-next-line vue/no-v-html -->
        <div ref="bodyRef" class="doc-body" data-test="viewer-body" v-html="renderedHtml" />
      </div>
    </template>
  </div>
</template>

<style scoped>
/*
 * 布局对「面板」与「整页」两种宿主都成立：
 * 外层高度由宿主决定（面板为 100%，整页由页面卡片高度决定），
 * 头部与提示固定，正文区域负责滚动。
 */
.doc-viewer {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  background: var(--sp-color-bg-card);
}

.doc-state {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-2);
  align-items: flex-start;
  padding: var(--sp-space-4);
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.doc-error-body {
  display: flex;
  gap: var(--sp-space-2);
  align-items: center;
  flex-wrap: wrap;
}

.doc-header {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-space-3) var(--sp-space-3) var(--sp-space-2);
  border-bottom: 1px solid var(--sp-color-border);
}

.doc-title {
  margin: 0;
  font-size: var(--sp-font-size-base);
  font-weight: 500;
  word-break: break-word;
}

.doc-meta {
  font-size: var(--sp-font-size-xs);
}

.doc-notices {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-1);
  padding: var(--sp-space-2) var(--sp-space-3) 0;
  font-size: var(--sp-font-size-xs);
}

.doc-outline {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-space-1);
  padding: var(--sp-space-2) var(--sp-space-3);
  border-bottom: 1px solid var(--sp-color-border);
}

.doc-outline-item {
  max-width: 100%;
  padding: 2px 8px;
  border: 1px solid var(--sp-color-border);
  border-radius: 999px;
  background: var(--sp-color-bg-hover);
  color: var(--sp-color-text-2);
  font-size: var(--sp-font-size-xs);
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.doc-outline-item[data-level='1'] {
  font-weight: 600;
}

.doc-outline-item[data-level='3'],
.doc-outline-item[data-level='4'],
.doc-outline-item[data-level='5'],
.doc-outline-item[data-level='6'] {
  color: var(--sp-color-text-3);
}

.doc-outline-item:hover,
.doc-outline-item:focus-visible {
  border-color: var(--sp-color-primary);
  color: var(--sp-color-primary);
  outline: none;
}

.doc-body-wrap {
  flex: 1;
  min-height: 0;
  /* 纵向可滚、横向不滚：正文按宽度换行，宽表格由表格自己内部横向滚动 */
  overflow-y: auto;
  overflow-x: hidden;
  /*
   * 必须固定预留纵向滚动条槽位：正文很长时纵向滚动条会占掉约 17px 宽度，
   * 若此时才收窄内容盒，正文块仍按收窄前的宽度排版，就会凭空多出一条
   * 几乎占满整行、还压住正文的横向滚动条（用户反馈的「下拉条太长」）。
   * 预留槽位后内容盒宽度恒定，横向滚动条不会再出现，长度也不会跳变。
   */
  scrollbar-gutter: stable;
  /* 底部留白：避免高亮落在视口最底部 */
  padding: var(--sp-space-3) var(--sp-space-3) 45vh;
}

.doc-body {
  font-size: var(--sp-font-size-sm);
  line-height: 1.75;
  color: var(--sp-color-text-1);
  word-break: break-word;
  overflow-wrap: anywhere;
}

/* 正文排版：v-html 生成的子节点需要 :deep() */
.doc-body :deep(h1),
.doc-body :deep(h2),
.doc-body :deep(h3),
.doc-body :deep(h4),
.doc-body :deep(h5),
.doc-body :deep(h6) {
  margin: var(--sp-space-3) 0 var(--sp-space-1);
  font-size: var(--sp-font-size-base);
  line-height: 1.5;
}

.doc-body :deep(h1) {
  font-size: 1.15rem;
}

.doc-body :deep(p) {
  margin: 0 0 var(--sp-space-2);
}

.doc-body :deep(ul),
.doc-body :deep(ol) {
  margin: 0 0 var(--sp-space-2);
  padding-left: 1.3em;
}

.doc-body :deep(li) {
  margin-bottom: 2px;
}

.doc-body :deep(blockquote) {
  margin: 0 0 var(--sp-space-2);
  padding: var(--sp-space-1) var(--sp-space-3);
  border-left: 3px solid var(--sp-color-primary);
  background: var(--sp-color-bg-hover);
  color: var(--sp-color-text-2);
}

.doc-body :deep(pre) {
  margin: 0 0 var(--sp-space-2);
  padding: var(--sp-space-2);
  background: var(--sp-color-bg-hover);
  border-radius: var(--sp-radius-sm);
  overflow-x: auto;
}

.doc-body :deep(code) {
  font-size: var(--sp-font-size-xs);
}

.doc-body :deep(table) {
  /*
   * 宽表格（多列 / 长单元格）不参与面板宽度的竞争：表格自身成为可横向滚动的块，
   * 这样既不会把正文区撑宽而触发面板级横向滚动条，也不会让列被裁掉。
   * max-width:100% 保证窄表格仍然按内容宽度渲染、宽表格最多占满面板宽度。
   */
  display: block;
  width: max-content;
  max-width: 100%;
  overflow-x: auto;
  margin-bottom: var(--sp-space-2);
  border-collapse: collapse;
  font-size: var(--sp-font-size-xs);
}

.doc-body :deep(th),
.doc-body :deep(td) {
  border: 1px solid var(--sp-color-border);
  padding: 4px 8px;
  text-align: left;
}

/* 引用高亮：常驻直到切换引用或关闭面板（不自动淡出） */
.doc-body :deep(.sp-range-highlight) {
  background: #fff2ac;
  box-shadow: 0 0 0 2px #fff2ac;
  border-radius: 2px;
  scroll-margin-top: 24px;
}
</style>
