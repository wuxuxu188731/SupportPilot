<script setup lang="ts">
/*
 * 对话区右侧正文面板：把「引用 → 正文」的查看器内嵌在对话页右侧。
 *
 * 事实与边界（设计稿 4.4）：
 *  - 面板**不是** `n-drawer`（覆盖式遮罩会盖住对话），而是普通 flex 列，
 *    与 `.chat-side`、`.chat-main` 同屏并排；宽度由宿主 ChatView 给定；
 *  - 面板只负责外壳：关闭按钮与把定位参数透传给查看器；
 *    正文加载、历史版本提示、高亮定位全部由 DocumentContentViewer 负责；
 *  - 关闭按钮只上抛 close 事件，不改会话路由、不触发历史重载。
 */
import { NButton } from 'naive-ui'

import type { CitationTarget } from '@/api/types'
import DocumentContentViewer from '@/components/knowledge/DocumentContentViewer.vue'

defineProps<{
  /** 当前定位目标：文档/版本/偏移/标题（结构化字段，来自引用卡片） */
  target: CitationTarget
}>()

const emit = defineEmits<{
  /** 用户点击关闭：由 ChatView 收起面板 */
  close: []
}>()
</script>

<template>
  <aside class="doc-panel" aria-label="来源文档正文" data-test="document-content-panel">
    <n-button
      quaternary
      size="small"
      class="doc-panel-close"
      aria-label="关闭正文面板"
      data-test="close-document-panel"
      @click="emit('close')"
    >
      关闭
    </n-button>
    <div class="doc-panel-body">
      <DocumentContentViewer
        :document-id="target.documentId"
        :version-id="target.versionId"
        :start-offset="target.startOffset"
        :end-offset="target.endOffset"
        :heading-path="target.headingPath"
        :show-outline="false"
      />
    </div>
  </aside>
</template>

<style scoped>
.doc-panel {
  position: relative;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  /* 与对话区同屏并排：宽度固定，clamp 保证窄桌面也不会挤坏对话区 */
  width: clamp(360px, 32vw, 520px);
  min-width: 0;
  background: var(--sp-color-bg-card);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-lg);
  overflow: hidden;
}

.doc-panel-close {
  position: absolute;
  z-index: 1;
  top: var(--sp-space-3);
  right: var(--sp-space-3);
}

.doc-panel-body {
  flex: 1;
  min-height: 0;
  display: flex;
}

.doc-panel-body :deep(.doc-header) {
  padding-right: 72px;
}
</style>
