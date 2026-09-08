<script setup lang="ts">
/*
 * 登录页：用户名 + 密码登录。
 *  - 提交期间按钮进入 loading 并禁止重复提交（store 层也有并发兜底）；
 *  - 服务端错误以醒目提示条展示（不吞错误）；
 *  - 注册成功返回时预填用户名；会话过期跳转时展示「登录已过期」提示；
 *  - 已登录用户访问本页会由路由守卫改道，此处再兜底一次防止竞态。
 */
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAlert, NButton, NInput, useMessage } from 'naive-ui'

import { ApiError } from '@/api/errors'
import AuthShell from '@/components/auth/AuthShell.vue'
import PasswordInput from '@/components/common/PasswordInput.vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const authStore = useAuthStore()

/** 登录用户名（提交前 strip 处理） */
const username = ref('')
/** 登录密码 */
const password = ref('')
/** 用户名本地校验错误 */
const usernameError = ref('')
/** 密码本地校验错误 */
const passwordError = ref('')
/** 服务端/网络错误展示文案 */
const serverError = ref('')

/** 计算登录成功后的落地地址：优先回到被拦截时记录的原始目标。 */
function resolveDestination(): string {
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : ''
  return redirect.startsWith('/') && !redirect.startsWith('//') ? redirect : '/organizations'
}

/** 表单本地校验：通过返回 true，失败则把错误挂到对应字段。 */
function validate(): boolean {
  usernameError.value = ''
  passwordError.value = ''
  if (!username.value.trim()) {
    usernameError.value = '请输入用户名'
    return false
  }
  if (!password.value) {
    passwordError.value = '请输入密码'
    return false
  }
  return true
}

/** 提交登录：失败时展示服务端消息，成功时跳转落地页。 */
async function handleSubmit(): Promise<void> {
  if (!validate()) return
  if (authStore.loading) return // 双保险：按钮已禁用时仍不重复提交
  serverError.value = ''
  try {
    await authStore.login(username.value.trim(), password.value)
    await router.replace(resolveDestination())
  } catch (error) {
    serverError.value =
      error instanceof ApiError ? error.message : '登录失败，请检查网络后重试'
  }
}

onMounted(() => {
  // 注册成功跳回登录页：预填用户名并给出成功提示
  if (typeof route.query.registered === 'string' && route.query.registered) {
    username.value = route.query.registered
    message.success('注册成功，请使用新账号登录')
  }
  // 会话过期被统一处理后跳回登录页：给出过期提示
  if (route.query.reason === 'expired') {
    message.warning('登录已过期，请重新登录')
  }
  // 兜底：极端竞态下已登录仍停留本页时改道
  if (authStore.isLoggedIn) {
    void router.replace(resolveDestination())
  }
})
</script>

<template>
  <AuthShell>
    <div class="page-heading">
      <h1 class="page-title">登录</h1>
      <p class="page-subtitle">欢迎回来，请登录你的 SupportPilot 账号</p>
    </div>

    <n-alert
      v-if="serverError"
      type="error"
      :show-icon="true"
      role="alert"
      class="server-alert"
    >
      {{ serverError }}
    </n-alert>

    <form novalidate @submit.prevent="handleSubmit">
      <div class="field">
        <span class="field-label">用户名</span>
        <n-input
          v-model:value="username"
          placeholder="请输入用户名"
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
          autocomplete="current-password"
          @enter="handleSubmit"
        />
        <p v-if="passwordError" class="field-error" role="alert">{{ passwordError }}</p>
      </div>

      <n-button
        type="primary"
        block
        size="large"
        :loading="authStore.loading"
        class="submit-button"
        @click="handleSubmit"
      >
        登录
      </n-button>
    </form>

    <p class="page-footer">
      还没有账号？
      <router-link class="page-link" :to="{ name: 'register' }">创建账号</router-link>
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
