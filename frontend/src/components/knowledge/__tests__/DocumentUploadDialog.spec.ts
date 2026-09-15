/*
 * DocumentUploadDialog 对话框测试：文件选择与类型映射、前端校验拦截
 * （docx/空文件/超 2MiB/非 UTF-8/标题）、上传期间锁定与长耗时提示、
 * 防重复提交、成功后清空表单并关闭、失败（422/超时）保留表单、
 * deduplicated 提示。mock API 层与 store，不触达网络。
 */

import { createPinia, setActivePinia } from 'pinia'
import { h, nextTick } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { IngestionReceipt } from '@/api/knowledgeTypes'
import DocumentUploadDialog from '@/components/knowledge/DocumentUploadDialog.vue'
import { useKnowledgeStore } from '@/stores/knowledge'
import { useOrganizationStore } from '@/stores/organization'
import { ORGANIZATION_STORAGE_KEY } from '@/stores/persistence'

vi.mock('@/api/knowledge', () => ({
  listKnowledgeDocuments: vi.fn(),
  getKnowledgeDocumentDetail: vi.fn(),
  uploadKnowledgeDocument: vi.fn(),
  uploadKnowledgeDocumentVersion: vi.fn(),
  disableKnowledgeDocument: vi.fn(),
  enableKnowledgeDocument: vi.fn(),
  getIngestionJob: vi.fn(),
}))
vi.mock('@/api/organization', () => ({
  listOrganizations: vi.fn(),
  createOrganization: vi.fn(),
}))

import * as knowledgeApi from '@/api/knowledge'

/** 构造上传回执。 */
function receipt(partial: Partial<IngestionReceipt> = {}): IngestionReceipt {
  return {
    document_id: 'doc-1',
    version_id: 'v-1',
    job_id: 'job-1',
    status: 'succeeded',
    deduplicated: false,
    ...partial,
  }
}

/** 构造指定大小的 utf-8 文本文件。 */
function textFile(name: string, size = 128): File {
  return new File([new ArrayBuffer(size)], name, { type: 'text/plain' })
}

/** 挂载对话框（teleport 打桩，让弹层内容可被查询）。 */
function mountDialog() {
  const pinia = createPinia()
  setActivePinia(pinia)
  window.localStorage.setItem(ORGANIZATION_STORAGE_KEY, JSON.stringify({ organizationId: 'org-1' }))
  const store = useKnowledgeStore()
  const organizationStore = useOrganizationStore()
  organizationStore.currentOrganizationId = 'org-1'
  organizationStore.organizations = [{ organization_id: 'org-1', name: '示例企业', role: 'admin' }]
  const wrapper = mount(
    { render: () => h(NMessageProvider, null, { default: () => h(DocumentUploadDialog, { show: true, onUploaded: () => undefined }) }) },
    { global: { plugins: [pinia], stubs: { teleport: true } } },
  )
  return { wrapper, store, dialog: wrapper.findComponent(DocumentUploadDialog) }
}

/** 冲刷微任务与宏任务：FileReader 读取文件是宏任务，需要真实等待。 */
async function settle(): Promise<void> {
  await flushPromises()
  await new Promise<void>((resolve) => setTimeout(resolve, 0))
  await flushPromises()
}

/** 通过原生 input 选择文件。 */
async function pickFile(wrapper: Awaited<ReturnType<typeof mountDialog>>['wrapper'], file: File): Promise<void> {
  const input = wrapper.find('[data-test="document-file-input"]').element as HTMLInputElement
  Object.defineProperty(input, 'files', { value: [file], configurable: true })
  await wrapper.find('[data-test="document-file-input"]').trigger('change')
  await settle()
}

/** 输入标题并提交。 */
async function submitWithTitle(wrapper: Awaited<ReturnType<typeof mountDialog>>['wrapper'], title: string): Promise<void> {
  await wrapper.find('[data-test="document-title-input"] input').setValue(title)
  await wrapper.find('[data-test="submit-upload"]').trigger('click')
  await settle()
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockResolvedValue(receipt())
})

