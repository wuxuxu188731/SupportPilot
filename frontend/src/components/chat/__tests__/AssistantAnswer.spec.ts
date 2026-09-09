/*
 * Agent 回答展示组件测试：纯文本展示、空回答降级、
 * answer_incomplete 警告、引用标记分段与正文不伪造 HTML。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import AssistantAnswer from '@/components/chat/AssistantAnswer.vue'

describe('AssistantAnswer 回答展示', () => {
  it('展示回答正文并保留换行（纯文本，不渲染 HTML）', async () => {
    // 保护行为：回答必须以安全纯文本渲染，正文中的尖括号不得被解释为标签
    const wrapper = mount(AssistantAnswer, {
      props: { content: '第一行\n第二行 <script>alert(1)</script>' },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('第一行')
    expect(wrapper.text()).toContain('<script>alert(1)</script>')
    expect(wrapper.find('.answer-text').element.innerHTML).not.toContain('<script>')
  })

  it('空回答显示降级提示', async () => {
    // 边界情况：null/空串回答必须给出明确降级文案，不能留白
    const wrapper = mount(AssistantAnswer, { props: { content: null } })
    await flushPromises()
    expect(wrapper.text()).toContain('未生成可展示的回答文本')
  })

  it('answer_incomplete=true 时显示谨慎采纳警告', async () => {
    // 保护行为：证据可能不完整的回答必须有明显但不惊吓的警告
    const wrapper = mount(AssistantAnswer, {
      props: { content: '回答', answerIncomplete: true },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('证据引用可能不完整')
  })

  it('answer_incomplete=false 时不显示警告', async () => {
    // 边界情况：正常回答不能出现不完整警告
    const wrapper = mount(AssistantAnswer, {
      props: { content: '回答', answerIncomplete: false },
    })
    await flushPromises()
    expect(wrapper.text()).not.toContain('证据引用可能不完整')
  })

  it('启用引用链接时正文中的 [C1] 分段为可点击标记', async () => {
    // 保护行为：启用 citationLinks 时 [C1] 变成引用跳转标记而非纯文本
    const wrapper = mount(AssistantAnswer, {
      props: { content: '依据政策 [C1] 处理', citationLinks: true },
    })
    await flushPromises()

    const ref = wrapper.find('.citation-ref')
    expect(ref.exists()).toBe(true)
    expect(ref.text()).toContain('C1')
  })

  it('未启用引用链接时正文原样展示，不生成引用标记', async () => {
    // 边界情况：历史纯文本（无结构化引用）不得生成假的 [C1] 链接
    const wrapper = mount(AssistantAnswer, {
      props: { content: '依据政策 [C1] 处理', citationLinks: false },
    })
    await flushPromises()
    expect(wrapper.find('.citation-ref').exists()).toBe(false)
    expect(wrapper.find('.answer-text').text()).toContain('[C1]')
  })
})
