/*
 * Agent 处理过程时间线测试：默认折叠、展开显示工具事件与耗时、
 * 失败事件的错误展示、工具原始参数/结果不直接渲染。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import AgentEventTimeline from '@/components/chat/AgentEventTimeline.vue'
import type { AgentEvent } from '@/api/types'

function event(partial: Partial<AgentEvent> & { type: AgentEvent['type'] }): AgentEvent {
  return {
    timestamp: '2026-09-06T10:00:00.000000',
    tool_call_id: 'call-1',
    tool_call_name: 'search_knowledge',
    tool_call_arguments: {},
    result: null,
    error: null,
    duration_ms: null,
    ...partial,
  }
}

const EVENTS: AgentEvent[] = [
  event({ type: 'tool_call.requested', tool_call_name: 'search_knowledge' }),
  event({ type: 'tool_call.completed', tool_call_name: 'search_knowledge', duration_ms: 128 }),
  event({ type: 'tool_call.failed', tool_call_name: 'get_order', error: 'TOOL_EXECUTION_FAILED' }),
]

/** 展开折叠面板（点击自定义切换按钮）。 */
async function expandTimeline(wrapper: ReturnType<typeof mount>): Promise<void> {
  await wrapper.find('[data-test="events-toggle"]').trigger('click')
  await flushPromises()
}

describe('AgentEventTimeline 处理过程', () => {
  it('默认折叠：展开前不展示事件明细', async () => {
    // 保护行为：处理过程默认折叠，不抢占主内容注意力（覆盖测试 23）
    const wrapper = mount(AgentEventTimeline, { props: { events: EVENTS } })
    await flushPromises()

    expect(wrapper.text()).toContain('处理过程（3 个事件）')
    expect(wrapper.find('.event-item').exists()).toBe(false)
  })

  it('展开后展示工具名、事件类型与耗时', async () => {
    // 保护行为：展开后按事件顺序展示用户可理解的工具执行信息
    const wrapper = mount(AgentEventTimeline, { props: { events: EVENTS } })
    await flushPromises()

    await expandTimeline(wrapper)

    expect(wrapper.text()).toContain('知识库检索')
    expect(wrapper.text()).toContain('128 ms')
    expect(wrapper.text()).toContain('工具完成')
  })

  it('失败事件展示安全错误信息并标记失败', async () => {
    // 保护行为：失败事件必须展示错误文本（安全错误码/消息），便于定位
    const wrapper = mount(AgentEventTimeline, { props: { events: EVENTS } })
    await flushPromises()

    await expandTimeline(wrapper)

    expect(wrapper.text()).toContain('工具失败')
    expect(wrapper.text()).toContain('TOOL_EXECUTION_FAILED')
    expect(wrapper.find('.event-item.failure').exists()).toBe(true)
  })

  it('不直接渲染工具原始参数与结果 JSON', async () => {
    // 安全边界：事件时间线默认不得暴露 tool_call_arguments/result 原始内容
    const withSecrets = [
      event({
        type: 'tool_call.completed',
        tool_call_arguments: { order_id: 'secret-order' },
        result: { internal_flag: 'secret-result' },
      }),
    ]
    const wrapper = mount(AgentEventTimeline, { props: { events: withSecrets } })
    await flushPromises()
    await expandTimeline(wrapper)

    expect(wrapper.text()).not.toContain('secret-order')
    expect(wrapper.text()).not.toContain('secret-result')
    // 同时声明「不展示原始参数」的界面说明存在
    expect(wrapper.text()).toContain('工具参数与原始结果属于内部数据')
  })
})
