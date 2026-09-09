/*
 * 时间与金额格式化工具测试。
 * 时间统一按「后端 UTC 文本 → 本地展示」解析，now 参数可注入以便断言。
 */

import { describe, expect, it } from 'vitest'

import { formatAmountCents } from '@/utils/money'
import {
  formatConversationTime,
  formatDateTime,
  formatMessageTime,
  parseUtcText,
} from '@/utils/time'

describe('parseUtcText：后端 UTC 文本解析', () => {
  it('无时区后缀的会话时间按 UTC 解释', () => {
    // 保护行为：后端 "%Y-%m-%d %H:%M:%S" 是无时区 UTC 文本，不能按本地误读
    const date = parseUtcText('2026-09-06 12:34:56')
    expect(date.toISOString()).toBe('2026-09-06T12:34:56.000Z')
  })

  it('带 Z 或时区偏移的 ISO 文本直接解析', () => {
    // 边界情况：事件时间为 ISO 格式（可能无时区后缀），统一兼容处理
    expect(parseUtcText('2026-09-06T12:34:56.000Z').toISOString()).toBe(
      '2026-09-06T12:34:56.000Z',
    )
    expect(parseUtcText('2026-09-06T20:34:56+08:00').toISOString()).toBe(
      '2026-09-06T12:34:56.000Z',
    )
  })
})

describe('formatConversationTime：会话更新时间', () => {
  it('今天只显示时分', () => {
    // 保护行为：会话列表里今天的会话只展示时钟，减少噪音
    const now = new Date('2026-09-06T04:00:00.000Z').getTime()
    expect(formatConversationTime('2026-09-06 03:05:00', now)).toBe('11:05')
  })

  it('今年内显示月-日', () => {
    // 边界情况：更早但同年的会话显示月日
    const now = new Date('2026-09-06T04:00:00.000Z').getTime()
    const text = formatConversationTime('2026-01-02 03:00:00', now)
    expect(text).toBe('01-02')
  })

  it('跨年显示完整日期', () => {
    // 边界情况：跨年会话需要保留年份避免歧义
    const now = new Date('2026-09-06T04:00:00.000Z').getTime()
    expect(formatConversationTime('2025-12-31 03:00:00', now)).toBe('2025-12-31')
  })
})

describe('formatMessageTime：消息时间', () => {
  it('今天的消息显示时分', () => {
    // 保护行为：消息区内今天消息只显示时钟
    const now = new Date('2026-09-06T04:00:00.000Z').getTime()
    expect(formatMessageTime('2026-09-06 02:30:00', now)).toBe('10:30')
  })

  it('非今天的消息带日期前缀', () => {
    // 边界情况：历史消息展示月-日 时分；跨年再补年份
    const now = new Date('2026-09-06T04:00:00.000Z').getTime()
    expect(formatMessageTime('2026-09-01 02:30:00', now)).toBe('09-01 10:30')
    expect(formatMessageTime('2025-08-01 02:30:00', now)).toBe('2025-08-01 10:30')
  })
})

describe('formatDateTime：知识库完整日期时间', () => {
  it('带时区偏移的 ISO 文本转为本地 "YYYY-MM-DD HH:mm"', () => {
    // 保护行为：知识库时间（isoformat 含 +00:00）必须按 UTC 解析后本地展示
    const text = formatDateTime('2026-09-06T12:34:56.789012+00:00')
    // 测试环境时区固定为 UTC+8（CI 配置），避免本地时区差异导致断言不稳定
    expect(text).toBe('2026-09-06 20:34')
  })

  it('空值显示「—」，不显示 null/undefined', () => {
    // 边界情况：可空时间字段（如任务 started_at）必须显示占位符
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime(undefined)).toBe('—')
  })
})

describe('formatAmountCents：按分格式化金额', () => {
  it('CNY 金额用 ¥ 前缀并保留两位小数与千分位', () => {
    // 保护行为：123456 分应展示为 ¥1,234.56（整数运算避免浮点误差）
    expect(formatAmountCents(123456, 'CNY')).toBe('¥1,234.56')
  })

  it('整元金额补齐两位小数', () => {
    // 边界情况：整元金额也必须带 .00，避免用户误读
    expect(formatAmountCents(100, 'CNY')).toBe('¥1.00')
    expect(formatAmountCents(0, 'CNY')).toBe('¥0.00')
  })

  it('未知币种回退为 ISO 代码前缀', () => {
    // 边界情况：不硬编码唯一币种，未知币种原样显示代码
    expect(formatAmountCents(2500, 'XXX')).toBe('XXX 25.00')
  })
})
