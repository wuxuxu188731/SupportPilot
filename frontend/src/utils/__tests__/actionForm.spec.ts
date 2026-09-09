/*
 * 审批决定表单校验测试：变化字段构造、金额转换、退款范围限制、
 * 未变化禁止提交与备注长度。
 */

import { describe, expect, it } from 'vitest'

import type { VersionView } from '@/api/actionTypes'
import { validateApprovedWithChanges, validateComment } from '@/utils/actionForm'

/** 构造一个请求版本（退款：6000 分、quality_issue、部分退款）。 */
function requestedVersion(partial: Partial<VersionView> = {}): VersionView {
  return {
    version_id: 'v-1',
    version_no: 1,
    amount_cents: 6000,
    currency: 'CNY',
    reason_code: 'quality_issue',
    reason_text: '商品存在质量问题',
    refund_scope: 'partial',
    coupon_valid_days: null,
    created_by_user_id: 'u-1',
    created_at: '2026-09-01T00:00:00Z',
    ...partial,
  }
}

/** 构造一个补偿请求版本（无 refund_scope）。 */
function compensationVersion(): VersionView {
  return requestedVersion({
    reason_code: 'delayed_shipment',
    reason_text: '发货延迟，客户不满意',
    refund_scope: null,
    coupon_valid_days: 30,
  })
}

describe('validateApprovedWithChanges 变化字段构造', () => {
  it('只发送真正变化的字段（未变化字段不发送）', () => {
    // 保护行为：修改后批准只提交变化的字段，未变字段交由后端沿用请求版本
    const result = validateApprovedWithChanges(
      requestedVersion(),
      {
        amountText: '8.00',
        reasonCode: 'quality_issue',
        reasonText: '商品存在质量问题',
        refundScope: 'partial',
      },
      'refund',
    )

    expect(result.ok).toBe(true)
    expect(result.changes).toEqual({ amount_cents: 800 })
  })

  it('多个字段变化时全部携带', () => {
    // 保护行为：金额、原因码、原因说明与退款范围都可一起修改
    const result = validateApprovedWithChanges(
      requestedVersion(),
      {
        amountText: '10.00',
        reasonCode: 'damaged_item',
        reasonText: '到货破损，已确认',
        refundScope: 'full',
      },
      'refund',
    )

    expect(result.ok).toBe(true)
    expect(result.changes).toEqual({
      amount_cents: 1000,
      reason_code: 'damaged_item',
      reason_text: '到货破损，已确认',
      refund_scope: 'full',
    })
  })

  it('未发生实际变化时禁止提交', () => {
    // 边界情况：与请求版本完全一致时后端会 422，前端必须先拦截
    const result = validateApprovedWithChanges(
      requestedVersion(),
      {
        amountText: '60.00',
        reasonCode: 'quality_issue',
        reasonText: '商品存在质量问题',
        refundScope: 'partial',
      },
      'refund',
    )

    expect(result.ok).toBe(false)
    expect(result.changes).toBeNull()
    expect(result.formMessage).toContain('至少修改一项')
  })

  it('金额字符串安全转换为分（0.29 → 29）', () => {
    // 保护行为：金额必须按字符串整数运算转换为分，禁止浮点误差
    const result = validateApprovedWithChanges(
      requestedVersion({ amount_cents: 2900 }),
      {
        amountText: '0.29',
        reasonCode: 'quality_issue',
        reasonText: '商品存在质量问题',
        refundScope: 'partial',
      },
      'refund',
    )

    expect(result.changes).toEqual({ amount_cents: 29 })
  })

  it('金额超过两位小数时报错并定位到金额字段', () => {
    // 边界情况：超出分精度的输入必须在字段级报错
    const result = validateApprovedWithChanges(
      requestedVersion(),
      {
        amountText: '12.345',
        reasonCode: 'damaged_item',
        reasonText: '到货破损，已确认',
        refundScope: 'partial',
      },
      'refund',
    )

    expect(result.ok).toBe(false)
    expect(result.errors.amount_cents).toBeDefined()
  })

  it('非正金额（0）报错', () => {
    // 边界情况：金额必须为严格正整数（分），0 元不允许
    const result = validateApprovedWithChanges(
      requestedVersion(),
      {
        amountText: '0',
        reasonCode: 'damaged_item',
        reasonText: '到货破损，已确认',
        refundScope: 'partial',
      },
      'refund',
    )

    expect(result.ok).toBe(false)
    expect(result.errors.amount_cents).toBeDefined()
    expect(result.errors.amount_cents).toContain('大于 0')
  })

  it('补偿提案不发送 refund_scope（即使草稿上有值）', () => {
    // 保护行为：refund_scope 仅退款提案允许；补偿提案必须拒绝携带
    const result = validateApprovedWithChanges(
      compensationVersion(),
      {
        amountText: '10.00',
        reasonCode: 'delayed_shipment',
        reasonText: '发货延迟，客户不满意',
        refundScope: 'partial',
      },
      'compensation',
    )

    expect(result.ok).toBe(true)
    expect(result.changes).toEqual({ amount_cents: 1000 })
    expect(result.changes).not.toHaveProperty('refund_scope')
  })

  it('原因码不在该动作枚举中时报错', () => {
    // 边界情况：退款提案不能提交补偿原因码（后端会 422）
    const result = validateApprovedWithChanges(
      requestedVersion(),
      {
        amountText: '8.00',
        reasonCode: 'delayed_shipment',
        reasonText: '发货延迟',
        refundScope: 'partial',
      },
      'refund',
    )

    expect(result.ok).toBe(false)
    expect(result.errors.reason_code).toBeDefined()
  })

  it('原因说明清空时报错（1–2000 字符约束）', () => {
    // 边界情况：原因说明最小长度 1，清空修改必须拦截
    const result = validateApprovedWithChanges(
      requestedVersion(),
      {
        amountText: '8.00',
        reasonCode: 'quality_issue',
        reasonText: '   ',
        refundScope: 'partial',
      },
      'refund',
    )

    expect(result.ok).toBe(false)
    expect(result.errors.reason_text).toBeDefined()
    expect(result.errors.reason_text).toContain('不能为空')
  })

  it('原因说明超过 2000 字符时报错', () => {
    // 边界情况：原因说明最大长度 2000，超长必须拦截
    const result = validateApprovedWithChanges(
      requestedVersion(),
      {
        amountText: '8.00',
        reasonCode: 'quality_issue',
        reasonText: '很长的说明'.repeat(401),
        refundScope: 'partial',
      },
      'refund',
    )

    expect(result.ok).toBe(false)
    expect(result.errors.reason_text).toContain('2000')
  })

  it('备注去首尾空白后最长 1000 字符', () => {
    // 边界情况：备注长度上限 1000（服务端 strip 后校验）
    expect(validateComment('同意退款')).toBeNull()
    expect(validateComment('  同意退款  ')).toBeNull()
    expect(validateComment('x'.repeat(1001))).toContain('1000')
    expect(validateComment('x'.repeat(1000))).toBeNull()
  })
})
