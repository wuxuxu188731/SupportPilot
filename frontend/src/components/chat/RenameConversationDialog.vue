<script setup lang="ts">
/* 会话重命名对话框：打开时填入当前标题，提交前校验 1–30 个字符。 */
import { computed, ref, watch } from 'vue'
import { NInput, NModal, NText } from 'naive-ui'

const props = defineProps<{
  /** 是否显示对话框 */
  show: boolean
  /** 当前会话标题 */
  title: string
  /** 更新请求是否进行中 */
  updating: boolean
}>()

const emit = defineEmits<{
  /** 关闭对话框 */
  'update:show': [value: boolean]
  /** 提交去除首尾空白后的标题 */
  save: [title: string]
}>()

const draft = ref('')
const errorMessage = ref('')
/** 与服务端按 Unicode 字符计数的口径保持一致。 */
const titleLength = computed(() => Array.from(draft.value.trim()).length)

watch(
  () => props.show,
  (visible) => {
    if (visible) {
      draft.value = props.title
      errorMessage.value = ''
    }
  },
)

/** 校验后提交；请求期间保持对话框打开，由父组件在成功后关闭。 */
function submit(): boolean {
  const value = draft.value.trim()
  if (titleLength.value < 1 || titleLength.value > 30) {
    errorMessage.value = '会话标题长度必须为 1–30 个字符'
    return false
  }
  errorMessage.value = ''
  emit('save', value)
  return false
}

/** 请求进行中禁止关闭，避免丢失保存状态。 */
function close(): boolean {
  if (props.updating) return false
  emit('update:show', false)
  return true
}
</script>

<template>
  <n-modal
    :show="show"
    preset="dialog"
    title="重命名会话"
    :show-icon="false"
    positive-text="保存"
    negative-text="取消"
    :positive-button-props="{ loading: updating, disabled: updating }"
    :mask-closable="!updating"
    :closable="!updating"
    @update:show="(value: boolean) => emit('update:show', value)"
    @positive-click="submit"
    @negative-click="close"
  >
    <div class="rename-body">
      <label for="rename-conversation-title">会话标题</label>
      <n-input
        id="rename-conversation-title"
        v-model:value="draft"
        :disabled="updating"
        placeholder="请输入会话标题"
      />
      <span class="title-count">{{ titleLength }}/30</span>
      <n-text v-if="errorMessage" type="error" role="alert">{{ errorMessage }}</n-text>
    </div>
  </n-modal>
</template>

<style scoped>
.rename-body { display: flex; flex-direction: column; gap: var(--sp-space-2); padding-top: var(--sp-space-2); }
.title-count { align-self: flex-end; color: var(--sp-color-text-3); font-size: var(--sp-font-size-xs); }
</style>
