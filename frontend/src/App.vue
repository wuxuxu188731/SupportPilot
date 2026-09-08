<script setup lang="ts">
/*
 * 应用根组件：统一提供 Naive UI 的中文本地化、主题覆盖与全局弹层
 * （消息提示 message、确认对话框 dialog），供所有页面组件消费。
 */
import { dateZhCN, zhCN, NConfigProvider, NDialogProvider, NMessageProvider } from 'naive-ui'

import { themeOverrides } from '@/theme'
</script>

<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN" :theme-overrides="themeOverrides">
    <n-message-provider placement="top">
      <n-dialog-provider>
        <router-view />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
