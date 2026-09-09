/*
 * DecisionForm 组件测试：三种决定载荷、变化字段构造、金额校验、
 * 补偿提案不发送 refund_scope、确认前不发请求、提交期间防重复、
 * 422 后端错误映射回字段且不清空用户输入。
 */

import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { VersionView } from '@/api/actionTypes'
import DecisionForm from '@/components/approval/DecisionForm.vue'

/** 构造一个退款请求版本（6000 分、quality_issue、部分退款）。 */
function refundVersion(): VersionView {
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
  }
}

/** 构造一个补偿请求版本（不含 refund_scope）。 */
function compensationVersion(): VersionView {
  return {
    ...refundVersion(),
    reason_code: 'delayed_shipment',
    reason_text: '发货延迟',
    refund_scope: null,
    coupon_valid_days: 30,
  }
}

/** 挂载表单（teleport 打桩，让确认对话框内容可被查询）。 */
function mountForm(options: {
  version?: VersionView
  actionType?: 'refund' | 'compensation'
  submitting?: boolean
  serverFieldErrors?: Record<string, string>
  serverMessage?: string | null
} = {}): VueWrapper {
  return mount(DecisionForm, {
    props: {
      requestedVersion: options.version ?? refundVersion(),
      actionType: options.actionType ?? 'refund',
      submitting: options.submitting ?? false,
      serverFieldErrors: options.serverFieldErrors ?? {},
      serverMessage: options.serverMessage ?? null,
    },
    global: {
      stubs: { teleport: true },
    },
  })
}

/** 选择决定方式并展开编辑区。 */
async function chooseMode(wrapper: VueWrapper, mode: string): Promise<void> {
  await wrapper.find(`[data-test="mode-${mode}"]`).setValue(true)
  await flushPromises()
}

/** 打开确认对话框（点击「提交决定」）。 */
async function openConfirm(wrapper: VueWrapper): Promise<void> {
  await wrapper.find('[data-test="submit-decision"]').trigger('click')
  await flushPromises()
}

/** 确认提交。 */
async function confirmSubmit(wrapper: VueWrapper): Promise<void> {
  await wrapper.find('[data-test="confirm-decision"]').trigger('click')
  await flushPromises()
}

