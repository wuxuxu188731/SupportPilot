/// <reference types="vitest/config" />
import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'

// 前端构建与测试统一配置。
// 开发环境通过 /api 前缀转发到后端（代理目标可用 VITE_PROXY_TARGET 覆盖），
// 转发时移除 /api 前缀；测试环境使用 jsdom，不依赖任何后端或外部网络。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  return {
    plugins: [vue()],
    resolve: {
      // 路径别名：@ 指向 src，与 tsconfig.json 的 paths 保持一致
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      host: '127.0.0.1',
      port: 5173,
      proxy: {
        // 前端所有请求统一以 /api 开头；开发代理把 /api 前缀移除后转发到后端
        '/api': {
          target: env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
      },
    },
    test: {
      // 测试环境：浏览器式 DOM（jsdom），与真实后端/外部网络完全隔离
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      include: ['src/**/*.spec.ts'],
      // 慢环境（受限开发机/低核数）下放宽默认 5s 超时；
      // 路由守卫与组件测试涉及异步导航与动画，需要更多余量
      testTimeout: 20_000,
      hookTimeout: 20_000,
    },
  }
})
