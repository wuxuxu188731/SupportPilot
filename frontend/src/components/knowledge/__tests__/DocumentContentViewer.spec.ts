/*
 * 正文查看器组件测试：加载/错误/空态、安全渲染、必须上屏的提示、
 * 偏移高亮与降级、目录跳转、同一版本不重复请求。
 *
 * 通过 mock @/api/knowledge 进行，不触达网络；正文渲染走真实 markdown 渲染器，
 * 因此能同时覆盖"偏移定位 + 渲染结果"这条真实链路。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/knowledge', () => ({
  getDocumentVersionContent: vi.fn(),
  getKnowledgeDocumentDetail: vi.fn(),
}))

import { getDocumentVersionContent, getKnowledgeDocumentDetail } from '@/api/knowledge'
import type { DocumentContentResponse } from '@/api/knowledgeTypes'
import DocumentContentViewer from '@/components/knowledge/DocumentContentViewer.vue'
import { clearDocumentContentCache } from '@/utils/documentContentCache'

/** 正文文本：包含标题、加粗与列表，用于覆盖目录与偏移标记对齐 */
const TEXT = '# 退货总则\n\n签收后 7 日内可申请**退货**，超期不再受理。\n\n- 需保持商品完好\n'
/** 构造正文响应（字段与后端 DocumentContentResponse 一一对应）。 */
function contentResponse(partial: Partial<DocumentContentResponse> = {}): DocumentContentResponse {
  return {
    document_id: 'doc-1',
    version_id: 'ver-3',
    version_no: 3,
    title: '退货与换货政策',
    source_type: 'markdown',
    status: 'active',
    active_version_id: 'ver-3',
    loader_version: 'loader-v1',
    chunker_version: 'chunker-v1',
    content_hash: 'sha256:abc',
    text: TEXT,
    text_length: TEXT.length,
    outline: [{ level: 1, title: '退货总则', heading_path: '退货总则', char_offset: 0 }],
    ...partial,
  }
}

/** 挂载查看器（默认无偏移，即"从知识库详情页查看正文"的形态）。 */
function mountViewer(props: Record<string, unknown> = {}) {
  return mount(DocumentContentViewer, {
    props: { documentId: 'doc-1', versionId: 'ver-3', ...props },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  // 正文缓存是模块级（跨组件实例复用），用例之间必须显式清空，避免互相污染
  clearDocumentContentCache()
  // jsdom 未实现 scrollIntoView / scrollTo：替换为可断言的桩，避免抛异常
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.scrollTo = vi.fn()
  vi.mocked(getKnowledgeDocumentDetail).mockResolvedValue({
    document_id: 'doc-1',
    title: '退货与换货政策',
    source_type: 'markdown',
    status: 'active',
    active_version_id: 'ver-9',
    created_at: '2026-01-01T00:00:00+00:00',
    updated_at: '2026-01-01T00:00:00+00:00',
    versions: [
      {
        version_id: 'ver-9',
        version_no: 9,
        content_hash: 'sha256:def',
        loader_version: 'loader-v1',
        chunker_version: 'chunker-v1',
        embedding_model: 'text-embedding-v3',
        embedding_dimensions: 1024,
        created_at: '2026-01-01T00:00:00+00:00',
      },
    ],
    latest_job: null,
  } as never)
})

describe('DocumentContentViewer 三态与安全渲染', () => {
  it('加载中展示加载态，加载完成后渲染正文', async () => {
    // 保护行为：正文拉取期间必须有 loading 反馈，完成后渲染的是接口返回的正文
    let resolve: (value: DocumentContentResponse) => void = () => {}
    vi.mocked(getDocumentVersionContent).mockReturnValue(
      new Promise<DocumentContentResponse>((done) => {
        resolve = done
      }),
    )
    const wrapper = mountViewer()
    await flushPromises()

    expect(wrapper.find('[data-test="viewer-loading"]').exists()).toBe(true)

    resolve(contentResponse())
    await flushPromises()

    expect(wrapper.find('[data-test="viewer-loading"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="viewer-body"]').text()).toContain('签收后 7 日内可申请退货')
  })

  it('加载失败展示错误与重试按钮，点重试会重新请求', async () => {
    // 保护行为：正文失败不影响调用方（对话区仍保留引用卡片），面板内可重试
    vi.mocked(getDocumentVersionContent).mockRejectedValueOnce(new Error('503'))
    const wrapper = mountViewer()
    await flushPromises()

    expect(wrapper.find('[data-test="viewer-error"]').exists()).toBe(true)

    vi.mocked(getDocumentVersionContent).mockResolvedValueOnce(contentResponse())
    await wrapper.find('[data-test="viewer-retry"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="viewer-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="viewer-body"]').text()).toContain('签收后 7 日内可申请退货')
    expect(getDocumentVersionContent).toHaveBeenCalledTimes(2)
  })

  it('空正文展示空态文案，不渲染正文区域', async () => {
    // 边界情况：版本存在但正文为空时给出明确空态，而不是空白面板
    vi.mocked(getDocumentVersionContent).mockResolvedValue(
      contentResponse({ text: '   \n', text_length: 4, outline: [] }),
    )
    const wrapper = mountViewer()
    await flushPromises()

    expect(wrapper.find('[data-test="viewer-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="viewer-body"]').exists()).toBe(false)
  })

  it('正文里的脚本标签被转义为纯文本（复用安全渲染器，不直出 HTML）', async () => {
    // 安全边界：正文来自上传文件，属于不可信输入，不得生成可执行标签
    vi.mocked(getDocumentVersionContent).mockResolvedValue(
      contentResponse({ text: '正文 <script>alert(1)</script>', outline: [] }),
    )
    const wrapper = mountViewer()
    await flushPromises()

    expect(wrapper.find('[data-test="viewer-body"]').html()).not.toContain('<script>')
    expect(wrapper.find('[data-test="viewer-body"]').text()).toContain('<script>alert(1)</script>')
  })
})

