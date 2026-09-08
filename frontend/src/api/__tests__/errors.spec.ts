/*
 * 错误统一解析测试：覆盖后端三种错误结构及其到前端错误对象的转换。
 * 保护行为：ApiError 的 status/code/message/fieldErrors 归一化规则，
 * 以及已知后端英文消息的中文化映射。
 */

import { describe, expect, it } from 'vitest'

import {
  ApiError,
  toApiError,
  toApiErrorFromThrowable,
  translateBackendMessage,
} from '@/api/errors'

describe('错误解析：字符串 detail', () => {
  it('把 {"detail": "message"} 解析为可读消息并中文化已知文案', () => {
    // 保护行为：登录失败的英文 detail 应翻译为中文且状态码正确
    const shape = toApiError(401, { detail: 'invalid username or password' }, {})
    expect(shape.status).toBe(401)
    expect(shape.code).toBeNull()
    expect(shape.message).toBe('用户名或密码错误')
    expect(shape.fieldErrors).toEqual({})
  })

  it('未知英文消息保持原样透出，不误翻译', () => {
    // 边界情况：映射表未收录的消息必须原样返回，避免把后端信息改坏
    const shape = toApiError(409, { detail: 'some brand new message' }, {})
    expect(shape.message).toBe('some brand new message')
  })
})

describe('错误解析：结构化 detail', () => {
  it('提取稳定错误码与 message，并按 code 兜底中文化', () => {
    // 保护行为：知识库/审批模块的 {"code","message"} 结构可被前端识别
    const shape = toApiError(404, { detail: { code: 'DOCUMENT_NOT_FOUND', message: 'document x not found' } }, {})
    expect(shape.status).toBe(404)
    expect(shape.code).toBe('DOCUMENT_NOT_FOUND')
    expect(shape.message).toBe('文档不存在或已被移除')
  })

  it('结构不完整时（缺 message）回落为按状态码的通用提示', () => {
    // 边界情况：结构化错误缺少 message 字段时不应产生空文案
    const shape = toApiError(503, { detail: { code: 'CHECKPOINT_UNAVAILABLE' } }, {})
    expect(shape.code).toBe('CHECKPOINT_UNAVAILABLE')
    expect(shape.message).toContain('稍后重试')
  })
})

describe('错误解析：FastAPI 422 detail 数组', () => {
  it('提取字段级错误：字段名 → 错误描述', () => {
    // 保护行为：表单校验错误按字段归集，便于表单控件回显
    const detail = [
      { type: 'string_too_short', loc: ['body', 'username'], msg: 'String should have at least 3 characters' },
      { type: 'value_error', loc: ['body', 'password'], msg: 'too short' },
    ]
    const shape = toApiError(422, { detail }, {})
    expect(shape.status).toBe(422)
    expect(shape.fieldErrors).toEqual({
      username: 'String should have at least 3 characters',
      password: 'too short',
    })
    expect(shape.message).toContain('提交内容不合法')
  })

  it('非字段级（仅 body/query 定位）错误拼接为消息', () => {
    // 边界情况：loc 只有 ["body"] 时应进入消息列表而非字段错误表
    const detail = [{ type: 'json_invalid', loc: ['body'], msg: 'JSON decode error' }]
    const shape = toApiError(422, { detail }, {})
    expect(shape.fieldErrors).toEqual({})
    expect(shape.message).toBe('JSON decode error')
  })
})

describe('错误解析：无 detail 与兜底', () => {
  it('无响应时（status=0）返回网络失败提示', () => {
    // 保护行为：网络层失败不误报为服务端错误
    const shape = toApiError(0, undefined, new Error('network down'))
    expect(shape.status).toBe(0)
    expect(shape.message).toContain('无法连接到服务器')
  })

  it('空 detail 回落为按状态码的通用提示（500/404）', () => {
    // 边界情况：detail 为空对象时给出可读的通用文案
    expect(toApiError(500, { detail: {} }, {}).message).toContain('服务器内部错误')
    expect(toApiError(404, {}, {}).message).toContain('资源不存在')
  })
})

describe('ApiError 与包装函数', () => {
  it('ApiError 实例字段与形状一致且可直接 instanceof 识别', () => {
    // 保护行为：页面通过 instanceof ApiError 判断并消费错误对象
    const error = new ApiError({
      status: 409,
      code: 'APPROVAL_ALREADY_DECIDED',
      message: '该审批已被处理',
      fieldErrors: {},
      raw: { detail: { code: 'APPROVAL_ALREADY_DECIDED', message: 'x' } },
    })
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(409)
    expect(error.code).toBe('APPROVAL_ALREADY_DECIDED')
  })

  it('toApiErrorFromThrowable：ApiError 原样返回，普通异常包装为网络错误', () => {
    // 边界情况：包装函数不能二次改写已统一的错误；未知异常按网络错误兜底
    const apiError = new ApiError({ status: 403, code: null, message: 'x', fieldErrors: {}, raw: null })
    expect(toApiErrorFromThrowable(apiError)).toBe(apiError)
    const wrapped = toApiErrorFromThrowable(new Error('boom'))
    expect(wrapped).toBeInstanceOf(ApiError)
    expect(wrapped.status).toBe(0)
  })

  it('translateBackendMessage 支持纯消息与消息+code 两种映射', () => {
    // 保护行为：中文化映射函数可被单独复用（例如日志与提示）
    expect(translateBackendMessage('admin role required')).toBe('该操作需要企业管理员权限')
    expect(translateBackendMessage('anything', 'RUN_NOT_FOUND')).toBe('任务不存在或不属于当前企业')
  })
})
