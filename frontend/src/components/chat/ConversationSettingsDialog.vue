<script setup lang="ts">
/*
 * 会话设置对话框：查看/更新当前会话的系统提示词。
 *
 * 文案事实：该提示词只是「会话附加偏好」，不能覆盖服务器规则；
 * 空白输入不能提交；更新期间防止重复提交（由父组件 store 状态保证）。
 */
import { ref, watch } from 'vue'
import { NInput, NModal, NText } from 'naive-ui'

const props = defineProps<{
  /** 是否显示对话框 */
  show: boolean
  /** 当前已保存的系统提示词（来自历史响应或最近保存值）；null 表示未设置 */
  systemPrompt: string | null
  /** 更新请求进行中（防重复提交） */
  updating: boolean
}>()

const emit = defineEmits<{
  /** 关闭对话框 */
  'update:show': [value: boolean]
  /** 提交保存：参数为去空白后的新系统提示词 */
  save: [systemPrompt: string]
}>()

/** 草稿：打开时同步当前值，编辑中允许与已保存值不同。 */
const draft = ref('')
/** 保存按钮的提交错误提示（本地校验，服务端错误由父组件展示）。 */
const errorMessage = ref('')

watch(
  () => props.show,
  (visible) => {
    if (visible) {
      draft.value = props.systemPrompt ?? ''
      errorMessage.value = ''
    }
  },
)

/** 保存：空白输入直接拦截，不发请求。 */
function submit(): void {
  const value = draft.value.trim()
  if (!value) {
    errorMessage.value = '系统提示词不能为空'
    return
  }
  errorMessage.value = ''
  emit('save', value)
}

function close(): void {
  if (props.updating) return // 更新中禁止关闭，防止半途丢状态
  emit('update:show', false)
}
</script>

<template>
  <n-modal
    :show="show"
    preset="dialog"
    title="会话设置"
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
    <div class="settings-body">
      <label class="prompt-label" for="conversation-settings-prompt">会话系统提示词</label>
      <n-input
        id="conversation-settings-prompt"
        v-model:value="draft"
        type="textarea"
        :rows="4"
        placeholder="未设置：可在此填写补充说明（例如回复语言、术语偏好）"
        :disabled="updating"
      />
      <n-text v-if="errorMessage" type="error" class="prompt-error">{{ errorMessage }}</n-text>
      <n-text depth="3" class="prompt-note">
        会话附加偏好：仅用于补充说明回答风格与关注点，不能覆盖服务器的安全与合规规则；该会话的历史回答不受本次修改影响。
      </n-text>
    </div>
  </n-modal>
</template>

<style scoped>
.settings-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-2);
  padding-top: var(--sp-space-2);
}

.prompt-label {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-2);
}

.prompt-error {
  font-size: var(--sp-font-size-sm);
}

.prompt-note {
  font-size: var(--sp-font-size-xs);
  line-height: 1.6;
}
</style>