describe('DocumentContentViewer 必须上屏的提示', () => {
  it('pdf/word 文档不再出现「PDF/Word 转换」提示', async () => {
    // 产品决定（本次调整）：来源转换属于用户已知事实，不再每次查看正文都上屏。
    // 该用例防止提示被无意加回，同时保护正文本身仍按转换后的 Markdown 正常渲染。
    vi.mocked(getDocumentVersionContent).mockResolvedValue(
      contentResponse({ source_type: 'pdf' }),
    )
    const wrapper = mountViewer()
    await flushPromises()

    expect(wrapper.find('[data-test="viewer-converted-notice"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="viewer-body"]').text()).toContain('签收后 7 日内可申请退货')
  })

  it('引用版本不是当前有效版本时提示两个版本号', async () => {
    // 保护行为：历史版本必须显著提示，且给出当前有效版本号（设计稿 4.8 第 2 条）
    vi.mocked(getDocumentVersionContent).mockResolvedValue(
      contentResponse({ version_id: 'ver-3', version_no: 3, active_version_id: 'ver-9' }),
    )
    const wrapper = mountViewer()
    await flushPromises()

    const notice = wrapper.find('[data-test="viewer-history-notice"]')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('v3')
    expect(notice.text()).toContain('v9')
  })

  it('文档被停用时提示不再参与检索，但正文仍可读', async () => {
    // 保护行为：停用文档的正文仍要能看（历史引用可能指向它），并说明停用状态
    vi.mocked(getDocumentVersionContent).mockResolvedValue(
      contentResponse({ status: 'disabled' }),
    )
    const wrapper = mountViewer()
    await flushPromises()

    expect(wrapper.find('[data-test="viewer-disabled-notice"]').text()).toContain('已停用')
    expect(wrapper.find('[data-test="viewer-body"]').text()).toContain('签收后')
  })
})

describe('DocumentContentViewer 偏移定位与降级', () => {
  it('传入偏移时高亮对应片段并滚动到可见位置', async () => {
    // 保护行为：引用跳转的核心能力——按偏移精确高亮，且只高亮一处
    vi.mocked(getDocumentVersionContent).mockResolvedValue(contentResponse())
    const start = TEXT.indexOf('签收后')
    const end = TEXT.indexOf('，超期')
    const wrapper = mountViewer({ startOffset: start, endOffset: end })
    await flushPromises()

    const highlights = wrapper.findAll('.sp-range-highlight')
    expect(highlights).toHaveLength(1)
    expect(highlights[0].text()).toBe('签收后 7 日内可申请退货')
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
    expect(wrapper.find('[data-test="viewer-fallback-notice"]').exists()).toBe(false)
  })

  it('偏移为空时展示降级文案，不抛异常也不高亮', async () => {
    // 边界情况：决策 2 允许偏移为空，此时只打开文档并说明未记录精确位置
    vi.mocked(getDocumentVersionContent).mockResolvedValue(contentResponse())
    const wrapper = mountViewer({ startOffset: null, endOffset: null })
    await flushPromises()

    expect(wrapper.find('[data-test="viewer-fallback-notice"]').exists()).toBe(true)
    expect(wrapper.findAll('.sp-range-highlight')).toHaveLength(0)
    expect(wrapper.find('[data-test="viewer-body"]').text()).toContain('签收后 7 日内可申请退货')
  })

  it('偏移越界时降级为只打开文档，不抛异常', async () => {
    // 边界情况：偏移与正文不同源（或正文已变更）时不能猜位置，必须降级
    vi.mocked(getDocumentVersionContent).mockResolvedValue(contentResponse())
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const wrapper = mountViewer({ startOffset: 5, endOffset: TEXT.length + 100 })
    await flushPromises()

    expect(wrapper.find('[data-test="viewer-fallback-notice"]').exists()).toBe(true)
    expect(wrapper.findAll('.sp-range-highlight')).toHaveLength(0)
    // 诊断只进控制台，不上屏
    expect(warn).toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('out-of-range')
    warn.mockRestore()
  })

  it('偏移不可用但引用带 heading_path 时滚动到对应章节', async () => {
    // 降级路径：偏移为空且引用来自某个章节时，优先滚到目录里的该章节
    vi.mocked(getDocumentVersionContent).mockResolvedValue(
      contentResponse({
        text: '# 第一章\n\n内容一\n\n## 第二章\n\n内容二\n',
        outline: [
          { level: 1, title: '第一章', heading_path: '第一章', char_offset: 0 },
          { level: 2, title: '第二章', heading_path: '第一章/第二章', char_offset: 12 },
        ],
      }),
    )
    const wrapper = mountViewer({
      startOffset: null,
      endOffset: null,
      headingPath: '第一章/第二章',
    })
    await flushPromises()

    const outlineButtons = wrapper.findAll('.doc-outline-item')
    expect(outlineButtons).toHaveLength(2)
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
  })
})

