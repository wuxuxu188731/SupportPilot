/*
 * Markdown 内容组件测试：富文本渲染、空文本渲染、引用标记点击定位、
 * 以及 v-html 输出不含可执行标记的安全边界。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import MarkdownContent from '@/components/common/MarkdownContent.vue'

describe('MarkdownContent 渲染', () => {
  it('渲染 Markdown 富文本而非语法源码', async () => {
    // 保护行为：Agent 消息正文必须以富文本展示，不能直接暴露 md 语法
    const wrapper = mount(MarkdownContent, {
      props: { content: '## 处理结果\n\n- 已受理\n- 预计 3 天\n' },
    })
    await flushPromises()

    expect(wrapper.find('h2').text()).toBe('处理结果')
    expect(wrapper.findAll('li')).toHaveLength(2)
    expect(wrapper.text()).not.toContain('##')
  })

  it('空文本渲染为空容器（降级文案由父组件负责）', async () => {
    // 边界情况：空内容不产生多余节点，也不抛错
    const wrapper = mount(MarkdownContent, { props: { content: '' } })
    await flushPromises()

    expect(wrapper.find('[data-test="markdown-body"]').element.innerHTML).toBe('')
  })

  it('源码内联 HTML 以纯文本展示', async () => {
    // 安全边界：v-html 的输出不得来自回答文本的原始标签
    const wrapper = mount(MarkdownContent, {
      props: { content: '正文 <script>alert(1)</script>' },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('<script>alert(1)</script>')
    expect(wrapper.element.innerHTML).not.toContain('<script>')
  })

  it('citations=false 时不生成引用标记元素', async () => {
    // 边界情况：历史消息无结构化引用，[C1] 只作为普通文本展示
    const wrapper = mount(MarkdownContent, {
      props: { content: '依据政策 [C1] 处理', citations: false },
    })
    await flushPromises()

    expect(wrapper.find('.citation-ref').exists()).toBe(false)
    expect(wrapper.text()).toContain('[C1]')
  })
})

describe('MarkdownContent 引用标记交互', () => {
  /** 构造一个引用卡片桩节点，用于断言定位行为。 */
  function mountCitationCard(): { card: HTMLElement; scrollIntoView: ReturnType<typeof vi.fn> } {
    const card = document.createElement('div')
    card.id = 'citation-C1'
    card.tabIndex = -1
    const scrollIntoView = vi.fn()
    card.scrollIntoView = scrollIntoView
    document.body.appendChild(card)
    return { card, scrollIntoView }
  }

  it('点击引用标记滚动并聚焦到对应引用卡片', async () => {
    // 保护行为：正文 [C1] 必须能定位到引用卡片，而不是纯装饰
    const { card, scrollIntoView } = mountCitationCard()
    const wrapper = mount(MarkdownContent, {
      props: { content: '依据政策 [C1] 处理', citations: true },
    })
    await flushPromises()

    await wrapper.find('.citation-ref').trigger('click')

    expect(scrollIntoView).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(card)
    card.remove()
  })

  it('回车键触发引用定位（键盘可达）', async () => {
    // 保护行为：引用标记是可聚焦元素，键盘用户必须能触发定位
    const { card, scrollIntoView } = mountCitationCard()
    const wrapper = mount(MarkdownContent, {
      props: { content: '依据政策 [C1] 处理', citations: true },
    })
    await flushPromises()

    await wrapper.find('.citation-ref').trigger('keydown.enter')

    expect(scrollIntoView).toHaveBeenCalledTimes(1)
    card.remove()
  })

  it('引用卡片不存在时点击不抛错', async () => {
    // 边界情况：引用卡片可能尚未渲染，点击必须安全降级
    const wrapper = mount(MarkdownContent, {
      props: { content: '依据政策 [C9] 处理', citations: true },
    })
    await flushPromises()

    await expect(wrapper.find('.citation-ref').trigger('click')).resolves.not.toThrow()
  })

  it('带 citationAnchorPrefix 时按前缀定位到对应回答的卡片', async () => {
    // 保护行为（设计 5.4）：同一页面多条回答都含 C1 时，必须先拼回答作用域前缀
    // 再查找卡片；否则 getElementById 只返回第一个，会跳到别条回答的卡片
    const legacyCard = document.createElement('div')
    legacyCard.id = 'citation-C1'
    const legacyScroll = vi.fn()
    legacyCard.scrollIntoView = legacyScroll
    document.body.appendChild(legacyCard)

    const scopedCard = document.createElement('div')
    scopedCard.id = 'answer-9-citation-C1'
    scopedCard.tabIndex = -1
    const scopedScroll = vi.fn()
    scopedCard.scrollIntoView = scopedScroll
    document.body.appendChild(scopedCard)

    const wrapper = mount(MarkdownContent, {
      props: {
        content: '依据政策 [C1] 处理',
        citations: true,
        citationAnchorPrefix: 'answer-9',
      },
    })
    await flushPromises()

    await wrapper.find('.citation-ref').trigger('click')

    expect(scopedScroll).toHaveBeenCalledTimes(1)
    expect(legacyScroll).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(scopedCard)
    legacyCard.remove()
    scopedCard.remove()
  })
})
