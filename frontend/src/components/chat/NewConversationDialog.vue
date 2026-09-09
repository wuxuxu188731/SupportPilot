<script setup lang="ts">
/*
 * 新建会话对话框：可选填写会话系统提示词，创建成功后由父组件导航。
 *
 * 文案事实：会话 system prompt 是「会话附加偏好」，不能覆盖服务器规则
 * （与后端 base system prompt 的拼接语义一致）。
 */
import { ref, watch } from 'vue'
import { NInput, NModal, NText } from 'naive-ui'

const props = defineProps<{
  /** 是否显示对话框 */
  show: boolean
  /** 创建请求进行中（防重复提交） */
  creating: boolean
}>()

const emit = defineEmits<{
  /** 关闭对话框 */
  'update:show': [value: boolean]
  /** 提交创建：参数为去空白后的系统提示词（可为空字符串表示不设置） */
  create: [systemPrompt: string]
}>()

/** 系统提示词草稿（可选填写）。 */
const draft = ref('')

// 每次打开时重置草稿与错误，避免残留上次输入
watch(
  () => props.show,
  (visible) => {
    if (visible) {
      draft.value = ''
    }
  },
)

/** 提交创建：空白输入允许（表示不设置），由调用方处理去空白逻辑。 */
function submit(): void {
  emit('create', draft.value.trim())
}

/** 取消：直接关闭对话框。 */
function close(): void {
  emit('update:show', false)
}
</script>

<template>
  <n-modal
    :show="show"
    preset="dialog"
    title="新建会话"
    :show-icon="false"
    positive-text="创建并开始对话"
    negative-text="取消"
    :positive-button-props="{ loading: creating, disabled: creating }"
    :mask-closable="!creating"
    :closable="!creating"
    @update:show="(value: boolean) => emit('update:show', value)"
    @positive-click="submit"
    @negative-click="close"
  >
    <div class="new-conversation-body">
      <label class="prompt-label" for="new-conversation-prompt">会话系统提示词（可选）</label>
      <n-input
        id="new-conversation-prompt"
        v-model:value="draft"
        type="textarea"
        :rows="3"
        placeholder="例如：回复使用简体中文、优先引用企业知识库"
        :disabled="creating"
        @keydown.enter.exact.prevent
      />
      <n-text depth="3" class="prompt-note">
        该提示词是会话附加偏好，用于补充说明回答风格与关注点，不能覆盖服务器的安全与合规规则。
      </n-text>
    </div>
  </n-modal>
</template>

<style scoped>
.new-conversation-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-2);
  padding-top: var(--sp-space-2);
}

.prompt-label {
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-2);
}

.prompt-note {
  font-size: var(--sp-font-size-xs);
  line-height: 1.6;
}
</style>
