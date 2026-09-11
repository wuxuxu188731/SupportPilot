"""后端 CORS 装配。

背景：前端开发环境通过 Vite dev server 的 `/api` 代理访问后端（同源，
不触发 CORS）；但前后端分离部署或直连后端时，浏览器会先发预检请求，
没有 CORS 响应头就会全部失败。本模块把允许来源收敛成一处可配置逻辑，
由 `main.py` 在创建 FastAPI 实例后立即装配。

设计取舍：

- 鉴权走 `Authorization: Bearer` 请求头（无 Cookie），因此
  `allow_credentials` 固定为 False，允许来源即可安全配置为 "*"；
- 允许头必须包含 `X-Organization-ID`，它是多租户接口的必需自定义头；
- 只放行后端真实存在的 HTTP 方法，避免无谓地扩大预检面。
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import (
    CORS_ALLOW_HEADERS,
    CORS_ALLOW_METHODS,
    get_cors_allow_origins,
)

# 预检结果缓存时长（秒）：减少浏览器重复 OPTIONS 请求
CORS_PREFLIGHT_MAX_AGE_SECONDS = 600


def configure_cors(app: FastAPI) -> list[str]:
    """为应用装配 CORS 中间件，返回实际生效的允许来源列表。

    允许来源读取环境变量 `CORS_ALLOW_ORIGINS`（英文逗号分隔，支持 "*"）。
    该函数在应用启动时调用一次即可；重复调用会叠加中间件，调用方需避免。
    """
    allow_origins = get_cors_allow_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_methods=list(CORS_ALLOW_METHODS),
        allow_headers=list(CORS_ALLOW_HEADERS),
        # 无 Cookie 鉴权：显式关闭凭据，保持与通配来源的兼容
        allow_credentials=False,
        max_age=CORS_PREFLIGHT_MAX_AGE_SECONDS,
    )
    return allow_origins
