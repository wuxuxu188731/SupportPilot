<script setup lang="ts">
/*
 * 消息输入组件：多行文本 + 发送按钮。
 *
 * 键盘与输入法行为：
 *  - Enter 发送（中文输入法 composition 选词期间不误发送）；
 *  - Shift+Enter 换行（保留 textarea 原生行为，不拦截）；
 *  - 发送期间/会话未就绪时输入与按钮禁用（防重复提交）。
 *  - 文本由父组件通过 v-model:value 控制：发送成功后由父组件清空；
 *    发送失败不清空输入，方便用户自行修改重发。
 */
import { computed } from 'vue'
import { NButton, NInput } from 'naive-ui'

const props = defineProps<{
  /** 输入框当前文本（v-model） */
  value: string
  /** 请求进行中：禁用发送并展示等待状态 */
  sending: boolean
  /** 输入是否禁用（如未选择会话 / 历史加载中） */
  disabled?: boolean
  /** 发送按钮文案或等待提示 */
  placeholder?: string
}>()

const emit = defineEmits<{
  'update:value': [value: string]
  /** 请求发送文本（已 trim 且非空） */
  send: [text: string]
}>()

/** 是否正在中文输入法选词（composition 期间 Enter 只负责确认候选词）。 */
let isComposing = false

function onCompositionStart(): void {
  isComposing = true
}

function onCompositionEnd(): void {
  isComposing = false
}

/** Enter（非 Shift、非输入法选词）触发发送；其余按键保留默认行为（换行等）。 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' || event.shiftKey || isComposing) {
    return
  }
  event.preventDefault()
  submit()
}

/** 提交前先 trim 校验：空白输入不发送。 */
function submit(): void {
  if (props.disabled || props.sending) return
  const text = props.value.trim()
  if (!text) return
  emit('send', text)
}

/** 发送按钮可用性：有文本且不在发送/禁用中。 */
const canSend = computed(() => !props.disabled && !props.sending && props.value.trim().length > 0)
</script>

<template>
  <div class="message-composer">
    <div class="composer-field">
      <n-input
        :value="value"
        type="textarea"
        :autosize="{ minRows: 2, maxRows: 8 }"
        :placeholder="placeholder ?? '输入问题，Enter 发送，Shift+Enter 换行'"
        :disabled="disabled || sending"
        aria-label="客服消息输入框"
        data-test="chat-input"
        @update:value="(text: string) => emit('update:value', text)"
        @keydown="onKeydown"
        @compositionstart="onCompositionStart"
        @compositionend="onCompositionEnd"
      />
    </div>
    <div class="composer-actions">
      <span class="composer-hint" aria-hidden="true">
        {{ sending ? 'Agent 处理中，请耐心等待…' : 'Enter 发送 · Shift+Enter 换行' }}
      </span>
      <n-button
        type="primary"
        :disabled="!canSend"
        :loading="sending"
        aria-label="发送消息"
        data-test="chat-send"
        @click="submit"
      >
        发送
      </n-button>
    </div>
  </div>
</template>

<style scoped>
.message-composer {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-2);
}

.composer-hint {
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-3);
}
</style>
