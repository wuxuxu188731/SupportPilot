<script setup lang="ts">
/*
 * 创建企业对话框（可复用表单组件）：企业选择页与主界面入口共用。
 *  - 名称规则与后端一致：去首尾空白后 2–100 个字符；
 *  - 创建期间按钮 loading 并禁止重复提交；
 *  - 创建成功后由父组件决定去向（本组件只负责提交与失败反馈）。
 */
import { ref } from 'vue'
import { NAlert, NButton, NInput, NModal } from 'naive-ui'

import { ApiError } from '@/api/errors'
import { useOrganizationStore } from '@/stores/organization'

const props = defineProps<{
  /** 对话框是否可见（v-model:show） */
  show: boolean
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  /** 创建成功回调（新企业已被 store 选中） */
  created: []
}>()

const organizationStore = useOrganizationStore()

/** 企业名称输入 */
const name = ref('')
/** 本地校验错误 */
const nameError = ref('')
/** 服务端错误文案 */
const serverError = ref('')

/** 关闭对话框：清空输入与错误，下次打开时是干净表单。 */
function handleClose(): void {
  name.value = ''
  nameError.value = ''
  serverError.value = ''
  emit('update:show', false)
}

/** 校验企业名称：与后端 trim 后 2–100 规则一致。 */
function validate(): boolean {
  nameError.value = ''
  const normalized = name.value.trim()
  if (normalized.length < 2 || normalized.length > 100) {
    nameError.value = '企业名称长度须为 2–100 个字符'
    return false
  }
  return true
}

/** 提交创建：成功后通知父组件（父组件负责跳转/刷新）。 */
async function handleSubmit(): Promise<void> {
  if (!validate()) return
  if (organizationStore.creating) return // 双保险防重复提交
  serverError.value = ''
  try {
    await organizationStore.create(name.value.trim())
    emit('created')
    handleClose()
  } catch (error) {
    serverError.value =
      error instanceof ApiError ? error.message : '创建失败，请检查网络后重试'
  }
}
</script>

<template>
  <n-modal
    :show="props.show"
    preset="card"
    :title="'创建企业'"
    style="max-width: 440px"
    :mask-closable="false"
    @update:show="handleClose"
    @after-leave="handleClose"
  >
    <n-alert v-if="serverError" type="error" :show-icon="true" role="alert" class="server-alert">
      {{ serverError }}
    </n-alert>

    <form novalidate @submit.prevent="handleSubmit">
      <div class="field">
        <span class="field-label">企业名称</span>
        <n-input
          v-model:value="name"
          placeholder="例如：示例电商售后部"
          aria-label="企业名称"
          maxlength="100"
          :status="nameError ? 'error' : undefined"
          @keydown.enter.prevent="handleSubmit"
        />
        <p v-if="nameError" class="field-error" role="alert">{{ nameError }}</p>
      </div>

      <div class="dialog-actions">
        <n-button size="large" @click="handleClose">取消</n-button>
        <n-button
          type="primary"
          size="large"
          :loading="organizationStore.creating"
          @click="handleSubmit"
        >
          创建并进入
        </n-button>
      </div>
    </form>
  </n-modal>
</template>

<style scoped>
.server-alert {
  margin-bottom: var(--sp-space-4);
}

.field {
  margin-bottom: var(--sp-space-4);
}

.field-label {
  display: block;
  margin-bottom: var(--sp-space-1);
  font-size: var(--sp-font-size-sm);
  font-weight: 500;
  color: var(--sp-color-text-1);
}

.field-error {
  margin: var(--sp-space-1) 0 0;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-error);
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-space-3);
}
</style>
