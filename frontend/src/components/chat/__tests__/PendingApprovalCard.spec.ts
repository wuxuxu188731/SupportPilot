/*
 * 待审批提案卡片测试：action_type/金额/币种/状态展示、approval_id 与
 * run_id 复制、resume_required/error_code 提示、审批中心未开放说明与
 * 预留「查看审批」事件。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import PendingApprovalCard from '@/components/chat/PendingApprovalCard.vue'
import type { PendingApproval } from '@/api/types'

function approval(partial: Partial<PendingApproval> = {}): PendingApproval {
  return {
    run_id: 'run-123',
    proposal_id: 'proposal-123',
    approval_id: 'approval-123',
    action_type: 'refund',
    status: 'awaiting_approval',
    amount_cents: 123456,
    currency: 'CNY',
    resume_required: false,
    error_code: null,
    ...partial,
  }
}

describe('PendingApprovalCard 待审批卡片', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('展示动作类型中文标签、格式化金额与状态', async () => {
    // 保护行为：卡片必须结构化展示 action_type、金额（按分格式化）与状态
    const wrapper = mount(PendingApprovalCard, { props: { approval: approval() } })
    await flushPromises()

    expect(wrapper.text()).toContain('退款提案')
    expect(wrapper.text()).toContain('¥1,234.56')
    expect(wrapper.text()).toContain('CNY')
    expect(wrapper.text()).toContain('awaiting_approval')
  })

  it('补偿提案显示补偿标签与 ISO 币种金额', async () => {
    // 边界情况：action_type=compensation 与未知币种都要正确呈现
    const wrapper = mount(PendingApprovalCard, {
      props: { approval: approval({ action_type: 'compensation', currency: 'USD', amount_cents: 5000 }) },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('补偿提案')
    expect(wrapper.text()).toContain('$50.00')
  })

  it('展示 approval_id 与 run_id，并支持复制', async () => {
    // 保护行为：批准标识与 Run 标识可复制（审批中心开放前的可用操作）
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', {
      ...navigator,
      clipboard: { writeText },
    })
    const wrapper = mount(PendingApprovalCard, { props: { approval: approval() } })
    await flushPromises()

    expect(wrapper.text()).toContain('approval-123')
    expect(wrapper.text()).toContain('run-123')

    const buttons = wrapper.findAll('button')
    const copyApproval = buttons.find((button) => button.text() === '复制')
    await copyApproval?.trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('approval-123')
  })

  it('resume_required=true 且有 error_code 时展示恢复提示', async () => {
    // 保护行为：需要管理员恢复的提案必须给出明确提示与错误码
    const wrapper = mount(PendingApprovalCard, {
      props: {
        approval: approval({ resume_required: true, error_code: 'CHECKPOINT_UNAVAILABLE' }),
      },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('CHECKPOINT_UNAVAILABLE')
    expect(wrapper.text()).toContain('需要管理员恢复')
  })

  it('审批中心未开放说明与预留查看入口', async () => {
    // 保护行为：当前必须如实标注「审批中心将在下一阶段开放」，
    // 「查看审批」点击只发出预留事件，不跳向不存在的页面
    const wrapper = mount(PendingApprovalCard, { props: { approval: approval() } })
    await flushPromises()

    expect(wrapper.text()).toContain('审批中心将在下一阶段开放')

    await wrapper.find('[data-test="open-approval"]').trigger('click')
    expect(wrapper.emitted('open-detail')?.[0]).toEqual(['approval-123'])
  })
})
