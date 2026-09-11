/*
 * Agent 回答展示组件测试：Markdown 富文本渲染、空回答降级、
 * answer_incomplete 警告、引用标记分段与正文不执行回答文本中的 HTML。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import AssistantAnswer from '@/components/chat/AssistantAnswer.vue'

describe('AssistantAnswer 回答展示', () => {
  it('回答正文按 Markdown 渲染，而不是展示语法源码', async () => {
    // 保护行为：Agent 回答是 Markdown 文本，必须渲染成富文本，
    // 不能把 ### / ** / - 等语法符号直接暴露给用户
    const wrapper = mount(AssistantAnswer, {
      props: { content: '### 处理结果\n\n**已受理**\n\n- 预计 3 天到账' },
    })
    await flushPromises()

    expect(wrapper.find('h3').text()).toBe('处理结果')
    expect(wrapper.find('strong').text()).toBe('已受理')
    expect(wrapper.findAll('li')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('###')
    expect(wrapper.text()).not.toContain('**')
  })

  it('回答正文中的换行保留展示', async () => {
    // 边界情况：Markdown 单换行在聊天场景下必须换行展示，不能合并成一行
    const wrapper = mount(AssistantAnswer, {
      props: { content: '第一行\n第二行' },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('第一行')
    expect(wrapper.text()).toContain('第二行')
    expect(wrapper.find('.answer-text').element.innerHTML).toContain('<br>')
  })

  it('正文中的尖括号文本不得被解释为标签（Markdown 渲染下的安全边界）', async () => {
    // 安全边界：Markdown 渲染不能以牺牲安全为代价，
    // <script> 只能作为转义后的纯文本出现在页面上
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
    expect(ref.attributes('data-citation-id')).toBe('C1')
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
