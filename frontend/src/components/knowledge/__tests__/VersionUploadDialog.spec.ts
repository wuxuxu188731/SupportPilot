/*
 * VersionUploadDialog 对话框测试：目标文档信息（只读标题，无重命名输入）、
 * 类型匹配（markdown 接受 md/markdown，text 只接受 txt；word 拒绝）、
 * 提交前确认（确认前不发请求）、上传中锁定、成功后关闭、
 * 失败保留文件。mock API 层与 store，不触达网络。
 */

import { createPinia, setActivePinia } from 'pinia'
import { h, nextTick } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { IngestionReceipt, KnowledgeDocumentDetail } from '@/api/knowledgeTypes'
import VersionUploadDialog from '@/components/knowledge/VersionUploadDialog.vue'
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

/** 构造文档详情。 */
function docDetail(partial: Partial<KnowledgeDocumentDetail> = {}): KnowledgeDocumentDetail {
  return {
    document_id: 'doc-1',
    title: '售后政策',
    source_type: 'markdown',
    status: 'active',
    active_version_id: 'v-2',
    created_at: '2026-09-01T00:00:00.000000+00:00',
    updated_at: '2026-09-02T00:00:00.000000+00:00',
    versions: [
      { version_id: 'v-1', version_no: 1, content_hash: 'sha256:a', loader_version: 'lv', chunker_version: 'cv', embedding_model: 'em', embedding_dimensions: 1024, created_at: '2026-09-01T00:00:00+00:00' },
      { version_id: 'v-2', version_no: 2, content_hash: 'sha256:b', loader_version: 'lv', chunker_version: 'cv', embedding_model: 'em', embedding_dimensions: 1024, created_at: '2026-09-02T00:00:00+00:00' },
    ],
    latest_job: null,
    ...partial,
  }
}

/** 构造上传回执。 */
function receipt(partial: Partial<IngestionReceipt> = {}): IngestionReceipt {
  return {
    document_id: 'doc-1',
    version_id: 'v-3',
    job_id: 'job-1',
    status: 'succeeded',
    deduplicated: false,
    ...partial,
  }
}

/** 挂载版本上传对话框。 */
function mountVersionDialog(document: KnowledgeDocumentDetail = docDetail()) {
  const pinia = createPinia()
  setActivePinia(pinia)
  window.localStorage.setItem(ORGANIZATION_STORAGE_KEY, JSON.stringify({ organizationId: 'org-1' }))
  const store = useKnowledgeStore()
  const organizationStore = useOrganizationStore()
  organizationStore.currentOrganizationId = 'org-1'
  organizationStore.organizations = [{ organization_id: 'org-1', name: '示例企业', role: 'admin' }]
  const wrapper = mount(
    { render: () => h(NMessageProvider, null, { default: () => h(VersionUploadDialog, { show: true, document, onUploaded: () => undefined }) }) },
    { global: { plugins: [pinia], stubs: { teleport: true } } },
  )
  return { wrapper, store, dialog: wrapper.findComponent(VersionUploadDialog) }
}

/** 冲刷微任务与宏任务：FileReader 读取文件是宏任务，需要真实等待。 */
async function settle(): Promise<void> {
  await flushPromises()
  await new Promise<void>((resolve) => setTimeout(resolve, 0))
  await flushPromises()
}

/** 通过原生 input 选择文件。 */
async function pickFile(wrapper: ReturnType<typeof mountVersionDialog>['wrapper'], file: File): Promise<void> {
  const input = wrapper.find('[data-test="version-file-input"]').element as HTMLInputElement
  Object.defineProperty(input, 'files', { value: [file], configurable: true })
  await wrapper.find('[data-test="version-file-input"]').trigger('change')
  await settle()
}

/** 点击「继续」打开确认。 */
async function prepare(wrapper: ReturnType<typeof mountVersionDialog>['wrapper']): Promise<void> {
  await wrapper.find('[data-test="prepare-version-upload"]').trigger('click')
  await settle()
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.mocked(knowledgeApi.uploadKnowledgeDocumentVersion).mockResolvedValue(receipt())
})

