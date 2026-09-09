/*
 * 知识库显示映射测试：状态文案、状态色、未知枚举安全降级、
 * 空值显示「—」、文件大小格式化、是否参与检索判定。
 */

import { describe, expect, it } from 'vitest'

import {
  DOCUMENT_SOURCE_TYPE_TEXT,
  DOCUMENT_SOURCE_TYPE_TAG,
  DOCUMENT_STATUS_TEXT,
  DOCUMENT_STATUS_TAG,
  INGESTION_STATUS_TEXT,
  INGESTION_STATUS_TAG,
  formatKnowledgeFileSize,
  isDocumentSearchable,
  knowledgeDisplayText,
  knowledgeStatusTag,
} from '@/utils/knowledgeDisplay'

describe('枚举状态中文文案', () => {
  it('文档来源类型：markdown/text/word', () => {
    // 保护行为：三种来源类型必须都有明确中文文案
    expect(DOCUMENT_SOURCE_TYPE_TEXT.markdown).toBe('Markdown')
    expect(DOCUMENT_SOURCE_TYPE_TEXT.text).toBe('纯文本')
    expect(DOCUMENT_SOURCE_TYPE_TEXT.word).toBe('Word（只读兼容）')
  })

  it('文档状态：processing/active/disabled/failed', () => {
    // 保护行为：四种文档状态必须都有明确中文文案
    expect(DOCUMENT_STATUS_TEXT.processing).toBe('处理中')
    expect(DOCUMENT_STATUS_TEXT.active).toBe('已启用')
    expect(DOCUMENT_STATUS_TEXT.disabled).toBe('已停用')
    expect(DOCUMENT_STATUS_TEXT.failed).toBe('失败')
  })

  it('入库任务状态：queued/running/succeeded/failed', () => {
    // 保护行为：四种任务状态必须都有明确中文文案
    expect(INGESTION_STATUS_TEXT.queued).toBe('排队中')
    expect(INGESTION_STATUS_TEXT.running).toBe('处理中')
    expect(INGESTION_STATUS_TEXT.succeeded).toBe('成功')
    expect(INGESTION_STATUS_TEXT.failed).toBe('失败')
  })

  it('未知枚举值安全降级为原始值（不崩溃）', () => {
    // 边界情况：后端新增枚举时前端不能崩溃，显示原始字符串
    expect(knowledgeDisplayText(DOCUMENT_STATUS_TEXT, 'archived')).toBe('archived')
    expect(knowledgeDisplayText(DOCUMENT_SOURCE_TYPE_TEXT, 'docx')).toBe('docx')
    expect(knowledgeDisplayText(INGESTION_STATUS_TEXT, 'paused')).toBe('paused')
  })

  it('空值显示「—」，不显示字符串 null/undefined', () => {
    // 边界情况：null/undefined 字段必须统一显示「—」占位
    expect(knowledgeDisplayText(DOCUMENT_STATUS_TEXT, null)).toBe('—')
    expect(knowledgeDisplayText(DOCUMENT_STATUS_TEXT, undefined)).toBe('—')
  })
})

describe('状态颜色映射（颜色不单独表意）', () => {
  it('已知状态返回对应标签色', () => {
    // 保护行为：状态色在文字之外提供辅助区分
    expect(DOCUMENT_STATUS_TAG.active).toBe('success')
    expect(DOCUMENT_STATUS_TAG.disabled).toBe('default')
    expect(DOCUMENT_STATUS_TAG.failed).toBe('error')
    expect(DOCUMENT_SOURCE_TYPE_TAG.markdown).toBe('info')
    expect(INGESTION_STATUS_TAG.succeeded).toBe('success')
  })

  it('未知状态降级为中性色 default', () => {
    // 边界情况：未知状态不得产生非法 Tag type
    expect(knowledgeStatusTag(DOCUMENT_STATUS_TAG, 'archived')).toBe('default')
    expect(knowledgeStatusTag(INGESTION_STATUS_TAG, 'paused')).toBe('default')
  })
})

describe('是否参与检索判定', () => {
  it('只有 active 状态参与检索', () => {
    // 保护行为：停用/处理中/失败的文档不得描述为可检索
    expect(isDocumentSearchable('active')).toBe(true)
    expect(isDocumentSearchable('disabled')).toBe(false)
    expect(isDocumentSearchable('processing')).toBe(false)
    expect(isDocumentSearchable('failed')).toBe(false)
  })
})

describe('文件大小格式化', () => {
  it('按 B / KB / MiB 展示并保留合适精度', () => {
    // 保护行为：字节数必须按 1024 进制展示（与后端 2MiB 口径一致）
    expect(formatKnowledgeFileSize(512)).toBe('512 B')
    expect(formatKnowledgeFileSize(1536)).toBe('1.5 KB')
    expect(formatKnowledgeFileSize(2 * 1024 * 1024)).toBe('2 MiB')
  })

  it('非法或负数大小显示「—」', () => {
    // 边界情况：无效大小不能崩溃
    expect(formatKnowledgeFileSize(-1)).toBe('—')
  })
})