/** 解析最新一次 submit 事件载荷。 */
function lastSubmit(wrapper: VueWrapper) {
  const emitted = wrapper.emitted('submit')
  expect(emitted).toBeDefined()
  return emitted![emitted!.length - 1][0] as {
    decision: string
    changes: Record<string, unknown> | null
    comment: string | null
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('DecisionForm 决定载荷', () => {
  it('直接批准：changes 必须为 null，只携带决定与备注', async () => {
    // 保护行为：批准请求禁止携带修改内容（覆盖测试 13）
    const wrapper = mountForm()
    await wrapper.find('[data-test="comment-input"]').setValue('同意退款')
    await openConfirm(wrapper)

    // 确认前不发请求（无 emit）
    expect(wrapper.emitted('submit')).toBeUndefined()
    expect(wrapper.find('[data-test="decision-confirm-content"]').exists()).toBe(true)

    await confirmSubmit(wrapper)

    expect(lastSubmit(wrapper)).toEqual({
      decision: 'approved',
      changes: null,
      comment: '同意退款',
    })
  })

  it('拒绝：changes 必须为 null', async () => {
    // 保护行为：拒绝请求禁止携带修改内容（覆盖测试 14）
    const wrapper = mountForm()
    await chooseMode(wrapper, 'rejected')
    await openConfirm(wrapper)
    await confirmSubmit(wrapper)

    const payload = lastSubmit(wrapper)
    expect(payload.decision).toBe('rejected')
    expect(payload.changes).toBeNull()
  })

  it('修改后批准：只发送真正变化的字段', async () => {
    // 保护行为：未变化的字段不发送（覆盖测试 15）
    const wrapper = mountForm()
    await chooseMode(wrapper, 'approved_with_changes')
    await wrapper.find('[data-test="amount-input"]').setValue('8.00')
    await openConfirm(wrapper)
    await confirmSubmit(wrapper)

    const payload = lastSubmit(wrapper)
    expect(payload.decision).toBe('approved_with_changes')
    expect(payload.changes).toEqual({ amount_cents: 800 })
  })

  it('修改后批准：多个字段变化时全部携带', async () => {
    // 保护行为：金额/原因码/原因说明/退款范围都可同时修改
    const wrapper = mountForm()
    await chooseMode(wrapper, 'approved_with_changes')
    await wrapper.find('[data-test="amount-input"]').setValue('10.00')
    await wrapper.find('[data-test="reason-code-select"]').setValue('damaged_item')
    await wrapper.find('[data-test="reason-text-input"]').setValue('到货破损，已确认')
    await openConfirm(wrapper)
    await confirmSubmit(wrapper)

    const payload = lastSubmit(wrapper)
    expect(payload.changes).toEqual({
      amount_cents: 1000,
      reason_code: 'damaged_item',
      reason_text: '到货破损，已确认',
    })
  })

  it('未发生实际变化时禁止提交并给出提示', async () => {
    // 保护行为：与请求版本完全一致时不能提交（覆盖测试 16）
    const wrapper = mountForm()
    await chooseMode(wrapper, 'approved_with_changes')
    await openConfirm(wrapper)

    expect(wrapper.emitted('submit')).toBeUndefined()
    expect(wrapper.find('[data-test="decision-confirm-content"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="decision-form-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('至少修改一项')
  })

  it('金额超过两位小数时报错并定位到金额字段', async () => {
    // 保护行为：金额精度校验（覆盖测试 18）
    const wrapper = mountForm()
    await chooseMode(wrapper, 'approved_with_changes')
    await wrapper.find('[data-test="amount-input"]').setValue('12.345')
    await openConfirm(wrapper)

    expect(wrapper.emitted('submit')).toBeUndefined()
    expect(wrapper.find('[data-test="amount-error"]').exists()).toBe(true)
  })

  it('补偿提案不发送 refund_scope（编辑区不展示退款范围）', async () => {
    // 保护行为：refund_scope 仅退款提案（覆盖测试 19）
    const wrapper = mountForm({ version: compensationVersion(), actionType: 'compensation' })
    await chooseMode(wrapper, 'approved_with_changes')

    expect(wrapper.find('[data-test="refund-scope-group"]').exists()).toBe(false)

    await wrapper.find('[data-test="amount-input"]').setValue('10.00')
    await openConfirm(wrapper)
    await confirmSubmit(wrapper)

    const payload = lastSubmit(wrapper)
    expect(payload.changes).toEqual({ amount_cents: 1000 })
    expect(payload.changes).not.toHaveProperty('refund_scope')
  })

  it('原因码下拉使用英文枚举提交、中文文案展示', async () => {
    // 保护行为：提交值必须保持英文枚举（覆盖测试 20）
    const wrapper = mountForm()
    await chooseMode(wrapper, 'approved_with_changes')

    const select = wrapper.find('[data-test="reason-code-select"]')
    const options = select.findAll('option')
    expect(options.some((option) => option.text().includes('商品质量问题'))).toBe(true)
    expect(options.some((option) => option.attributes('value') === 'quality_issue')).toBe(true)

    await wrapper.find('[data-test="amount-input"]').setValue('8.00')
    await wrapper.find('[data-test="reason-code-select"]').setValue('lost_in_transit')
    await openConfirm(wrapper)
    await confirmSubmit(wrapper)

    expect(lastSubmit(wrapper).changes).toHaveProperty('reason_code', 'lost_in_transit')
  })

  it('确认前不发起任何请求', async () => {
    // 保护行为：用户确认前不得调用 API（覆盖测试 21；组件只发出 submit 事件）
    const wrapper = mountForm()
    await wrapper.find('[data-test="comment-input"]').setValue('备注')
    await openConfirm(wrapper)

    expect(wrapper.emitted('submit')).toBeUndefined()
  })

  it('提交期间按钮禁用且不重复触发（防重复点击）', async () => {
    // 保护行为：提交中禁止重复点击（覆盖测试 22）
    const wrapper = mountForm()
    await openConfirm(wrapper)
    await wrapper.setProps({ submitting: true })
    await flushPromises()

    // 提交中：确认与提交按钮均处于禁用状态，点击不会触发事件
    expect(wrapper.find('[data-test="confirm-decision"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-test="submit-decision"]').attributes('disabled')).toBeDefined()
    await wrapper.find('[data-test="confirm-decision"]').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('submit')).toBeUndefined()
  })

  it('备注超过 1000 字符时报错', async () => {
    // 边界情况：备注长度上限（覆盖测试 18 同组约束）
    const wrapper = mountForm()
    await wrapper.find('[data-test="comment-input"]').setValue('很长的备注'.repeat(251))
    await openConfirm(wrapper)

    expect(wrapper.emitted('submit')).toBeUndefined()
    expect(wrapper.find('[data-test="comment-error"]').exists()).toBe(true)
  })

  it('后端 422 字段错误映射到对应表单项且不清空用户输入', async () => {
    // 保护行为：422 映射回表单、保留输入（覆盖测试 26）
    const wrapper = mountForm({
      serverFieldErrors: { amount_cents: 'value is not a valid integer' },
      serverMessage: '修改内容不合法',
    })
    await chooseMode(wrapper, 'approved_with_changes')
    await wrapper.find('[data-test="amount-input"]').setValue('12.34')
    await flushPromises()

    // 已输入的值保留
    expect((wrapper.find('[data-test="amount-input"]').element as HTMLInputElement).value).toBe('12.34')
    expect(wrapper.find('[data-test="amount-error"]').text()).toContain('value is not a valid integer')
  })
})
