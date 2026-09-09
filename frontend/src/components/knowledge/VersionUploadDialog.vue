<script setup lang="ts">
/*
 * 新版本上传对话框（文档详情页，仅 admin 且文档为 active/disabled 且
 * 类型为 markdown/text 时提供入口）。
 *
 * 事实与约束（与后端契约一致）：
 *  - 版本上传**不提供重命名能力**：标题固定为当前文档标题（后端仍要求
 *    title 字段，前端自动传当前标题，不向用户提供重命名输入）；
 *  - 文件类型必须与原文档 source_type 一致（markdown 接受 .md/.markdown，
 *    text 只接受 .txt；word 不提供入口）；
 *  - 提交前必须经过确认对话框（展示目标文档、当前版本与文件信息），
 *    确认前不发起任何请求；
 *  - 上传期间：表单锁定、防重复提交、长耗时提示、阻止误关闭、无虚假取消；
 *  - 失败（含超时/断网）后保留文件选择；成功后关闭并由 store 刷新详情；
 *  - deduplicated=true 预示未创建新版本，不虚构版本条目。
 */
import { computed, ref, watch } from 'vue'
import { NAlert, NButton, NModal, useMessage } from 'naive-ui'

import type { IngestionReceipt, KnowledgeDocumentDetail } from '@/api/knowledgeTypes'
import { useKnowledgeStore } from '@/stores/knowledge'
import {
  detectSourceTypeByFilename,
  validateNewVersionUpload,
} from '@/utils/knowledgeUpload'
import {
  DOCUMENT_SOURCE_TYPE_TEXT,
  formatKnowledgeFileSize,
  knowledgeDisplayText,
} from '@/utils/knowledgeDisplay'

const props = defineProps<{
  /** 对话框显隐（父级 v-model） */
  show: boolean
  /** 目标文档详情（提供标题、类型与当前版本） */
  document: KnowledgeDocumentDetail
}>()

const emit = defineEmits<{
  /** 对话框显隐变化（父级 v-model） */
  'update:show': [value: boolean]
  /** 上传被服务端受理（成功/deduplicated）：父级可刷新提示 */
  uploaded: [receipt: IngestionReceipt]
}>()

const message = useMessage()
const knowledgeStore = useKnowledgeStore()

/** 已选择的单个文件。 */
const selectedFile = ref<File | null>(null)
/** 文件校验错误。 */
const fileError = ref<string | null>(null)
/** 表单级错误消息。 */
const formError = ref<string | null>(null)
/** 确认对话框显隐（确认前不发起任何请求）。 */
const showConfirm = ref(false)
/** 原生文件输入引用。 */
const fileInputRef = ref<HTMLInputElement | null>(null)

/** 是否正在上传（防重复提交 + 锁定表单）。 */
const submitting = computed(() => knowledgeStore.versionUploadSubmitting)

/** 按文档类型推导可接受的扩展名（markdown → md/markdown；text → txt）。 */
const acceptExtensions = computed(() =>
  props.document.source_type === 'text' ? '.txt' : '.md,.markdown',
)

/** 当前有效版本号（用于确认内容展示）。 */
const activeVersionNo = computed(() => {
  const id = props.document.active_version_id
  const active = props.document.versions.find((version) => version.version_id === id)
  return active ? `v${active.version_no}` : '—'
})

/** 已选文件展示（名称/大小/类型）。 */
const fileSummaryText = computed(() => {
  if (!selectedFile.value) return null
  const type = detectSourceTypeByFilename(selectedFile.value.name)
  return `${selectedFile.value.name}（${formatKnowledgeFileSize(selectedFile.value.size)} · ${
    type ? knowledgeDisplayText(DOCUMENT_SOURCE_TYPE_TEXT, type) : '未知类型'
  }）`
})

/** 清空选择与错误。 */
function resetForm(): void {
  selectedFile.value = null
  fileError.value = null
  formError.value = null
  showConfirm.value = false
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

/** 文件选择变化：记录文件并重置错误。 */
function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  selectedFile.value = input.files?.[0] ?? null
  fileError.value = null
  input.value = ''
}

/** 校验文件（类型匹配/大小/UTF-8），通过后打开确认对话框。 */
async function onPrepareSubmit(): Promise<void> {
  if (submitting.value) return
  formError.value = null
  const issue = await validateNewVersionUpload({
    fileCount: selectedFile.value ? 1 : 0,
    file: selectedFile.value,
    documentSourceType: props.document.source_type,
  })
  fileError.value = null
  formError.value = null
  if (!issue) {
    showConfirm.value = true // 确认前不发起任何请求
    return
  }
  if (issue.field === 'file') fileError.value = issue.message
  else formError.value = issue.message
}

/** 确认对话框「确认上传」：真正发起上传（防重复提交）。 */
async function onConfirmSubmit(): Promise<void> {
  if (submitting.value) return
  await onSubmit()
}

