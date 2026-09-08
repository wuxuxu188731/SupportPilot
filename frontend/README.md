# SupportPilot 前端（frontend/）

SupportPilot 电商售后客服工作台的 Web 前端。技术栈：**Vue 3 + TypeScript +
Vite + Vue Router + Pinia + Axios + Naive UI + Vitest**。

当前阶段已实现：注册 / 登录 / 登录状态恢复 / 退出登录 / 企业列表 / 创建企业 /
企业选择与切换 / 主界面骨架（认证与企业上下文闭环）。
客服对话、知识库、审批、成员管理为后续阶段模块，界面中明确标注「待实现」。

## 环境要求

- Node.js ≥ 20（开发环境使用 Node 24 验证）
- npm ≥ 10

## 安装依赖

```bash
cd frontend
npm install
```

> 说明：若 npm 因全局缓存目录无写权限失败，可为本项目指定本地缓存后重试：
> `npm install --cache ./.npm-cache`（`.npm-cache` 已加入 .gitignore）。

## 启动开发服务器

```bash
npm run dev
```

默认地址：http://127.0.0.1:5173

开发环境所有请求统一以 `/api` 开头，由 Vite 代理转发到后端并移除 `/api`
前缀（后端默认 http://127.0.0.1:8000，后端地址通过环境变量可改，见下）。
因此**无需修改后端 CORS 配置**。

### 后端启动（联调前提）

在仓库根目录（`main.py` 所在目录）执行：

```bash
# 需提供环境变量（至少含 32 字符的 AUTH_SECRET_KEY；DEEPSEEK/DASHSCOPE 的 key
# 在本阶段仅影响导入，可先给占位值，但聊天/知识库模块需要真实值）
export AUTH_SECRET_KEY='至少32个随机字符'
export DEEPSEEK_API_KEY='your-key'     # 仅启动需要占位，聊天阶段用真实值
export DASHSCOPE_API_KEY='your-key'    # 同上
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

若想使用独立数据库而不污染仓库根目录的 `chat_history.db`，可额外设置
`CHAT_DB_PATH` 与 `LANGGRAPH_CHECKPOINT_DB_PATH` 指向临时文件。

## 环境变量

复制 `.env.example` 为 `.env.local`（**禁止提交**）后按需修改：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `/api` | Axios 请求基础地址；开发用 `/api`（代理转发），部署时改为真实网关地址 |
| `VITE_PROXY_TARGET` | `http://127.0.0.1:8000` | Vite 开发代理目标后端 |

`.env.local` 中不得存放真实密钥；仓库只提交 `.env.example` 占位文件。

## 常用命令

```bash
npm run dev        # 开发服务器（带 /api 代理）
npm run lint       # ESLint 检查（Vue + TS）
npm run typecheck  # vue-tsc 类型检查
npm run test       # Vitest 单元/组件测试（jsdom，不依赖后端或外部网络）
npm run test:watch # 测试监听模式
npm run build      # 类型检查 + 生产构建（输出 dist/）
npm run preview    # 预览生产构建
```

## 代码结构

```
frontend/
├── index.html                 # 应用 HTML 入口
├── vite.config.ts             # Vite/Vitest 配置与开发代理
├── tsconfig.json              # TypeScript 配置（路径别名 @ -> src）
├── eslint.config.js           # ESLint 扁平配置
├── .env.example               # 环境变量占位示例（不含密钥）
├── src/
│   ├── main.ts                # 应用引导：Pinia/Router/401 全局处理
│   ├── App.vue                # 根组件（Naive UI 中文语言包与主题）
│   ├── styles/tokens.css      # 设计令牌：颜色/间距/圆角/字体
│   ├── theme.ts               # Naive UI 主题覆盖（与令牌对齐）
│   ├── api/                   # HTTP 层
│   │   ├── http.ts            # Axios 实例、拦截器、租户 Header 注入
│   │   ├── errors.ts          # 三种后端错误结构 → 统一 ApiError
│   │   ├── types.ts           # 请求/响应 DTO（全部属性带中文注释）
│   │   ├── auth.ts            # /auth 接口封装
│   │   └── organization.ts    # /organizations 接口封装
│   ├── stores/                # Pinia Store
│   │   ├── auth.ts            # 令牌/用户/登录态恢复/退出
│   │   ├── organization.ts    # 企业列表/当前企业/角色/切换/创建
│   │   ├── tenantReset.ts     # 切换企业时清理租户缓存（统一入口）
│   │   └── persistence.ts     # localStorage 读写（令牌/企业选择）
│   ├── router/index.ts        # 路由与守卫（登录态/企业上下文/redirect）
│   ├── layouts/MainLayout.vue # 主界面布局（顶栏/侧栏占位/内容区）
│   ├── views/                 # 页面：Login/Register/Organizations/AppHome
│   ├── components/            # 可复用组件（AuthShell/PasswordInput/RoleTag/
│   │                          #   OrganizationSwitcher/OrganizationCreateDialog）
│   └── test/                  # 测试环境（jsdom 补齐、路由助手）
```

## 登录态与安全边界（重要事实说明）

- 后端**没有 refresh token，也没有服务端 logout 接口**。因此「退出登录」
  只清理前端本地状态（令牌/用户/企业选择），令牌只能等待自然过期
  （有效期由后端 `ACCESS_TOKEN_TTL_SECONDS` 决定，登录响应 `expires_in` 会
  返回秒数）；前端按此设计，不存在服务端强制下线能力。
- 访问令牌保存在 **localStorage**。这是常见的便捷做法，但其安全性依赖
  站点本身（localStorage 可被同源 XSS 读取），**不是完全安全的存储方案**；
  生产环境应配合 CSP、HttpOnly Cookie 方案或短期令牌与刷新机制演进。
- 任意接口返回 401（登录接口自身的失败除外）都会触发统一的会话过期处理：
  清除本地令牌/用户/企业 → 跳转登录页并提示「登录已过期」。
- 界面上的角色（admin/agent）只用于展示与按钮显隐；**所有权限由后端在每个
  写操作上实时校验**，前端缓存不是安全依据。

## 测试说明

- 测试运行在 jsdom 环境，mock 全部 API 层，**不依赖 DeepSeek/DashScope/
  Qdrant 或任何外部网络**。
- 覆盖：三种后端错误结构解析、登录/恢复/401 清理/退出、租户 Header 注入
  规则、企业列表与本地选择恢复/失效清除、路由守卫（未登录、无企业不可进
  主界面、已登录访问公开页改道）、表单提交防重复等。

## 真实联调验证（第二阶段）

后端正常启动后，本阶段在 API 层完成 16 项真实联调断言并全部通过：
注册 → 登录 → `/auth/me` 恢复 → 创建企业 → 企业列表（角色 admin）→
携带 `X-Organization-ID` 访问租户接口（200）/ 缺头（400）→ 创建第二个企业并
切换 → 错误密码与无效令牌均返回 401。
浏览器 UI 的人工目检（桌面/移动布局）需在有浏览器的环境执行。