describe('DocumentContentViewer 目录与请求去重', () => {
  it('outline 非空时渲染目录，点击目录项滚动到对应标题', async () => {
    // 保护行为：目录来自服务端 outline（前端不自行解析标题），点击可跳转
    vi.mocked(getDocumentVersionContent).mockResolvedValue(contentResponse())
    const wrapper = mountViewer()
    await flushPromises()

    const outline = wrapper.find('[data-test="viewer-outline"]')
    expect(outline.exists()).toBe(true)
    const items = wrapper.findAll('.doc-outline-item')
    expect(items).toHaveLength(1)
    expect(items[0].text()).toBe('退货总则')

    vi.mocked(Element.prototype.scrollIntoView).mockClear()
    await items[0].trigger('click')
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
  })

  it('outline 为空时不渲染目录区域', async () => {
    // 边界情况：没有标题的文档不应出现空目录
    vi.mocked(getDocumentVersionContent).mockResolvedValue(contentResponse({ outline: [] }))
    const wrapper = mountViewer()
    await flushPromises()

    expect(wrapper.find('[data-test="viewer-outline"]').exists()).toBe(false)
  })

  it('同一 (document_id, version_id) 只请求一次，切回同一版本不再请求', async () => {
    // 保护行为：面板内同一版本只请求一次；切换引用到同一版本只重新定位
    vi.mocked(getDocumentVersionContent).mockResolvedValue(contentResponse())
    const wrapper = mountViewer()
    await flushPromises()
    expect(getDocumentVersionContent).toHaveBeenCalledTimes(1)

    await wrapper.setProps({ versionId: 'ver-3' })
    await flushPromises()
    expect(getDocumentVersionContent).toHaveBeenCalledTimes(1)

    // 换到另一个版本会请求；再切回来读缓存
    vi.mocked(getDocumentVersionContent).mockResolvedValue(
      contentResponse({ version_id: 'ver-4', version_no: 4, active_version_id: 'ver-4' }),
    )
    await wrapper.setProps({ versionId: 'ver-4' })
    await flushPromises()
    expect(getDocumentVersionContent).toHaveBeenCalledTimes(2)

    await wrapper.setProps({ versionId: 'ver-3' })
    await flushPromises()
    expect(getDocumentVersionContent).toHaveBeenCalledTimes(2)
  })

  it('切换引用偏移量只重新定位，不重新请求正文', async () => {
    // 保护行为：同版本内多条引用之间跳转不应产生新的正文请求
    vi.mocked(getDocumentVersionContent).mockResolvedValue(contentResponse())
    const wrapper = mountViewer({ startOffset: 0, endOffset: 4 })
    await flushPromises()
    expect(getDocumentVersionContent).toHaveBeenCalledTimes(1)

    const second = TEXT.indexOf('需保持商品完好')
    await wrapper.setProps({ startOffset: second, endOffset: second + '需保持商品完好'.length })
    await flushPromises()

    expect(getDocumentVersionContent).toHaveBeenCalledTimes(1)
    const highlights = wrapper.findAll('.sp-range-highlight')
    expect(highlights).toHaveLength(1)
    // 高亮跨到列表项结尾时会带上块级分隔换行，比较时忽略空白
    expect(highlights[0].text().replace(/\s+/g, '')).toBe('需保持商品完好')
  })
})
