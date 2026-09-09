<script setup lang="ts">
/*
 * 文档版本历史面板：按 version_no 升序展示全部版本
 * （当前有效版本高亮），不含正文/下载/预览入口。
 *
 * 事实说明：
 *  - 版本字段来自后端 DocumentVersionResponse（无 raw_text）；
 *  - content_hash / version_id 支持截断与复制；
 *  - 空值一律显示「—」，不显示 null/undefined。
 */
import { computed } from 'vue'
import { NTag, useMessage } from 'naive-ui'

import type { DocumentVersionInfo } from '@/api/knowledgeTypes'
import { copyToClipboard } from '@/utils/clipboard'
import { formatDateTime } from '@/utils/time'

const props = defineProps<{
  /** 全部版本（service 返回 version_no 升序；组件内再排序防御） */
  versions: DocumentVersionInfo[]
  /** 当前有效版本 id（用于高亮「当前有效版本」标记） */
  activeVersionId: string | null
}>()

const message = useMessage()

/** 按 version_no 升序排列（防御服务端顺序异常）。 */
const sortedVersions = computed(() =>
  [...props.versions].sort(
    (a, b) => a.version_no - b.version_no || a.version_id.localeCompare(b.version_id),
  ),
)

/** 长值截断（中间省略，保留首尾）。 */
function shorten(value: string | null | undefined, head = 8, tail = 4): string {
  if (!value) return '—'
  if (value.length <= head + tail + 1) return value
  return `${value.slice(0, head)}…${value.slice(-tail)}`
}

/** 点击复制（成功提示，失败静默）。 */
async function onCopy(value: string | null): Promise<void> {
  if (!value) return
  const ok = await copyToClipboard(value)
  if (ok) message.success('已复制')
}

/** 展示辅助：空值 → 「—」。 */
function display(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return String(value)
}
</script>

<template>
  <section class="version-history" data-test="version-history">
    <h3 class="panel-title">版本历史（{{ sortedVersions.length }}）</h3>
    <p v-if="sortedVersions.length === 0" class="panel-empty">
      暂无版本记录（文档尚未成功入库）。
    </p>

    <ol v-else class="version-list" data-test="version-list">
      <li
        v-for="version in sortedVersions"
        :key="version.version_id"
        class="version-item"
        :class="{ 'is-active': version.version_id === activeVersionId }"
        data-test="version-item"
      >
        <div class="version-head">
          <span class="version-no">v{{ version.version_no }}</span>
          <n-tag v-if="version.version_id === activeVersionId" type="success" size="small">
            当前有效版本
          </n-tag>
        </div>

        <dl class="version-meta">
          <div class="meta-item meta-wide">
            <dt>版本 ID</dt>
            <dd>
              <button type="button" class="id-link" :title="version.version_id" @click="onCopy(version.version_id)">
                {{ shorten(version.version_id) }}
              </button>
            </dd>
          </div>
          <div class="meta-item meta-wide">
            <dt>内容哈希</dt>
            <dd>
              <button type="button" class="id-link" :title="version.content_hash" @click="onCopy(version.content_hash)">
                {{ shorten(version.content_hash, 10, 6) }}
              </button>
            </dd>
          </div>
          <div class="meta-item">
            <dt>解析器版本</dt>
            <dd>{{ display(version.loader_version) }}</dd>
          </div>
          <div class="meta-item">
            <dt>分块器版本</dt>
            <dd>{{ display(version.chunker_version) }}</dd>
          </div>
          <div class="meta-item">
            <dt>Embedding 模型</dt>
            <dd>{{ display(version.embedding_model) }}</dd>
          </div>
          <div class="meta-item">
            <dt>向量维度</dt>
            <dd>{{ display(version.embedding_dimensions) }}</dd>
          </div>
          <div class="meta-item">
            <dt>创建时间</dt>
            <dd>{{ formatDateTime(version.created_at) }}</dd>
          </div>
        </dl>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.version-history {
  background: var(--sp-color-bg-card);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-md);
  padding: var(--sp-space-4);
}

.panel-title {
  margin: 0 0 var(--sp-space-3);
  font-size: var(--sp-font-size-md);
  font-weight: 600;
}

.panel-empty {
  margin: 0;
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.version-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-3);
}

.version-item {
  padding: var(--sp-space-3);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-sm);
}

.version-item.is-active {
  border-color: var(--sp-color-primary);
  background: var(--sp-color-bg-elevated);
}

.version-head {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  margin-bottom: var(--sp-space-2);
}

.version-no {
  font-weight: 600;
  font-size: var(--sp-font-size-md);
}

.version-meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--sp-space-2) var(--sp-space-4);
  margin: 0;
}

.meta-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.meta-wide {
  grid-column: span 2;
}

@media (max-width: 640px) {
  .meta-wide {
    grid-column: span 1;
  }
}

.meta-item dt {
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.meta-item dd {
  margin: 0;
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-2);
  min-width: 0;
  overflow-wrap: anywhere;
}

.id-link {
  border: none;
  background: none;
  padding: 0;
  font: inherit;
  font-family: monospace;
  color: var(--sp-color-text-2);
  cursor: pointer;
  text-decoration: underline;
  text-decoration-style: dotted;
  text-underline-offset: 2px;
}

.id-link:hover {
  color: var(--sp-color-primary);
}
</style>