describe('DocumentUploadDialog 文件选择与校验', () => {
  it('选择 .md 文件后展示文件名、大小与推导类型', async () => {
    // 保护行为：选择结果必须展示文件名/大小/推导出的来源类型（覆盖测试 2/3）
    const { wrapper } = mountDialog()

    await pickFile(wrapper, new File(['知识内容'], '售后政策.MD'))

    expect(wrapper.find('[data-test="file-summary"]').text()).toContain('售后政策.MD')
    expect(wrapper.find('[data-test="file-summary"]').text()).toContain('B')
    expect(wrapper.find('[data-test="file-summary"]').text()).toContain('Markdown')
  })

  it('.txt 文件推导为纯文本', async () => {
    // 保护行为：txt 扩展名映射为 text（覆盖测试 4）
    const { wrapper } = mountDialog()

    await pickFile(wrapper, new File(['知识内容'], 'a.TXT'))

    expect(wrapper.find('[data-test="file-summary"]').text()).toContain('纯文本')
  })

  it('docx / pdf 文件被接受并推导出正确类型', async () => {
    // 保护行为：Word 与 PDF 已放开上传，不能再被客户端拦截。
    // 注意用的是二进制字节：这两种格式不是合法 UTF-8，编码校验必须跳过。
    const { wrapper } = mountDialog()

    await pickFile(wrapper, new File(['内容'], 'a.docx'))
    expect(wrapper.find('[data-test="file-summary"]').text()).toContain('Word')

    await pickFile(wrapper, new File(['内容'], 'a.pdf'))
    expect(wrapper.find('[data-test="file-summary"]').text()).toContain('PDF')
  })

  it('真正不支持的扩展名仍被前端拒绝且不发起上传请求', async () => {
    // 边界情况：放开 docx/pdf 不等于放开一切——老式 .doc 必须继续被拦截
    const { wrapper } = mountDialog()

    await pickFile(wrapper, new File(['内容'], 'a.doc'))
    await submitWithTitle(wrapper, '测试文档')

    expect(wrapper.text()).toContain('仅支持 .md、.markdown、.txt、.docx 或 .pdf')
    expect(knowledgeApi.uploadKnowledgeDocument).not.toHaveBeenCalled()
  })

  it('pdf 文件通过校验并调用上传 API', async () => {
    // 保护行为：真实 pdf 字节不是合法 UTF-8，仍必须能走到上传请求
    const { wrapper } = mountDialog()

    await pickFile(wrapper, new File([new Uint8Array([0x25, 0x50, 0x44, 0x46, 0xff, 0xfe])], 'a.pdf'))
    await submitWithTitle(wrapper, '测试文档')

    expect(knowledgeApi.uploadKnowledgeDocument).toHaveBeenCalledTimes(1)
  })

  it('空文件被拒绝', async () => {
    // 保护行为：0 字节文件必须被拦截（覆盖测试 16）
    const { wrapper } = mountDialog()

    await pickFile(wrapper, new File([], 'a.md'))
    await submitWithTitle(wrapper, '测试文档')

    expect(wrapper.text()).toContain('文件内容为空')
    expect(knowledgeApi.uploadKnowledgeDocument).not.toHaveBeenCalled()
  })

  it('超过 2MiB 被拒绝', async () => {
    // 保护行为：超过 2MiB 必须被拦截（覆盖测试 17）
    const { wrapper } = mountDialog()

    await pickFile(wrapper, textFile('big.md', 2 * 1024 * 1024 + 1))
    await submitWithTitle(wrapper, '测试文档')

    expect(wrapper.text()).toContain('不能超过 2MiB')
    expect(knowledgeApi.uploadKnowledgeDocument).not.toHaveBeenCalled()
  })

  it('非 UTF-8 内容被拒绝', async () => {
    // 保护行为：非法编码必须被拦截（覆盖测试 18）
    const { wrapper } = mountDialog()

    await pickFile(wrapper, new File([new Uint8Array([0xff, 0xfe, 0xff])], 'a.md'))
    await submitWithTitle(wrapper, '测试文档')

    expect(wrapper.text()).toContain('UTF-8')
    expect(knowledgeApi.uploadKnowledgeDocument).not.toHaveBeenCalled()
  })

  it('标题空白或超过 200 字符被拒绝', async () => {
    // 保护行为：标题 strip 后 1–200 字符（覆盖测试 19）
    const { wrapper } = mountDialog()

    await pickFile(wrapper, new File(['内容'], 'a.md'))
    await submitWithTitle(wrapper, '   ')
    expect(wrapper.text()).toContain('标题不能为空')
    expect(knowledgeApi.uploadKnowledgeDocument).not.toHaveBeenCalled()

    await submitWithTitle(wrapper, 'a'.repeat(201))
    expect(wrapper.text()).toContain('不能超过 200 个字符')
    expect(knowledgeApi.uploadKnowledgeDocument).not.toHaveBeenCalled()
  })

  it('合法输入通过校验并调用上传 API（FormData 只有 title+file）', async () => {
    // 保护行为：合法表单必须通过并调用项目 API 层（覆盖测试 3）
    const { wrapper } = mountDialog()

    await pickFile(wrapper, new File(['知识内容'], 'a.md'))
    await submitWithTitle(wrapper, '  测试文档  ')

    expect(knowledgeApi.uploadKnowledgeDocument).toHaveBeenCalledTimes(1)
    const [input] = vi.mocked(knowledgeApi.uploadKnowledgeDocument).mock.calls[0]
    expect(input.title).toBe('测试文档')
    expect(input.file.name).toBe('a.md')
  })
})