/** 上传：store 提交 → 回执/错误分流（失败保留文件选择）。 */
async function onSubmit(): Promise<void> {
  const outcome = await knowledgeStore.uploadNewVersion(props.document.document_id, {
    title: props.document.title,
    file: selectedFile.value as File,
  })
  showConfirm.value = false

  if (outcome.receipt) {
    message.success(
      outcome.receipt.deduplicated
        ? '上传内容与当前有效版本相同，未创建重复版本。'
        : outcome.receipt.status === 'succeeded'
          ? '新版本已上传并激活。'
          : '上传已受理，任务状态请看入库任务面板。',
    )
    resetForm()
    emit('update:show', false)
    emit('uploaded', outcome.receipt)
    return
  }

  // 失败：保留文件选择与对话框（超时/断网提示结果不确定）
  message.warning(knowledgeStore.notice ?? '上传失败，请检查后重试')
}

// 打开时重置表单状态
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
    title="上传新版本"
    class="upload-modal"
    data-test="version-upload-dialog"
    @update:show="onClose"
  >
    <div class="upload-body">
      <p v-if="submitting" class="uploading-note" data-test="uploading-note">
        正在上传并建立知识索引，请勿重复提交。
      </p>

      <!-- 目标文档信息（版本上传不提供重命名入口） -->
      <div class="target-info" data-test="target-info">
        <div class="target-row">
          <span class="target-label">目标文档</span>
          <span class="target-value">{{ document.title }}</span>
        </div>
        <div class="target-row">
          <span class="target-label">文档类型</span>
          <span class="target-value">
            {{ knowledgeDisplayText(DOCUMENT_SOURCE_TYPE_TEXT, document.source_type) }}
          </span>
        </div>
        <div class="target-row">
          <span class="target-label">当前有效版本</span>
          <span class="target-value">{{ activeVersionNo }}</span>
        </div>
      </div>

      <div class="form-field">
        <label class="field-label">新版本文件（标题沿用当前文档标题，不提供重命名）</label>
        <input
          ref="fileInputRef"
          type="file"
          class="native-file-input"
          :accept="acceptExtensions"
          data-test="version-file-input"
          @change="onFileChange"
        >
        <div class="file-pick-row">
          <n-button :disabled="submitting" data-test="pick-version-file" @click="onPickFile">
            {{ selectedFile ? '重新选择文件' : '选择文件' }}
          </n-button>
          <span v-if="fileSummaryText" class="file-summary" data-test="version-file-summary">
            {{ fileSummaryText }}
          </span>
          <span v-else class="file-placeholder">尚未选择文件</span>
        </div>
        <p v-if="fileError" class="field-error" role="alert">{{ fileError }}</p>
      </div>

      <n-alert type="info" :show-icon="true" class="rules-note">
        文件类型必须与原文档一致（{{ acceptExtensions.replace(/\./g, ' ').trim() }}）；
        单文件不超过 2MiB；内容必须为合法 UTF-8。上传成功后将自动成为新的当前有效版本。
      </n-alert>

      <p v-if="formError" class="field-error form-error" role="alert" data-test="form-error">
        {{ formError }}
      </p>
    </div>

    <template #footer>
      <div class="upload-actions">
        <n-button :disabled="submitting" data-test="cancel-version-upload" @click="onClose">
          取消
        </n-button>
        <n-button
          type="primary"
          :loading="submitting"
          :disabled="submitting"
          data-test="prepare-version-upload"
          @click="onPrepareSubmit"
        >
          {{ submitting ? '上传中…' : '继续' }}
        </n-button>
      </div>
    </template>
  </n-modal>

  <!-- 提交前确认对话框：展示目标文档/当前版本/新文件信息 -->
  <n-modal
    :show="showConfirm"
    :mask-closable="false"
    :close-on-esc="false"
    preset="card"
    title="确认上传新版本"
    class="upload-modal"
    data-test="version-confirm-dialog"
    @update:show="onClose"
  >
    <p class="confirm-text">
      将为文档 <strong>{{ document.title }}</strong>
      （当前有效版本 {{ activeVersionNo }}）上传新版本：
      <strong>{{ fileSummaryText }}</strong>
      。上传成功后新版本将自动生效并参与知识检索；期间请勿重复提交。
    </p>
    <template #footer>
      <div class="upload-actions">
        <n-button :disabled="submitting" data-test="back-from-confirm" @click="showConfirm = false">返回修改</n-button>
        <n-button type="primary" :loading="submitting" :disabled="submitting" data-test="confirm-version-upload" @click="onConfirmSubmit">
          确认上传
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

.target-info {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-1);
  padding: var(--sp-space-3);
  background: var(--sp-color-bg-active);
  border-radius: var(--sp-radius-sm);
}

.target-row {
  display: flex;
  align-items: baseline;
  gap: var(--sp-space-3);
}

.target-label {
  flex-shrink: 0;
  width: 100px;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.target-value {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-1);
  min-width: 0;
  overflow-wrap: anywhere;
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

.confirm-text {
  margin: 0;
  line-height: 1.7;
  color: var(--sp-color-text-2);
}

.upload-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-space-2);
}
</style>
