import eslint from '@eslint/js'
import vue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

// ESLint 扁平配置：先忽略产物目录，再叠加 JS 推荐、TS 推荐与 Vue 推荐规则
export default tseslint.config(
  {
    ignores: ['dist/**', 'coverage/**', 'node_modules/**'],
  },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  ...vue.configs['flat/recommended'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        // Vue 单文件组件的 <script lang="ts"> 使用 TypeScript 解析器
        parser: tseslint.parser,
      },
    },
  },
  {
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    rules: {
      // 组件名允许单词命名（如 App.vue、LoginView.vue），关闭多词限制
      'vue/multi-word-component-names': 'off',
      // 未使用变量仅告警，避免误伤开发中的临时变量（以 _ 开头的参数忽略）
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
      'vue/require-default-prop': 'off',
      // 纯排版类规则交由书写习惯与后续格式化工具（如 Prettier）约束，不产生告警噪音
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
    },
  },
)
