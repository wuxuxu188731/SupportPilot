/*
 * 金额展示工具。
 *
 * 后端事实（docs/frontend/api-inventory.md 5.2）：金额字段一律为整数「分」
 * （amount_cents），不存在小数金额字段；用整数运算换算避免浮点误差。
 */

/** 常见币种符号：未收录的币种原样显示 ISO 代码。 */
const CURRENCY_SYMBOLS: Record<string, string> = {
  CNY: '¥',
  USD: '$',
  EUR: '€',
  JPY: '¥',
}

/** 按「分」金额格式化为千分位金额文本，如 123456 分 -> "¥1,234.56"。 */
export function formatAmountCents(amountCents: number, currency: string): string {
  // 负金额防御：前端展示场景金额应为正，但仍按符号正确处理
  const sign = amountCents < 0 ? '-' : ''
  const absolute = Math.abs(amountCents)
  const yuan = Math.floor(absolute / 100)
  const cents = absolute % 100
  // 千分位分隔（整数部分）
  const yuanText = String(yuan).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const numeric = `${yuanText}.${String(cents).padStart(2, '0')}`
  const symbol = CURRENCY_SYMBOLS[currency] ?? `${currency} `
  return `${sign}${symbol}${numeric}`
}

/** 金额输入正则：纯数字或最多两位小数；拒绝负数、指数与空串。 */
const YUAN_INPUT_PATTERN = /^\d+(\.\d{1,2})?$/

/**
 * 把用户输入的「元」金额字符串安全转换为「分」整数。
 *
 * 转换规则（与后端 amount_cents 严格正整数约束对齐）：
 *  - 去首尾空白后必须匹配纯数字（可带最多两位小数），否则返回 null；
 *  - 不允许负数、逗号、百分号、指数、小数位超过两位或空串；
 *  - 使用字符串整数运算（整数部分 ×100 + 小数部分补齐两位），
 *    **禁止浮点乘法直接生成金额**（0.29*100=28.999... 类误差）；
 *  - 结果是严格正整数（>0），否则返回 null。
 *
 * @returns 分；输入非法或金额非正时返回 null
 */
export function parseYuanToCents(input: string): number | null {
  const trimmed = input.trim()
  if (!YUAN_INPUT_PATTERN.test(trimmed)) return null
  const [integerPart, fractionPart = ''] = trimmed.split('.')
  const cents = fractionPart.padEnd(2, '0')
  const value = Number(integerPart) * 100 + Number(cents)
  if (!Number.isSafeInteger(value) || value <= 0) return null
  return value
}

/** 把「分」转换为表单金额输入文本，如 6000 -> "60.00"（保持两位小数）。 */
export function formatCentsToYuanInput(amountCents: number): string {
  const yuan = Math.floor(amountCents / 100)
  const cents = amountCents % 100
  return `${yuan}.${String(cents).padStart(2, '0')}`
}
