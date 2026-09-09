/*
 * 时间展示工具。
 *
 * 后端时间事实（与 docs/frontend/api-inventory.md 5.2 一致）：
 *  - users / sessions（注册时间、会话创建/更新、历史消息时间）：
 *    "%Y-%m-%d %H:%M:%S"（无时区后缀），语义为 UTC；
 *  - 统一先把无时区文本按 UTC 解释，再转本地时间展示。
 */

/** 把后端 UTC 文本解析为 Date：支持无时区后缀的 "%Y-%m-%d %H:%M:%S"。 */
export function parseUtcText(text: string): Date {
  const trimmed = text.trim()
  // 已含时区标记（Z 或 ±hh:mm）时直接按 ISO 解析
  if (/Z$|[+-]\d{2}:?\d{2}$/.test(trimmed)) {
    return new Date(trimmed)
  }
  // 无时区后缀：按 UTC 解释（会话/历史消息均如此）
  const normalized = trimmed.includes('T') ? trimmed : trimmed.replace(' ', 'T')
  return new Date(`${normalized}Z`)
}

/** 补零到两位。 */
function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

/**
 * 会话列表的「更新时间」文本：今天显示 HH:mm，
 * 今年内显示 MM-DD，更早显示 YYYY-MM-DD。
 * nowMs 可注入以便测试（默认取当前时间）。
 */
export function formatConversationTime(utcText: string, nowMs = Date.now()): string {
  const date = parseUtcText(utcText)
  if (Number.isNaN(date.getTime())) return utcText // 解析失败原样展示
  const now = new Date(nowMs)
  const sameDay = date.toDateString() === now.toDateString()
  if (sameDay) {
    return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  }
  const sameYear = date.getFullYear() === now.getFullYear()
  const monthDay = `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
  if (sameYear) return monthDay
  return `${date.getFullYear()}-${monthDay}`
}

/** 消息时间：今天显示 HH:mm，否则显示 MM-DD HH:mm（跨年追加年份）。 */
export function formatMessageTime(utcText: string, nowMs = Date.now()): string {
  const date = parseUtcText(utcText)
  if (Number.isNaN(date.getTime())) return utcText
  const now = new Date(nowMs)
  const clock = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  const sameDay = date.toDateString() === now.toDateString()
  if (sameDay) return clock
  const monthDay = `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
  const prefix =
    date.getFullYear() === now.getFullYear() ? monthDay : `${date.getFullYear()}-${monthDay}`
  return `${prefix} ${clock}`
}
