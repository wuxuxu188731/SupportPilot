/*
 * 消息输入组件测试：Enter 发送、Shift+Enter 换行（不发送）、
 * 中文输入法 composition 期间不误发送、空白输入不发送、发送中禁用。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import MessageComposer from '@/components/chat/MessageComposer.vue'

/** 挂载输入组件并返回 textarea 与发送按钮。 */
async function mountComposer(props: { value?: string; sending?: boolean; disabled?: boolean } = {}) {
  const wrapper = mount(MessageComposer, {
    props: {
      value: props.value ?? '',
      sending: props.sending ?? false,
      disabled: props.disabled ?? false,
      'onUpdate:value': (value: string) => wrapper.setProps({ value }),
    },
  })
  await flushPromises()
  const textarea = wrapper.find('textarea')
  return { wrapper, textarea }
}

describe('MessageComposer 键盘发送行为', () => {
  it('按 Enter 发送非空消息', async () => {
    // 保护行为：Enter 应发送去空白后非空的输入内容
    const { wrapper, textarea } = await mountComposer({ value: '  查询订单  ' })

    await textarea.trigger('keydown', { key: 'Enter' })

    expect(wrapper.emitted('send')).toBeTruthy()
    expect(wrapper.emitted('send')?.[0]).toEqual(['查询订单'])
  })

  it('Shift+Enter 不发送（交给浏览器原生换行）', async () => {
    // 保护行为：Shift+Enter 用于换行，不能触发发送
    const { wrapper, textarea } = await mountComposer({ value: '多行内容' })

    await textarea.trigger('keydown', { key: 'Enter', shiftKey: true })

    expect(wrapper.emitted('send')).toBeFalsy()
  })

  it('中文输入法 composition 选词期间按 Enter 不发送', async () => {
    // 保护行为：拼音/五笔选词确认使用 Enter，此时必须不触发发送
    const { wrapper, textarea } = await mountComposer({ value: 'nihao' })

    await textarea.trigger('compositionstart')
    await textarea.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('send')).toBeFalsy()

    // composition 结束后 Enter 恢复正常发送
    await textarea.trigger('compositionend')
    await textarea.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('send')).toBeTruthy()
  })

  it('空白输入不发送', async () => {
    // 边界情况：只有空白的内容不得发起请求
    const { wrapper, textarea } = await mountComposer({ value: '   \n  ' })

    await textarea.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('send')).toBeFalsy()
  })

  it('发送进行中再次按 Enter 不发送（防重复提交）', async () => {
    // 保护行为：请求进行中输入与发送必须被锁定
    const { wrapper, textarea } = await mountComposer({ value: '请求中', sending: true })

    await textarea.trigger('keydown', { key: 'Enter' })

    expect(wrapper.emitted('send')).toBeFalsy()
  })

  it('点击发送按钮发送非空消息', async () => {
    // 保护行为：发送按钮与 Enter 行为一致
    const { wrapper } = await mountComposer({ value: '按钮发送' })
    const button = wrapper.find('[data-test="chat-send"]')

    await button.trigger('click')

    expect(wrapper.emitted('send')?.[0]).toEqual(['按钮发送'])
  })

  it('发送按钮在空白/禁用/发送中不可点击', async () => {
    // 边界情况：发送按钮 must 在无内容或忙碌状态 disabled
    const blank = await mountComposer({ value: '' })
    expect(blank.wrapper.find('[data-test="chat-send"]').attributes('disabled')).toBeDefined()

    const busy = await mountComposer({ value: '内容', sending: true })
    expect(busy.wrapper.find('[data-test="chat-send"]').attributes('disabled')).toBeDefined()

    const locked = await mountComposer({ value: '内容', disabled: true })
    expect(locked.wrapper.find('[data-test="chat-send"]').attributes('disabled')).toBeDefined()
  })
})
