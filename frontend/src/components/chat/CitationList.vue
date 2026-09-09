<script setup lang="ts">
/*
 * 知识引用列表：结构化展示 citations（来源文档、标题路径、引用正文）。
 *
 * 事实：引用必须读取结构化 citations 数组，禁止从回答文本猜测；
 * 回答正文中的 [C1] 标记通过元素 id（citation-C1）与本卡片关联。
 */
import { NCollapse, NCollapseItem, NText } from 'naive-ui'

import type { Citation } from '@/api/types'

defineProps<{
  /** 结构化知识引用数组（C1..Cn） */
  citations: Citation[]
}>()

/** 引用默认全部展开：引用是回答的证据来源，默认可见而非折叠在深处。 */
function defaultExpanded(citations: Citation[]): string[] {
  return citations.map((citation) => citation.citation_id)
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
</style>
