/*
 * 知识引用列表测试：结构化 citations 展示文档标题/路径/正文/辅助标识，
 * 以及「查看原文位置」入口（结构化载荷、无偏移时降级、键盘可达）。
 * 测试数据来自结构化字段，不解析回答文本。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import CitationList from '@/components/chat/CitationList.vue'
import type { Citation, CitationTarget } from '@/api/types'

const CITATIONS: Citation[] = [
  {
    citation_id: 'C1',
    document_id: 'doc-1',
    version_id: 'ver-3',
    chunk_id: 'chunk-9',
    title: '退货与换货政策',
    heading_path: '2. 退货流程',
    content: '消费者可在签收后 7 日内申请退货。',
    start_offset: 120,
    end_offset: 138,
  },
]

/** 构造一条引用（默认带偏移，便于覆盖降级场景）。 */
function citation(partial: Partial<Citation> = {}): Citation {
  return { ...CITATIONS[0], ...partial }
}

describe('CitationList 引用列表', () => {
  it('渲染引用编号、文档标题与引用正文', async () => {
    // 保护行为：引用必须来自结构化 citations（覆盖测试 20）
    const wrapper = mount(CitationList, { props: { citations: CITATIONS } })
    await flushPromises()

    expect(wrapper.text()).toContain('[C1]')
    expect(wrapper.text()).toContain('退货与换货政策')
    expect(wrapper.text()).toContain('消费者可在签收后 7 日内申请退货。')
  })

  it('展示标题路径与文档/版本辅助标识', async () => {
    // 保护行为：heading_path 与文档/版本标识是可展示的辅助信息
    const wrapper = mount(CitationList, { props: { citations: CITATIONS } })
    await flushPromises()

    expect(wrapper.text()).toContain('2. 退货流程')
    expect(wrapper.text()).toContain('doc-1')
    expect(wrapper.text()).toContain('ver-3')
  })

  it('引用默认展开可见（证据默认呈现而非藏在折叠深处）', async () => {
    // 边界情况：引用卡片应默认可见，方便核对回答依据
    const wrapper = mount(CitationList, { props: { citations: CITATIONS } })
    await flushPromises()

    expect(wrapper.text()).toContain('消费者可在签收后 7 日内申请退货。')
  })

  it('heading_path 为 null 时不渲染章节行', async () => {
    // 边界情况：无章节路径的引用不能渲染空章节行
    const [first, ...rest] = CITATIONS
    const noHeading = [{ ...first, heading_path: null }]
    const wrapper = mount(CitationList, { props: { citations: [...noHeading, ...rest] } })
    await flushPromises()

    expect(wrapper.text()).not.toContain('章节：null')
  })

  it('anchorPrefix 使卡片 id 带回答作用域前缀（不传时保持原 id）', async () => {
    // 保护行为（设计 5.4）：同一页面多条回答各有 C1 时，卡片 id 必须按回答作用域
    // 区分（否则 id 重复，正文 [C1] 会跳到第一条回答的卡片）；
    // 不传 anchorPrefix 时退化为 citation-C1，保持既有用法与被外部指向的 id 可用
    const scoped = mount(CitationList, {
      props: { citations: CITATIONS, anchorPrefix: 'answer-7' },
    })
    await flushPromises()
    expect(scoped.find('.citation-card').attributes('id')).toBe('answer-7-citation-C1')
    // 引用渲染逻辑不变：编号、标题、正文照旧展示
    expect(scoped.text()).toContain('[C1]')
    expect(scoped.text()).toContain('消费者可在签收后 7 日内申请退货。')

    const legacy = mount(CitationList, { props: { citations: CITATIONS } })
    await flushPromises()
    expect(legacy.find('.citation-card').attributes('id')).toBe('citation-C1')
  })
})

describe('CitationList「查看原文位置」入口', () => {
  it('每条引用都有键盘可达的查看入口（button 而非 div）', async () => {
    // 保护行为：跳转正文是操作而不是展示，必须是可聚焦、可键盘触发的 button
    const wrapper = mount(CitationList, {
      props: { citations: [citation(), citation({ citation_id: 'C2' })] },
    })
    await flushPromises()

    const buttons = wrapper.findAll('button[data-test^="open-document-"]')
    expect(buttons).toHaveLength(2)
    expect(buttons[0].element.tagName).toBe('BUTTON')
    expect(buttons[0].text()).toBe('查看原文位置')
  })

  it('emit 的载荷是结构化字段，不从回答文本或引用正文解析', async () => {
    // 保护行为：跳转载荷必须逐字段取自 citations（文档、版本、偏移、标题路径）
    const wrapper = mount(CitationList, { props: { citations: CITATIONS } })
    await flushPromises()

    await wrapper.find('[data-test="open-document-C1"]').trigger('click')

    const emitted = wrapper.emitted('openDocument')
    expect(emitted).toHaveLength(1)
    expect(emitted?.[0]?.[0]).toEqual({
      documentId: 'doc-1',
      versionId: 'ver-3',
      startOffset: 120,
      endOffset: 138,
      headingPath: '2. 退货流程',
      title: '退货与换货政策',
    } satisfies CitationTarget)
  })

  it('偏移为 null 时入口仍可用，但不再声称"定位"', async () => {
    // 边界情况：后端允许偏移为空（决策 2），此时降级为「查看来源文档」
    const wrapper = mount(CitationList, {
      props: { citations: [citation({ start_offset: null, end_offset: null })] },
    })
    await flushPromises()

    const button = wrapper.find('[data-test="open-document-C1"]')
    expect(button.text()).toBe('查看来源文档')

    await button.trigger('click')
    const payload = wrapper.emitted('openDocument')?.[0]?.[0] as CitationTarget
    expect(payload.startOffset).toBeNull()
    expect(payload.endOffset).toBeNull()
    // 标题路径仍然带上，供查看器回退到所属章节
    expect(payload.headingPath).toBe('2. 退货流程')
  })

  it('旧载荷缺少偏移字段时同样按降级处理（undefined 等价 null）', async () => {
    // 边界情况：历史载荷不含两个偏移字段，`== null` 判断必须同时覆盖 undefined
    const legacy = citation()
    delete legacy.start_offset
    delete legacy.end_offset
    const wrapper = mount(CitationList, { props: { citations: [legacy] } })
    await flushPromises()

    expect(wrapper.find('[data-test="open-document-C1"]').text()).toBe('查看来源文档')
  })

  it('偏移为 0 时仍视为可精确定位（不得用取反判断）', async () => {
    // 边界情况：片段恰好在正文开头时 start_offset === 0；
    // 用 !start_offset 判断会把合法定位误判成无偏移
    const wrapper = mount(CitationList, {
      props: { citations: [citation({ start_offset: 0, end_offset: 12 })] },
    })
    await flushPromises()

    expect(wrapper.find('[data-test="open-document-C1"]').text()).toBe('查看原文位置')
    await wrapper.find('[data-test="open-document-C1"]').trigger('click')
    const payload = wrapper.emitted('openDocument')?.[0]?.[0] as CitationTarget
    expect(payload.startOffset).toBe(0)
  })
})
