/*
 * 金额工具测试：分 → 千分位金额文本、元字符串 → 分（安全转换）。
 */

import { describe, expect, it } from 'vitest'

import {
  formatAmountCents,
  formatCentsToYuanInput,
  parseYuanToCents,
} from '@/utils/money'

describe('formatAmountCents 金额展示', () => {
  it('按分格式化为千分位金额文本', () => {
    // 保护行为：123456 分必须显示为 ¥1,234.56（整数运算，无浮点误差）
    expect(formatAmountCents(123456, 'CNY')).toBe('¥1,234.56')
    expect(formatAmountCents(100, 'CNY')).toBe('¥1.00')
    expect(formatAmountCents(5, 'CNY')).toBe('¥0.05')
    expect(formatAmountCents(0, 'CNY')).toBe('¥0.00')
  })

  it('未知币种回退为 ISO 代码', () => {
    // 边界情况：后端只承诺币种等于订单币种，不硬编码唯一币种
    expect(formatAmountCents(5000, 'USD')).toBe('$50.00')
    expect(formatAmountCents(5000, 'JPY')).toBe('¥50.00')
    expect(formatAmountCents(5000, 'XXX')).toBe('XXX 50.00')
  })
})

describe('parseYuanToCents 金额字符串安全转换', () => {
  it('整数元转换为分', () => {
    // 保护行为：输入以「元」为单位，提交前必须安全转成「分」整数
    expect(parseYuanToCents('60')).toBe(6000)
    expect(parseYuanToCents('1')).toBe(100)
  })

  it('小数最多两位，按字符串整数运算', () => {
    // 保护行为：禁止浮点乘法（0.29*100=28.999...），0.29 必须精确为 29 分
    expect(parseYuanToCents('0.29')).toBe(29)
    expect(parseYuanToCents('12.3')).toBe(1230)
    expect(parseYuanToCents('100.5')).toBe(10050)
    expect(parseYuanToCents('0.05')).toBe(5)
  })

  it('超过两位小数报错（返回 null）', () => {
    // 边界情况：超出后端整数分精度，必须拒绝
    expect(parseYuanToCents('1.234')).toBeNull()
    expect(parseYuanToCents('12.345')).toBeNull()
  })

  it('非正数、负数、空串与非法字符一律拒绝', () => {
    // 边界情况：金额必须是严格正整数，拒绝 0/负数/空/逗号/字母
    expect(parseYuanToCents('0')).toBeNull()
    expect(parseYuanToCents('0.00')).toBeNull()
    expect(parseYuanToCents('-5')).toBeNull()
    expect(parseYuanToCents('')).toBeNull()
    expect(parseYuanToCents('  ')).toBeNull()
    expect(parseYuanToCents('1,000')).toBeNull()
    expect(parseYuanToCents('abc')).toBeNull()
    expect(parseYuanToCents('1e3')).toBeNull()
    expect(parseYuanToCents('.5')).toBeNull()
    expect(parseYuanToCents('5.')).toBeNull()
  })

  it('首尾空白不影响转换', () => {
    // 边界情况：去除首尾空白后按数字处理
    expect(parseYuanToCents(' 60.50 ')).toBe(6050)
  })
})

describe('formatCentsToYuanInput 表单回填', () => {
  it('分 → 两位小数的元文本', () => {
    // 保护行为：表单初始值必须可编辑且保留两位小数
    expect(formatCentsToYuanInput(6000)).toBe('60.00')
    expect(formatCentsToYuanInput(29)).toBe('0.29')
    expect(formatCentsToYuanInput(123456)).toBe('1234.56')
  })
})
