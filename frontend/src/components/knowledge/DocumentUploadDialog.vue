<script setup lang="ts">
/*
 * 新文档上传对话框（仅 admin 可见入口）。
 *
 * 事实与约束（与后端契约一致）：
 *  - 上传由本项目 API 层统一控制（不用 Naive UI 自动上传）：提交时构造
 *    FormData（title + file），由 http 层注入认证与租户头；
 *  - 文件校验在客户端做（类型/大小/UTF-8/标题），仅用于改善体验，
 *    不能替代后端校验；422 字段错误由 store 回传后映射到表单；
 *  - 上传期间：表单锁定、防止重复提交、展示长耗时提示（无虚假百分比）、
 *    阻止误关闭（禁用关闭按钮与遮罩点击）、不提供虚假的取消按钮；
 *  - 失败（含超时/断网）后**保留标题与文件选择**，由用户自行决定；
 *  - 上传成功或 deduplicated 后才清理表单并关闭。
 */
import { computed, ref, watch } from 'vue'
import { NAlert, NButton, NInput, NModal, useMessage } from 'naive-ui'

import type { IngestionReceipt } from '@/api/knowledgeTypes'
import { useKnowledgeStore } from '@/stores/knowledge'
import {
  detectSourceTypeByFilename,
  validateNewDocumentUpload,
} from '@/utils/knowledgeUpload'
import { DOCUMENT_SOURCE_TYPE_TEXT, formatKnowledgeFileSize, knowledgeDisplayText } from '@/utils/knowledgeDisplay'

const props = defineProps<{
  /** 对话框显隐（父级 v-model） */
  show: boolean
}>()

const emit = defineEmits<{
  /** 对话框显隐变化（父级 v-model） */
  'update:show': [value: boolean]
  /** 上传成功（尚未关闭前事务完成）：父级可跳转详情 */
  uploaded: [receipt: IngestionReceipt]
}>()

const message = useMessage()
const knowledgeStore = useKnowledgeStore()

/** 标题输入（提交时 strip 后校验 1–200 字符）。 */
const title = ref('')
/** 已选择的单个文件。 */
const selectedFile = ref<File | null>(null)
/** 客户端校验错误：标题/文件分别提示。 */
const titleError = ref<string | null>(null)
const fileError = ref<string | null>(null)
/** 表单级错误消息（store 回传的稳定 message）。 */
const formError = ref<string | null>(null)
/** 原生文件输入引用（受控选择，不自动上传）。 */
const fileInputRef = ref<HTMLInputElement | null>(null)

/** 是否正在上传（防重复提交 + 锁定表单）。 */
const submitting = computed(() => knowledgeStore.uploadSubmitting)

/** 已选文件的来源类型展示。 */
const fileSourceTypeText = computed(() => {
  if (!selectedFile.value) return null
  const type = detectSourceTypeByFilename(selectedFile.value.name)
  return type ? knowledgeDisplayText(DOCUMENT_SOURCE_TYPE_TEXT, type) : null
})

/** 已按扩展名过滤的 accept 值（与后端 _extract_source_type 放行的扩展名一致）。 */
const ACCEPT_EXTENSIONS = '.md,.markdown,.txt,.docx,.pdf'

/** 清空选择与校验错误（成功或重新选择时）。 */
function resetForm(): void {
  title.value = ''
  selectedFile.value = null
  titleError.value = null
  fileError.value = null
  formError.value = null
}

/** 关闭请求（上传中禁止关闭）。 */
function onClose(): void {
  if (submitting.value) return
  emit('update:show', false)
}

/** 触发原生文件选择。 */
function onPickFile(): void {
  if (submitting.value) return
  fileInputRef.value?.click()
}

/** 文件选择变化：记录文件并重置文件错误。 */
function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  selectedFile.value = file
  fileError.value = null
  // 允许再次选择同一文件
  input.value = ''
}

/** 映射校验问题到字段错误并返回是否通过。 */
function applyIssue(issue: { field: 'title' | 'file' | 'form'; message: string } | null): boolean {
  titleError.value = null
  fileError.value = null
  formError.value = null
  if (!issue) return true
  if (issue.field === 'title') titleError.value = issue.message
  else if (issue.field === 'file') fileError.value = issue.message
  else formError.value = issue.message
  return false
}

