<script setup lang="ts">
/*
 * 密码输入框（可复用表单组件）：登录与注册页共用。
 * 依赖 Naive UI 内置的明文切换（show-password-on），保证键盘可达与焦点清晰；
 * autocomplete 交给调用方（登录页 current-password / 注册页 new-password）。
 */
import { NInput } from 'naive-ui'

withDefaults(
  defineProps<{
    /** 当前密码值（v-model） */
    modelValue: string
    /** 输入框占位文案 */
    placeholder?: string
    /** 浏览器自动填充语义：登录 current-password；注册 new-password */
    autocomplete?: 'current-password' | 'new-password'
    /** 无障碍标签（必填，供读屏识别；同时作为可见标签的语义补充） */
    label: string
  }>(),
  {
    placeholder: '请输入密码',
    autocomplete: 'current-password',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  /** 输入框内按回车（由父级提交表单） */
  enter: []
}>()
</script>

<template>
  <n-input
    :type="'password'"
    :value="modelValue"
    :placeholder="placeholder"
    :autocomplete="autocomplete"
    :aria-label="label"
    show-password-on="click"
    @update:value="(value: string) => emit('update:modelValue', value)"
    @keydown.enter.prevent="emit('enter')"
  />
</template>
