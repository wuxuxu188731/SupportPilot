<script setup lang="ts">
/*
 * 添加成员对话框（可复用表单组件）：成员管理页的「添加成员」入口。
 *
 * 事实与约束（依据 docs/frontend/api-inventory.md 4.2.3 与后端实现）：
 *  - 后端按**精确用户名**把「已注册用户」加入企业，不存在的用户名返回 404；
 *  - 用户名规则与注册一致：3–32 位小写字母、数字或下划线（服务端会
 *    strip + casefold 归一化），前端做同规则前置校验但以服务端为准；
 *  - 添加成员写操作仅 admin；agent 提交会得到 403，此处按提示文案兜底；
 *  - 提交成功后由父组件（store）负责刷新列表，本组件只负责提交与反馈。
 */
import { computed, ref } from 'vue'
import { NAlert, NButton, NInput, NModal, NRadioButton, NRadioGroup } from 'naive-ui'

import type { MembershipRole } from '@/api/types'
import { useMembersStore, type AddMemberOutcome } from '@/stores/members'

const props = defineProps<{
  /** 对话框是否可见（v-model:show） */
  show: boolean
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  /** 添加成功回调（参数为新增成员的用户名，供父组件提示） */
  added: [username: string]
}>()

const membersStore = useMembersStore()

/** 用户名输入（后端会做 strip + casefold，这里同样按小写提交） */
const username = ref('')
/** 角色选择：默认客服（最小权限原则） */
const role = ref<MembershipRole>('agent')
/** 本地校验错误 */
const usernameError = ref('')
/** 服务端错误文案 */
const serverError = ref('')

/** 提交中状态：来自 store，防重复提交 */
const submitting = computed(() => membersStore.adding)

/** 关闭对话框：清空输入与错误，下次打开时是干净表单。 */
function handleClose(): void {
  username.value = ''
  role.value = 'agent'
  usernameError.value = ''
  serverError.value = ''
  emit('update:show', false)
}

/** 校验用户名：与后端注册/添加成员规则一致（归一化后 3–32 位小写字母数字下划线）。 */
function validate(): boolean {
  usernameError.value = ''
  const normalized = username.value.trim().toLowerCase()
  if (!/^[a-z0-9_]{3,32}$/.test(normalized)) {
    usernameError.value = '用户名须为 3–32 位小写字母、数字或下划线'
    return false
  }
  return true
}

/** 把失败分类转换为用户可读文案。 */
function outcomeMessage(outcome: AddMemberOutcome): string {
  if (outcome.result === 'error') {
    // error 分类由 store 带回已翻译的具体消息
    return outcome.message
  }
  switch (outcome.result) {
    case 'duplicate':
      return '该用户已是本企业成员'
    case 'user-missing':
      return '用户不存在，请确认对方已注册且用户名正确'
    case 'forbidden':
      return '只有企业管理员可以添加成员'
    case 'not-found':
      return '企业不存在，或你已不在该企业中'
    default:
      return '添加失败，请稍后重试'
  }
}

/** 提交：成功后通知父组件，并关闭对话框。 */
async function handleSubmit(): Promise<void> {
  if (!validate()) return
  if (submitting.value) return // 双保险防重复提交
  serverError.value = ''
  const normalized = username.value.trim().toLowerCase()
  const outcome = await membersStore.addMember(normalized, role.value)
  if (outcome.result === 'ok') {
    emit('added', normalized)
    handleClose()
    return
  }
  serverError.value = outcomeMessage(outcome)
}
</script>

<template>
  <n-modal
    :show="props.show"
    preset="card"
    title="添加成员"
    style="max-width: 460px"
    :mask-closable="false"
    @update:show="handleClose"
    @after-leave="handleClose"
  >
    <n-alert v-if="serverError" type="error" :show-icon="true" role="alert" class="server-alert">
      {{ serverError }}
    </n-alert>

    <form novalidate @submit.prevent="handleSubmit">
      <div class="field">
        <span class="field-label">用户名</span>
        <n-input
          v-model:value="username"
          placeholder="对方在 SupportPilot 注册的用户名"
          data-test="member-username-input"
          aria-label="成员用户名"
          maxlength="32"
          :status="usernameError ? 'error' : undefined"
          @keydown.enter.prevent="handleSubmit"
        />
        <p v-if="usernameError" class="field-error" role="alert">{{ usernameError }}</p>
        <p v-else class="field-hint">
          只能添加已注册用户，需输入完整用户名（3–32 位小写字母、数字或下划线）。
        </p>
      </div>

      <div class="field">
        <span class="field-label">角色</span>
        <n-radio-group v-model:value="role" aria-label="成员角色">
          <n-radio-button value="agent">客服</n-radio-button>
          <n-radio-button value="admin">管理员</n-radio-button>
        </n-radio-group>
        <p class="field-hint">
          管理员可审批退款/补偿、管理知识库与成员；客服仅可处理对话与查看数据。
        </p>
      </div>

      <div class="dialog-actions">
        <n-button size="large" @click="handleClose">取消</n-button>
        <n-button
          type="primary"
          size="large"
          data-test="submit-add-member"
          :loading="submitting"
          @click="handleSubmit"
        >
          添加
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

.field-hint {
  margin: var(--sp-space-1) 0 0;
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
  line-height: 1.5;
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
