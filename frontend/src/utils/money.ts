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