/** 提交：客户端校验 → store 上传 → 按回执/错误分流（失败保留表单）。 */
async function onSubmit(): Promise<void> {
  if (submitting.value) return // 防重复提交兜底
  formError.value = null
  const issue = await validateNewDocumentUpload({
    title: title.value,
    file: selectedFile.value,
  })
  if (!applyIssue(issue)) return

  const outcome = await knowledgeStore.uploadNewDocument({
    title: title.value.trim(),
    file: selectedFile.value as File,
  })

  if (outcome.receipt) {
    // succeeded / deduplicated / pending / failed 均属于服务端已受理：
    // 清空表单并关闭（结果详情由列表/详情页展示）
    message.success(
      outcome.receipt.deduplicated
        ? '上传内容与当前有效版本相同，未创建重复版本。'
        : outcome.receipt.status === 'succeeded'
          ? '文档上传并入库成功。'
          : '上传已受理，任务状态请看文档详情。',
    )
    resetForm()
    emit('update:show', false)
    emit('uploaded', outcome.receipt)
    return
  }

  // 失败：保留标题与文件选择，展示稳定安全消息
  message.warning(knowledgeStore.notice ?? '上传失败，请检查后重试')
}

// 打开时重置表单状态（父级复用同一实例）
watch(
  () => props.show,
  (visible) => {
    if (!visible) resetForm()
  },
)
</script>

<template>
  <n-modal
    :show="show"
    :mask-closable="false"
    :close-on-esc="false"
    :closable="!submitting"
    preset="card"
    title="上传新文档"
    class="upload-modal"
    data-test="upload-document-dialog"
    @update:show="onClose"
  >
    <div class="upload-body">
      <p v-if="submitting" class="uploading-note" data-test="uploading-note">
        正在上传并建立知识索引，请勿重复提交。
      </p>

      <div class="form-field">
        <label class="field-label" for="document-title">文档标题</label>
        <n-input
          id="document-title"
          v-model:value="title"
          :maxlength="200"
          :disabled="submitting"
          :status="titleError ? 'error' : undefined"
          placeholder="例如：售后处理政策（1–200 字符）"
          data-test="document-title-input"
        />
        <p v-if="titleError" class="field-error" role="alert">{{ titleError }}</p>
      </div>

      <div class="form-field">
        <label class="field-label">文件</label>
        <input
          ref="fileInputRef"
          type="file"
          class="native-file-input"
          :accept="ACCEPT_EXTENSIONS"
          data-test="document-file-input"
          @change="onFileChange"
        >
        <div class="file-pick-row">
          <n-button :disabled="submitting" data-test="pick-file" @click="onPickFile">
            {{ selectedFile ? '重新选择文件' : '选择文件' }}
          </n-button>
          <span v-if="selectedFile" class="file-summary" data-test="file-summary">
            <strong>{{ selectedFile.name }}</strong>
            （{{ formatKnowledgeFileSize(selectedFile.size) }} · {{ fileSourceTypeText }}）
          </span>
          <span v-else class="file-placeholder">尚未选择文件</span>
        </div>
        <p v-if="fileError" class="field-error" role="alert">{{ fileError }}</p>
      </div>

      <n-alert type="info" :show-icon="true" class="rules-note">
        支持 .md / .markdown / .txt 文本文件与 .docx / .pdf 文档；单文件不超过
        2MiB；文本文件内容必须为合法 UTF-8。上传会同步执行解析、分块、向量化与
        索引建立，Word 与 PDF 需要调用解析服务，可能需要较长时间，期间请勿重复提交。
      </n-alert>

      <p v-if="formError" class="field-error form-error" role="alert" data-test="form-error">
        {{ formError }}
      </p>
    </div>

    <template #footer>
      <div class="upload-actions">
        <n-button :disabled="submitting" data-test="cancel-upload" @click="onClose">
          取消
        </n-button>
        <n-button
          type="primary"
          :loading="submitting"
          :disabled="submitting"
          data-test="submit-upload"
          @click="onSubmit"
        >
          {{ submitting ? '上传中…' : '上传' }}
        </n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.upload-modal {
  max-width: 560px;
}

.upload-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-4);
}

.uploading-note {
  margin: 0;
  padding: var(--sp-space-3);
  border-radius: var(--sp-radius-sm);
  background: var(--sp-color-bg-active);
  color: var(--sp-color-text-1);
  font-size: var(--sp-font-size-sm);
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-2);
}

.field-label {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-2);
}

.native-file-input {
  /* 原生文件输入隐藏：通过按钮触发，选择结果在下方摘要展示 */
  display: none;
}

.file-pick-row {
  display: flex;
  align-items: center;
  gap: var(--sp-space-3);
  flex-wrap: wrap;
}

.file-summary {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-2);
  min-width: 0;
  overflow-wrap: anywhere;
}

.file-placeholder {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-3);
}

.field-error {
  margin: 0;
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-danger);
}

.form-error {
  padding: var(--sp-space-2) var(--sp-space-3);
  background: var(--sp-color-bg-active);
  border-radius: var(--sp-radius-sm);
}

.rules-note {
  font-size: var(--sp-font-size-sm);
}

.upload-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-space-2);
}
</style>
