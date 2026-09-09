/*
 * 知识库集中显示映射：状态文案 + 状态色 + 通用格式化。
 *
 * 事实说明：
 *  - 所有枚举值以后端 `app/knowledge/base.py` / `app/schemas/knowledge.py`
 *    为准；展示文案集中在本文件，禁止把中文字符串散落在多个组件；
 *  - 未知枚举值必须安全降级为原始值（displayText / statusTagType 兜底），
 *    不能导致页面崩溃；
 *  - 颜色不能作为唯一状态表达方式：所有状态都同时展示文字；
 *  - 空值（null/undefined）统一显示「—」，不得显示字符串 "null" 或
 *    "undefined"。
 */

import type { TagProps } from 'naive-ui'

import type {
  DocumentSourceTypeValue,
  DocumentStatusValue,
  IngestionStatusValue,
} from '@/api/knowledgeTypes'

/** 文档来源类型 → 中文标签。 */
export const DOCUMENT_SOURCE_TYPE_TEXT: Record<DocumentSourceTypeValue, string> = {
  markdown: 'Markdown',
  text: '纯文本',
  word: 'Word（只读兼容）',
}

/** 文档来源类型 → 标签色（与文字同时展示，颜色不单独表意）。 */
export const DOCUMENT_SOURCE_TYPE_TAG: Record<DocumentSourceTypeValue, TagProps['type']> = {
  markdown: 'info',
  text: 'default',
  word: 'warning',
}

/** 文档状态 → 中文标签。 */
export const DOCUMENT_STATUS_TEXT: Record<DocumentStatusValue, string> = {
  processing: '处理中',
  active: '已启用',
  disabled: '已停用',
  failed: '失败',
}

/** 文档状态 → 标签色。 */
export const DOCUMENT_STATUS_TAG: Record<DocumentStatusValue, TagProps['type']> = {
  processing: 'warning',
  active: 'success',
  disabled: 'default',
  failed: 'error',
}

/** 入库任务状态 → 中文标签。 */
export const INGESTION_STATUS_TEXT: Record<IngestionStatusValue, string> = {
  queued: '排队中',
  running: '处理中',
  succeeded: '成功',
  failed: '失败',
}

/** 入库任务状态 → 标签色。 */
export const INGESTION_STATUS_TAG: Record<IngestionStatusValue, TagProps['type']> = {
  queued: 'info',
  running: 'info',
  succeeded: 'success',
  failed: 'error',
}

/** 文档状态 → 是否参与知识检索（active 时参与；processing/failed 说明尚未生效）。 */
export function isDocumentSearchable(status: DocumentStatusValue | string | null | undefined): boolean {
  return status === 'active'
}

/** 通用映射查表：空值显示「—」，未知值安全降级为原始字符串。 */
export function knowledgeDisplayText(
  map: Record<string, string>,
  value: string | null | undefined,
): string {
  if (value === null || value === undefined) return '—'
  return map[value] ?? value
}

/** 通用标签色映射查表：未知值降级为 default（中性色）。 */
export function knowledgeStatusTag(
  map: Record<string, TagProps['type']>,
  value: string,
): TagProps['type'] {
  return map[value] ?? 'default'
}

/** 文件大小展示：字节数 → "1.5 KB" / "2 MiB" 风格（下限 B）。 */
export function formatKnowledgeFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const kb = bytes / 1024
  if (kb < 1024) return `${trimDecimal(kb)} KB`
  return `${trimDecimal(kb / 1024)} MiB`
}

/** 去掉多余小数点后的零（如 1.50 → 1.5），最多保留两位小数。 */
function trimDecimal(value: number): string {
  const rounded = Math.round(value * 100) / 100
  return String(rounded)
}
