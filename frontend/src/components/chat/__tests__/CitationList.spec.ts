/*
 * 知识引用列表测试：结构化 citations 展示文档标题/路径/正文/辅助标识。
 * 测试数据来自结构化字段，不解析回答文本。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import CitationList from '@/components/chat/CitationList.vue'
import type { Citation } from '@/api/types'

const CITATIONS: Citation[] = [
  {
    citation_id: 'C1',
    document_id: 'doc-1',
    version_id: 'ver-3',
    chunk_id: 'chunk-9',
    title: '退货与换货政策',
    heading_path: '2. 退货流程',
    content: '消费者可在签收后 7 日内申请退货。',
  },
]

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
})