describe('VersionUploadDialog 目标信息与类型匹配', () => {
  it('展示目标文档标题与当前有效版本（不提供重命名输入）', async () => {
    // 保护行为：标题固定为当前文档标题，接口仍会传 title 但不能让用户重命名
    const { wrapper } = mountVersionDialog()

    expect(wrapper.find('[data-test="target-info"]').text()).toContain('售后政策')
    expect(wrapper.find('[data-test="target-info"]').text()).toContain('v2')
    // 对话框中只有文件选择输入，没有标题/重命名输入框
    const inputs = wrapper.findAll('[data-test="version-upload-dialog"] input')
    expect(inputs).toHaveLength(1)
  })

  it('markdown 文档接受 .md 与 .markdown（大小写不敏感）', async () => {
    // 保护行为：markdown 类型接受 md/markdown 扩展名（覆盖测试 20）
    const { wrapper } = mountVersionDialog(docDetail({ source_type: 'markdown' }))

    await pickFile(wrapper, new File(['新内容'], 'v2.MD'))
    expect(wrapper.text()).not.toContain('文件类型不匹配')
    await prepare(wrapper)
    expect(wrapper.find('[data-test="version-confirm-dialog"]').exists()).toBe(true)
  })

  it('text 文档只接受 .txt；传 .md 被拒绝', async () => {
    // 保护行为：text 类型只接受 txt（覆盖测试 20）
    const { wrapper } = mountVersionDialog(docDetail({ source_type: 'text' }))

    await pickFile(wrapper, new File(['新内容'], 'v2.md'))
    await prepare(wrapper)

    expect(wrapper.text()).toContain('文件类型不匹配')
    expect(knowledgeApi.uploadKnowledgeDocumentVersion).not.toHaveBeenCalled()

    await pickFile(wrapper, new File(['新内容'], 'v2.TXT'))
    await prepare(wrapper)
    expect(wrapper.find('[data-test="version-confirm-dialog"]').exists()).toBe(true)
  })

  it('word 类型文档即使挂载也拒绝上传新版本', async () => {
    // 保护行为：word 类型不得上传新版本（组件入口已防御，校验再兜底）
    const { wrapper } = mountVersionDialog(docDetail({ source_type: 'word' }))

    await pickFile(wrapper, new File(['新内容'], 'v2.md'))
    await prepare(wrapper)

    expect(wrapper.text()).toContain('Word 类型文档暂不支持上传新版本')
    expect(knowledgeApi.uploadKnowledgeDocumentVersion).not.toHaveBeenCalled()
  })
})

describe('VersionUploadDialog 提交确认与上传', () => {
  it('提交前必须确认：确认前不发送请求，确认后才调用接口', async () => {
    // 保护行为：确认对话框出现前不得发起上传请求（覆盖测试 45）
    const { wrapper, dialog } = mountVersionDialog()

    await pickFile(wrapper, new File(['新内容'], 'v2.md'))
    await prepare(wrapper)

    expect(wrapper.find('[data-test="version-confirm-dialog"]').exists()).toBe(true)
    expect(knowledgeApi.uploadKnowledgeDocumentVersion).not.toHaveBeenCalled()
    expect(dialog.emitted('update:show')).toBeUndefined()

    await wrapper.find('[data-test="confirm-version-upload"]').trigger('click')
    await settle()

    expect(knowledgeApi.uploadKnowledgeDocumentVersion).toHaveBeenCalledWith('doc-1', {
      title: '售后政策',
      file: expect.any(File),
    })
  })

  it('返回修改：关闭确认框且不发送请求', async () => {
    // 边界情况：用户反悔时允许返回修改，不能已提交
    const { wrapper } = mountVersionDialog()

    await pickFile(wrapper, new File(['新内容'], 'v2.md'))
    await prepare(wrapper)
    await wrapper.find('[data-test="back-from-confirm"]').trigger('click')
    await settle()

    expect(knowledgeApi.uploadKnowledgeDocumentVersion).not.toHaveBeenCalled()
  })

  it('上传中：锁定表单、显示长耗时提示、无百分比进度', async () => {
    // 保护行为：提交期间锁定并明确提示（覆盖测试 12）
    const { wrapper, store } = mountVersionDialog()

    await pickFile(wrapper, new File(['新内容'], 'v2.md'))
    await prepare(wrapper)
    store.versionUploadSubmitting = true
    await nextTick()

    expect(wrapper.find('[data-test="uploading-note"]').text()).toContain('正在上传并建立知识索引，请勿重复提交。')
    expect(wrapper.text()).not.toContain('%')
    expect(wrapper.find('[data-test="confirm-version-upload"]').attributes('disabled')).toBeDefined()
  })

  it('成功后关闭并发出 uploaded 事件（回执含 deduplicated）', async () => {
    // 保护行为：成功后必须关闭（父级刷新详情），deduplicated 由父级提示
    vi.mocked(knowledgeApi.uploadKnowledgeDocumentVersion).mockResolvedValue(
      receipt({ deduplicated: true }),
    )
    const { wrapper, dialog } = mountVersionDialog()

    await pickFile(wrapper, new File(['新内容'], 'v2.md'))
    await prepare(wrapper)
    await wrapper.find('[data-test="confirm-version-upload"]').trigger('click')
    await settle()

    const emitted = dialog.emitted('uploaded') as unknown as [IngestionReceipt][]
    expect(emitted?.[0]?.[0]?.deduplicated).toBe(true)
    const showEvents = dialog.emitted('update:show')
    expect(showEvents?.[showEvents!.length - 1]?.[0]).toBe(false)
  })

  it('失败（超时/网络）保留文件选择且不关闭对话框', async () => {
    // 保护行为：结果不确定时必须保留文件选择，由用户决定是否重传（覆盖测试 43）
    vi.mocked(knowledgeApi.uploadKnowledgeDocumentVersion).mockRejectedValue(
      new Error('timeout'),
    )
    const { wrapper, dialog } = mountVersionDialog()

    await pickFile(wrapper, new File(['新内容'], 'v2.md'))
    await prepare(wrapper)
    await wrapper.find('[data-test="confirm-version-upload"]').trigger('click')
    await settle()

    expect(wrapper.find('[data-test="version-file-summary"]').exists()).toBe(true)
    expect(dialog.emitted('update:show')).toBeUndefined()
  })
})
