/*
 * 登录页组件测试：服务端错误展示、防重复提交。
 * 页面直接使用真实 auth store（其 API 层被 mock），验证「页面 + store」
 * 组合行为，不依赖真实后端。
 */

import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'
import { createAppRouter } from '@/router'
import { flushNavigation } from '@/test/routerHelpers'
import LoginView from '@/views/LoginView.vue'

vi.mock('@/api/auth', () => ({
  login: vi.fn(),
  register: vi.fn(),
  me: vi.fn(),
}))
vi.mock('@/api/organization', () => ({
  listOrganizations: vi.fn(),
  createOrganization: vi.fn(),
}))

import * as authApi from '@/api/auth'
import type { TokenResponse } from '@/api/types'

/** 用消息提供者包裹被测页面（组件内 useMessage 依赖 provider）。 */
async function mountLoginView() {
  const router = createAppRouter(createMemoryHistory())
  const pinia = createPinia()
  const wrapper = mount(
    { render: () => h(NMessageProvider, null, { default: () => h(LoginView) }) },
    {
      global: {
        plugins: [pinia, router],
      },
    },
  )
  // 等待路由初始导航完成：mount 触发的首个导航（含懒加载组件）结束后，
  // 后续程序化跳转（登录成功/注册后）才会稳定生效
  await router.isReady()
  return { wrapper, router }
}

/** 输入用户名与密码并点击登录按钮。 */
async function fillAndSubmit(wrapper: ReturnType<typeof mount>, username: string, password: string): Promise<void> {
  const inputs = wrapper.findAll('input')
  await inputs[0].setValue(username)
  await inputs[1].setValue(password)
  const submitButton = wrapper.findAll('button').find((button) => button.text().includes('登录'))
  expect(submitButton).toBeDefined()
  await submitButton!.trigger('click')
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('登录页', () => {
  it('登录失败：展示服务端错误消息（不吞错误）', async () => {
    // 保护行为：401 等登录失败必须让用户看到明确的服务端提示
    vi.mocked(authApi.login).mockRejectedValue(
      new ApiError({
        status: 401,
        code: null,
        message: '用户名或密码错误',
        fieldErrors: {},
        raw: { detail: 'invalid username or password' },
      }),
    )

    const { wrapper } = await mountLoginView()
    await fillAndSubmit(wrapper, 'alice', 'wrong-password')
    await flushPromises()

    expect(wrapper.text()).toContain('用户名或密码错误')
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
  })

  it('登录成功：跳转到落地页（默认企业选择页）', async () => {
    // 保护行为：登录闭环完成后离开登录页，不把用户留在原地
    vi.mocked(authApi.login).mockResolvedValue({
      access_token: 'token-1',
      token_type: 'bearer',
      expires_in: 1800,
    } satisfies TokenResponse)
    vi.mocked(authApi.me).mockResolvedValue({
      user_id: 'u-1',
      username: 'alice',
      created_at: '2026-09-01 00:00:00',
    })

    const { wrapper, router } = await mountLoginView()
    await fillAndSubmit(wrapper, 'alice', 'correct-password')
    await flushPromises()
    // 等待懒加载目标路由组件就绪，导航落定后再断言
    await flushNavigation()

    expect(router.currentRoute.value.name).toBe('organizations')
  })

  it('表单加载期间不能重复提交：只发送一次登录请求', async () => {
    // 保护行为：慢网络下连点提交不得产生重复登录请求（覆盖测试 13）
    let resolveLogin!: (value: TokenResponse) => void
    vi.mocked(authApi.login).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveLogin = resolve
        }),
    )
    vi.mocked(authApi.me).mockResolvedValue({
      user_id: 'u-1',
      username: 'alice',
      created_at: '2026-09-01 00:00:00',
    })

    const { wrapper } = await mountLoginView()
    const inputs = wrapper.findAll('input')
    await inputs[0].setValue('alice')
    await inputs[1].setValue('correct-password')
    const submitButton = wrapper.findAll('button').find((button) => button.text().includes('登录'))!
    await submitButton.trigger('click')
    // 请求进行中：按钮进入 loading（不可点）；再尝试触发一次点击
    await submitButton.trigger('click')
    await flushPromises()

    expect(authApi.login).toHaveBeenCalledTimes(1)

    // 收尾：完成挂起的登录请求，避免影响后续用例
    resolveLogin({ access_token: 'token-1', token_type: 'bearer', expires_in: 1800 })
    await flushPromises()
  })

  it('空表单提交：本地校验拦截并提示，不发请求', async () => {
    // 边界情况：前端校验必须先于网络请求，缺字段时不允许提交
    const { wrapper } = await mountLoginView()
    const submitButton = wrapper.findAll('button').find((button) => button.text().includes('登录'))!
    await submitButton.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('请输入用户名')
    expect(authApi.login).not.toHaveBeenCalled()
  })
})
