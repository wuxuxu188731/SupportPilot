<script setup lang="ts">
/*
 * 知识引用列表：结构化展示 citations（来源文档、标题路径、引用正文），
 * 并提供「查看原文位置」入口（引用 → 知识库正文）。
 *
 * 事实：引用必须读取结构化 citations 数组，禁止从回答文本猜测；
 * 回答正文中的 [C1] 标记通过元素 id（citation-C1）与本卡片关联。
 *
 * 跳转载荷：emit 的是**结构化字段**（documentId/versionId/startOffset/endOffset/
 * title/headingPath），全部直接取自 citations 元素，不从回答文本或引用正文里解析。
 * 偏移为 null/undefined 时入口仍然可用（后端允许偏移为空），
 * 只是不再声称"定位"——文案退化为「查看来源文档」。
 */
import { NButton, NCollapse, NCollapseItem, NText } from 'naive-ui'

import type { Citation, CitationTarget } from '@/api/types'

defineProps<{
  /** 结构化知识引用数组（C1..Cn） */
  citations: Citation[]
}>()

const emit = defineEmits<{
  /** 打开来源文档正文：载荷为结构化定位信息（不做任何文本解析） */
  openDocument: [payload: CitationTarget]
}>()

/** 引用默认全部展开：引用是回答的证据来源，默认可见而非折叠在深处。 */
function defaultExpanded(citations: Citation[]): string[] {
  return citations.map((citation) => citation.citation_id)
}

/**
 * 是否记录了精确位置。
 *
 * 必须用 `== null` 判断：偏移 0 是合法值（片段恰好在正文开头），
 * 用 `!start_offset` 会把合法定位误判成"无偏移"。
 */
function hasPreciseRange(citation: Citation): boolean {
  return citation.start_offset != null && citation.end_offset != null
}

/** 点击「查看原文位置」：把结构化定位信息上抛给宿主（面板或整页跳转）。 */
function onOpenDocument(citation: Citation): void {
  emit('openDocument', {
    documentId: citation.document_id,
    versionId: citation.version_id,
    startOffset: citation.start_offset ?? null,
    endOffset: citation.end_offset ?? null,
    headingPath: citation.heading_path,
    title: citation.title,
  })
}
</script>

<template>
  <div class="citation-list" aria-label="知识引用">
    <n-collapse
      arrow-placement="right"
      class="citation-collapse"
      :default-expanded-names="defaultExpanded(citations)"
    >
      <n-collapse-item
        v-for="citation in citations"
        :key="citation.citation_id"
        :name="citation.citation_id"
        :title="`[${citation.citation_id}] ${citation.title}`"
      >
        <div
          :id="`citation-${citation.citation_id}`"
          tabindex="-1"
          class="citation-card"
          :aria-label="`引用 ${citation.citation_id} 详情`"
        >
          <n-text v-if="citation.heading_path" depth="3" class="citation-heading">
            章节：{{ citation.heading_path }}
          </n-text>
          <blockquote class="citation-content">{{ citation.content }}</blockquote>
          <n-text depth="3" class="citation-meta">
            文档 {{ citation.document_id }} · 版本 {{ citation.version_id }} ·
            知识块 {{ citation.chunk_id }}
          </n-text>
          <!-- 引用 → 正文：键盘可达的 button；无偏移时仍可用但不说"定位" -->
          <div class="citation-actions">
            <n-button
              size="tiny"
              secondary
              type="primary"
              :data-test="`open-document-${citation.citation_id}`"
              @click="onOpenDocument(citation)"
            >
              {{ hasPreciseRange(citation) ? '查看原文位置' : '查看来源文档' }}
            </n-button>
          </div>
        </div>
      </n-collapse-item>
    </n-collapse>
  </div>
</template>

<style scoped>
.citation-list {
  margin-top: var(--sp-space-3);
}

.citation-collapse {
  background: var(--sp-color-bg-hover);
  border-radius: var(--sp-radius-md);
}

.citation-card {
  padding: var(--sp-space-2) var(--sp-space-3) var(--sp-space-3);
}

.citation-card:focus-visible {
  outline: 2px solid var(--sp-color-primary);
  outline-offset: 2px;
}

.citation-heading {
  display: block;
  font-size: var(--sp-font-size-sm);
  margin-bottom: var(--sp-space-1);
}

.citation-content {
  margin: var(--sp-space-1) 0;
  padding: var(--sp-space-2) var(--sp-space-3);
  border-left: 3px solid var(--sp-color-primary);
  background: var(--sp-color-bg-card);
  border-radius: 0 var(--sp-radius-sm) var(--sp-radius-sm) 0;
  color: var(--sp-color-text-2);
  font-size: var(--sp-font-size-sm);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.citation-meta {
  display: block;
  font-size: var(--sp-font-size-xs);
  word-break: break-all;
}

.citation-actions {
  display: flex;
  gap: var(--sp-space-2);
  margin-top: var(--sp-space-2);
}
</style>
