/*
 * 注册页组件测试：本地校验（用户名规则、密码一致）、失败不发请求。
 */

import { createPinia, setActivePinia } from 'pinia'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '@/router'
import { flushNavigation } from '@/test/routerHelpers'
import RegisterView from '@/views/RegisterView.vue'

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

/** 用消息提供者包裹被测页面。 */
async function mountRegisterView() {
  const router = createAppRouter(createMemoryHistory())
  const pinia = createPinia()
  const wrapper = mount(
    { render: () => h(NMessageProvider, null, { default: () => h(RegisterView) }) },
    {
      global: {
        plugins: [pinia, router],
      },
    },
  )
  // 等待路由初始导航完成，避免后续程序化跳转与初始导航竞争
  await router.isReady()
  return { wrapper, router }
}

/** 按顺序填写三个输入框（用户名/密码/确认密码）。 */
async function fillFields(
  wrapper: ReturnType<typeof mount>,
  values: [string, string, string],
): Promise<void> {
  const inputs = wrapper.findAll('input')
  await inputs[0].setValue(values[0])
  await inputs[1].setValue(values[1])
  await inputs[2].setValue(values[2])
}

async function clickSubmit(wrapper: ReturnType<typeof mount>): Promise<void> {
  const submitButton = wrapper.findAll('button').find((button) => button.text().includes('注册'))
  expect(submitButton).toBeDefined()
  await submitButton!.trigger('click')
}

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('注册页本地校验', () => {
  it('两次密码不一致：本地拦截并提示，不发注册请求', async () => {
    // 保护行为：确认密码不一致必须在客户端拦截，避免无谓请求（覆盖 13 的边界）
    const { wrapper } = await mountRegisterView()
    await fillFields(wrapper, ['alice', 'password-123', 'password-456'])
    await clickSubmit(wrapper)
    await flushPromises()

    expect(wrapper.text()).toContain('两次输入的密码不一致')
    expect(authApi.register).not.toHaveBeenCalled()
  })

  it('用户名不含非法字符：本地规则提示', async () => {
    // 边界情况：与服务端一致的用户名规则应在前端提前给出明确提示
    const { wrapper } = await mountRegisterView()
    await fillFields(wrapper, ['Bad Name!', 'password-123', 'password-123'])
    await clickSubmit(wrapper)
    await flushPromises()

    expect(wrapper.text()).toContain('3–32 位小写字母、数字或下划线')
    expect(authApi.register).not.toHaveBeenCalled()
  })

  it('密码过短：本地拦截提示', async () => {
    // 边界情况：短于 8 个字符的密码不应提交（与后端字节规则对齐）
    const { wrapper } = await mountRegisterView()
    await fillFields(wrapper, ['alice', '123', '123'])
    await clickSubmit(wrapper)
    await flushPromises()

    expect(wrapper.text()).toContain('密码长度须为 8–72 个字符')
    expect(authApi.register).not.toHaveBeenCalled()
  })
})

describe('注册页成功行为', () => {
  it('注册成功：明确跳转登录页（不假定后端自动登录）', async () => {
    // 保护行为：注册接口不返回 Token，成功后应引导用户去登录页
    vi.mocked(authApi.register).mockResolvedValue({
      user_id: 'u-1',
      username: 'alice',
      created_at: '2026-09-01 00:00:00',
    })

    const { wrapper, router } = await mountRegisterView()
    await fillFields(wrapper, ['alice', 'password-123', 'password-123'])
    await clickSubmit(wrapper)
    await flushPromises()
    // 等待懒加载目标路由组件就绪，导航落定后再断言
    await flushNavigation()

    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.registered).toBe('alice')
  })

  it('服务端返回错误（如用户名已存在）：页面展示明确提示', async () => {
    // 保护行为：409 等冲突错误必须以用户可读方式呈现
    vi.mocked(authApi.register).mockRejectedValue(Object.assign(new Error('用户名已被注册'), { status: 409 }))

    const { wrapper } = await mountRegisterView()
    await fillFields(wrapper, ['alice', 'password-123', 'password-123'])
    await clickSubmit(wrapper)
    await flushPromises()

    expect(wrapper.text()).toContain('注册失败')
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
  })
})
