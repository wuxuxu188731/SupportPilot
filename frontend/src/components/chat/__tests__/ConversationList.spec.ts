/*
 * 会话列表组件测试：列表渲染与高亮、空态、加载态、错误与重试、
 * 加载更多按钮、新建会话入口。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ConversationList from '@/components/chat/ConversationList.vue'
import type { ConversationListItem } from '@/api/types'

function conversation(id: string, title: string, updatedAt = '2026-09-06 08:00:00'): ConversationListItem {
  return {
    conversation_id: id,
    title,
    created_at: '2026-09-01 08:00:00',
    updated_at: updatedAt,
  }
}

function baseProps(overrides: Record<string, unknown> = {}) {
  return {
    conversations: [] as ConversationListItem[],
    loading: false,
    error: null as string | null,
    activeConversationId: null as string | null,
    hasMore: false,
    loadingMore: false,
    creating: false,
    ...overrides,
  }
}

describe('ConversationList 会话列表', () => {
  it('渲染会话并高亮当前选中项', async () => {
    // 保护行为：会话标题必须可见，当前会话高亮（覆盖：会话列表展示）
    const wrapper = mount(ConversationList, {
      props: baseProps({
        conversations: [conversation('conv-1', '订单问题'), conversation('conv-2', '退款咨询')],
        activeConversationId: 'conv-2',
      }),
    })
    await flushPromises()

    expect(wrapper.text()).toContain('订单问题')
    expect(wrapper.text()).toContain('退款咨询')
    const active = wrapper.find('.list-item.active')
    expect(active.exists()).toBe(true)
    expect(active.text()).toContain('退款咨询')
  })

  it('点击会话项发出 select 事件', async () => {
    // 保护行为：点击会话必须把会话 id 交给父组件导航
    const wrapper = mount(ConversationList, {
      props: baseProps({ conversations: [conversation('conv-1', '订单问题')] }),
    })
    await flushPromises()

    await wrapper.find('.list-item').trigger('click')

    expect(wrapper.emitted('select')?.[0]).toEqual(['conv-1'])
  })

  it('空列表显示空态并引导新建', async () => {
    // 保护行为：无会话时展示空态而不是空白，并给出新建入口
    const wrapper = mount(ConversationList, { props: baseProps() })
    await flushPromises()

    expect(wrapper.text()).toContain('暂无会话')
  })

  it('加载中显示加载状态', async () => {
    // 保护行为：首屏加载必须可感知
    const wrapper = mount(ConversationList, { props: baseProps({ loading: true }) })
    await flushPromises()

    expect(wrapper.text()).toContain('会话加载中')
  })

  it('加载失败显示错误与重试按钮', async () => {
    // 保护行为：失败必须可见，且重试可用（覆盖：错误和重试）
    const wrapper = mount(ConversationList, {
      props: baseProps({ error: '列表加载失败，请稍后再试' }),
    })
    await flushPromises()

    expect(wrapper.text()).toContain('列表加载失败，请稍后再试')
    const retry = wrapper.findAll('button').find((button) => button.text() === '重试')
    expect(retry).toBeDefined()
    await retry!.trigger('click')
    expect(wrapper.emitted('retry')).toBeTruthy()
  })

  it('hasMore 为真时显示「加载更多」按钮（不假装有总数）', async () => {
    // 边界情况：分页由「最近一页是否拉满」推断，按钮触发 loadMore
    const wrapper = mount(ConversationList, {
      props: baseProps({
        conversations: [conversation('conv-1', '会话')],
        hasMore: true,
      }),
    })
    await flushPromises()

    const loadMore = wrapper.findAll('button').find((button) => button.text().includes('加载更多'))
    expect(loadMore).toBeDefined()
    await loadMore!.trigger('click')
    expect(wrapper.emitted('loadMore')).toBeTruthy()
  })

  it('新建按钮触发 create 事件', async () => {
    // 保护行为：新建入口始终可达（覆盖：新建会话）
    const wrapper = mount(ConversationList, {
      props: baseProps({ conversations: [conversation('conv-1', '会话')] }),
    })
    await flushPromises()

    await wrapper.find('[data-test="new-conversation"]').trigger('click')
    expect(wrapper.emitted('create')).toBeTruthy()
  })
})
