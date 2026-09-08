<script setup lang="ts">
/*
 * 角色徽标（可复用展示组件）：在企业列表与主界面展示当前用户角色。
 * 角色仅用于界面区分（admin 显示为实心主色标签、agent 为中性标签），
 * 真正的权限校验由后端完成。
 */
import { computed } from 'vue'
import { NTag, type TagProps } from 'naive-ui'

const props = defineProps<{
  /** 成员角色：admin 或 agent */
  role: 'admin' | 'agent'
}>()

/** 角色对应的中文标签文案。 */
const label = computed(() => (props.role === 'admin' ? '管理员' : '客服'))

/** admin 使用实心主题色，agent 使用中性描边样式，保持克制不喧宾夺主。 */
const tagType = computed<TagProps['type']>(() => (props.role === 'admin' ? 'primary' : 'default'))
</script>

<template>
  <n-tag :type="tagType" :bordered="role === 'agent'" size="small" round>
    {{ label }}
  </n-tag>
</template>
