<script setup lang="ts">
/*
 * 注册页：用户名 + 密码 + 确认密码。
 *  - 前端做与服务端一致的基础校验（用户名归一化规则、密码 UTF-8 字节长度）；
 *  - 提交期间防重复提交（按钮 loading + store 并发兜底）；
 *  - 注册接口不返回 Token：成功后不自动登录，明确跳转登录页并预填用户名。
 */
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { NAlert, NButton, NInput, useMessage } from 'naive-ui'

import { ApiError } from '@/api/errors'
import AuthShell from '@/components/auth/AuthShell.vue'
import PasswordInput from '@/components/common/PasswordInput.vue'
import { useAuthStore } from '@/stores/auth'

/** 与服务端一致的归一化用户名规则：strip + 小写后匹配小写字母/数字/下划线 */
const USERNAME_PATTERN = /^[a-z0-9_]{3,32}$/
/** 密码长度边界（UTF-8 字节，与后端 bcrypt 限制一致） */
const PASSWORD_MIN_BYTES = 8
const PASSWORD_MAX_BYTES = 72

const router = useRouter()
const message = useMessage()
const authStore = useAuthStore()

/** 注册用户名（原始输入，用于校验与成功后回填登录页） */
const username = ref('')
/** 注册密码 */
const password = ref('')
/** 确认密码 */
const confirmPassword = ref('')
/** 用户名校验错误 */
const usernameError = ref('')
/** 密码校验错误 */
const passwordError = ref('')
/** 确认密码校验错误 */
const confirmError = ref('')
/** 服务端/网络错误展示文案 */
const serverError = ref('')

/** 计算字符串的 UTF-8 字节长度（中文等多字节字符按实际字节计）。 */
function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).length
}

/** 表单本地校验（与服务端规则对齐），返回是否通过。 */
function validate(): boolean {
  usernameError.value = ''
  passwordError.value = ''
  confirmError.value = ''
  const normalized = username.value.trim().toLowerCase()
  if (!USERNAME_PATTERN.test(normalized)) {
    usernameError.value = '用户名须为 3–32 位小写字母、数字或下划线'
    return false
  }
  const passwordBytes = utf8ByteLength(password.value)
  if (passwordBytes < PASSWORD_MIN_BYTES || passwordBytes > PASSWORD_MAX_BYTES) {
    passwordError.value = '密码长度须为 8–72 个字符'
    return false
  }
  if (confirmPassword.value !== password.value) {
    confirmError.value = '两次输入的密码不一致'
    return false
  }
  return true
}

/** 提交注册：成功后跳转登录页并携带用户名，由登录页提示「注册成功」。 */
async function handleSubmit(): Promise<void> {
  if (!validate()) return
  if (authStore.loading) return // 双保险：按钮 loading 期间不重复提交
  serverError.value = ''
  try {
    await authStore.register(username.value.trim(), password.value)
    message.success('注册成功')
    await router.replace({ name: 'login', query: { registered: username.value.trim() } })
  } catch (error) {
    serverError.value =
      error instanceof ApiError ? error.message : '注册失败，请检查网络后重试'
  }
}
</script>

<template>
  <AuthShell>
    <div class="page-heading">
      <h1 class="page-title">创建账号</h1>
      <p class="page-subtitle">注册后即可创建或加入企业，开始使用客服工作台</p>
    </div>

    <n-alert v-if="serverError" type="error" :show-icon="true" role="alert" class="server-alert">
      {{ serverError }}
    </n-alert>

    <form novalidate @submit.prevent="handleSubmit">
      <div class="field">
        <span class="field-label">用户名</span>
        <n-input
          v-model:value="username"
          placeholder="3–32 位小写字母、数字或下划线"
          autocomplete="username"
          aria-label="用户名"
          :status="usernameError ? 'error' : undefined"
          @keydown.enter.prevent="handleSubmit"
        />
        <p v-if="usernameError" class="field-error" role="alert">{{ usernameError }}</p>
      </div>

      <div class="field">
        <span class="field-label">密码</span>
        <PasswordInput
          v-model="password"
          label="密码"
          placeholder="至少 8 个字符"
          autocomplete="new-password"
          @enter="handleSubmit"
        />
        <p v-if="passwordError" class="field-error" role="alert">{{ passwordError }}</p>
      </div>

      <div class="field">
        <span class="field-label">确认密码</span>
        <PasswordInput
          v-model="confirmPassword"
          label="确认密码"
          placeholder="再次输入密码"
          autocomplete="new-password"
          @enter="handleSubmit"
        />
        <p v-if="confirmError" class="field-error" role="alert">{{ confirmError }}</p>
      </div>

      <n-button
        type="primary"
        block
        size="large"
        :loading="authStore.loading"
        class="submit-button"
        @click="handleSubmit"
      >
        注册
      </n-button>
    </form>

    <p class="page-footer">
      已有账号？
      <router-link class="page-link" :to="{ name: 'login' }">返回登录</router-link>
    </p>
  </AuthShell>
</template>

<style scoped>
.page-heading {
  margin-bottom: var(--sp-space-5);
}

.page-title {
  margin: 0 0 var(--sp-space-1);
  font-size: var(--sp-font-size-xl);
  font-weight: 600;
}

.page-subtitle {
  margin: 0;
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-base);
}

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

.submit-button {
  margin-top: var(--sp-space-2);
}

.page-footer {
  margin: var(--sp-space-5) 0 0;
  text-align: center;
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.page-link {
  color: var(--sp-color-primary);
  font-weight: 500;
}
</style>
