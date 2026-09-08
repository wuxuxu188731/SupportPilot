<script setup lang="ts">
/*
 * 认证页共用外壳：登录/注册/企业选择页共用的居中布局。
 * 左侧品牌区（深色克制风格），右侧内容区；窄屏时品牌区折叠为顶部条。
 */
</script>

<template>
  <div class="auth-shell">
    <aside class="auth-brand" aria-hidden="true">
      <div class="auth-brand-inner">
        <div class="auth-brand-mark" aria-hidden="true">SP</div>
        <h1 class="auth-brand-title">SupportPilot</h1>
        <p class="auth-brand-desc">面向电商售后客服团队的多租户工单处理工作台</p>
      </div>
    </aside>
    <main class="auth-panel">
      <div class="auth-card">
        <slot />
      </div>
    </main>
  </div>
</template>

<style scoped>
.auth-shell {
  display: grid;
  grid-template-columns: minmax(280px, 5fr) minmax(380px, 4fr);
  min-height: 100vh;
  background: var(--sp-color-bg-page);
}

/* 品牌区：深色底 + 克制排版，不做花哨装饰 */
.auth-brand {
  display: flex;
  align-items: center;
  justify-content: center;
  background: #101a30;
  color: #ffffff;
  padding: var(--sp-space-8);
}

.auth-brand-inner {
  max-width: 360px;
}

.auth-brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: var(--sp-radius-md);
  background: var(--sp-color-primary);
  color: #ffffff;
  font-weight: 600;
  letter-spacing: 0.5px;
  margin-bottom: var(--sp-space-5);
}

.auth-brand-title {
  margin: 0 0 var(--sp-space-3);
  font-size: var(--sp-font-size-xxl);
  font-weight: 600;
}

.auth-brand-desc {
  margin: 0;
  color: rgba(255, 255, 255, 0.72);
  font-size: var(--sp-font-size-base);
  line-height: 1.7;
}

/* 右侧内容区：垂直居中放置表单卡片 */
.auth-panel {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-space-6);
}

.auth-card {
  width: 100%;
  max-width: 400px;
}

/* 窄屏（常见移动端宽度）：品牌区折叠为顶部横条 */
@media (max-width: 860px) {
  .auth-shell {
    display: block;
  }

  .auth-brand {
    padding: var(--sp-space-4) var(--sp-space-5);
    justify-content: flex-start;
  }

  .auth-brand-inner {
    display: flex;
    align-items: center;
    gap: var(--sp-space-3);
    width: 100%;
  }

  .auth-brand-mark {
    width: 32px;
    height: 32px;
    font-size: var(--sp-font-size-sm);
    margin-bottom: 0;
  }

  .auth-brand-title {
    margin: 0;
    font-size: var(--sp-font-size-lg);
  }

  .auth-brand-desc {
    display: none;
  }

  .auth-panel {
    min-height: calc(100vh - 64px);
    align-items: flex-start;
    padding: var(--sp-space-6) var(--sp-space-4);
  }
}
</style>