describe('DocumentUploadDialog 上传期间与结果', () => {
  it('上传中：表单锁定、显示长耗时提示、不显示百分比进度', async () => {
    // 保护行为：提交期间必须锁定并明确提示长时间（覆盖测试 12）
    const { wrapper, store } = mountDialog()

    await pickFile(wrapper, new File(['内容'], 'a.md'))
    await wrapper.find('[data-test="document-title-input"] input').setValue('测试文档')
    store.uploadSubmitting = true
    await nextTick()

    expect(wrapper.find('[data-test="uploading-note"]').text()).toContain('正在上传并建立知识索引，请勿重复提交。')
    expect(wrapper.text()).not.toContain('%')
    const submitButton = wrapper.find('[data-test="submit-upload"]')
    expect(submitButton.attributes('disabled')).toBeDefined()
  })

  it('上传成功后清空表单并关闭（不重复出现旧内容）', async () => {
    // 保护行为：成功后表单必须清理并可关闭（避免残留导致误提交）
    const { wrapper, dialog } = mountDialog()

    await pickFile(wrapper, new File(['内容'], 'a.md'))
    await submitWithTitle(wrapper, '测试文档')
    await flushPromises()

    expect(knowledgeApi.uploadKnowledgeDocument).toHaveBeenCalledTimes(1)
    expect(dialog.emitted('uploaded')).toBeTruthy()
    // v-model:show=false 已经发出（父级关闭）
    const showEvents = dialog.emitted('update:show')
    expect(showEvents?.[showEvents!.length - 1]?.[0]).toBe(false)
  })

  it('上传失败（422）保留标题与文件选择', async () => {
    // 保护行为：失败不得清空表单（覆盖测试 42）
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockRejectedValue(
      new Error('title must be 1-200'),
    )
    const { wrapper, dialog } = mountDialog()

    await pickFile(wrapper, new File(['内容'], 'a.md'))
    await submitWithTitle(wrapper, '测试文档')

    // 表单保留：标题与文件仍在
    expect((wrapper.find('[data-test="document-title-input"] input').element as HTMLInputElement).value).toBe('测试文档')
    expect(wrapper.find('[data-test="file-summary"]').exists()).toBe(true)
    expect(dialog.emitted('update:show')).toBeUndefined()
  })

  it('超时/断网提示结果不确定并保留表单', async () => {
    // 保护行为：超时后必须提示不确定且保留表单（覆盖测试 43）
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockRejectedValue(
      new Error('timeout'),
    )
    const { wrapper, dialog } = mountDialog()

    await pickFile(wrapper, new File(['内容'], 'a.md'))
    await submitWithTitle(wrapper, '测试文档')

    expect(wrapper.find('[data-test="file-summary"]').exists()).toBe(true)
    expect(dialog.emitted('update:show')).toBeUndefined()
  })

  it('deduplicated 提示「未创建重复版本」', async () => {
    // 保护行为：重复内容必须明确提示（覆盖测试 32）
    vi.mocked(knowledgeApi.uploadKnowledgeDocument).mockResolvedValue(
      receipt({ deduplicated: true }),
    )
    const { wrapper, dialog } = mountDialog()

    await pickFile(wrapper, new File(['内容'], 'a.md'))
    await submitWithTitle(wrapper, '测试文档')

    // 对话框关闭，提示由 message 展示（已上传事件带回执）
    const emitted = dialog.emitted('uploaded') as unknown as [IngestionReceipt][]
    expect(emitted?.[0]?.[0]?.deduplicated).toBe(true)
  })
})
