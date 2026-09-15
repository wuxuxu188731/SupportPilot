<script setup lang="ts">
/** 共用线性图标，控件名称由外层按钮或链接提供。 */
defineProps<{
  /** 图标名称，对应功能或操作。 */
  name: string
  /** 图标宽高，默认二十像素。 */
  size?: number
}>()

const paths: Record<string, string> = {
  home: 'M3 10 12 3l9 7v10h-6v-7H9v7H3Z',
  chat: 'M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 3V6a2 2 0 0 1 2-2ZM7 9h10M7 13h6',
  approvals: 'M9 4H5v17h14V4h-4M9 3h6v4H9ZM8 14l3 3 5-6',
  knowledge: 'M12 5v16M12 5C9 3 5 3 2 4v15c3-1 7-1 10 2 3-3 7-3 10-2V4c-3-1-7-1-10 1Z',
  members: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M17 15a4 4 0 0 1 5 4v2M17 3a4 4 0 0 1 0 8M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  arrow: 'M4 12h16M14 6l6 6-6 6',
  spark: 'm12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z',
  shield: 'm12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6ZM8 12l3 3 5-6',
  logout: 'M9 4H4v16h5M10 12h11M17 8l4 4-4 4',
  building: 'M4 21V3h12v18M16 9h4v12M2 21h20M8 7h4M8 11h4M8 15h4M9 21v-3h3v3',
}
</script>

<template>
  <svg :width="size ?? 20" :height="size ?? 20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" class="app-icon">
    <path :d="paths[name] ?? paths.spark" />
  </svg>
</template>

<style scoped>
.app-icon { display: inline-block; flex-shrink: 0; vertical-align: middle; }
</style>
