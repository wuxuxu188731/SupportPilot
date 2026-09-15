# 知识库 RAG 运维说明（SupportPilot 阶段 A）

本文档面向部署/运维人员，说明如何安装依赖、启动与健康检查 Qdrant、调用 7 个文档管理 API，以及排查失败 Job 与了解版本/模型约束。

## 1. 安装依赖

项目根目录安装 Python 依赖：

```bash
pip install -r requirements.txt
```

其中与知识库 RAG 直接相关的关键依赖包括：

- `dashscope`：文本嵌入（dense+sparse，`text-embedding-v4`，1024 维）；
- `qdrant-client`：Qdrant 向量检索（原生 RRF 混合检索）；
- `langchain-text-splitters`、`tiktoken`：稳定分块与 token 计量。

若使用非默认默认的 DashScope Base URL，可通过 `DASHSCOPE_BASE_URL` 覆盖（默认 `https://dashscope.aliyuncs.com/api/v1`）。

## 2. 环境变量

运行需要以下环境变量：

| 变量 | 必填 | 说明 | 默认 |
|---|---|---|---|
| `DASHSCOPE_API_KEY` | 是 | DashScope API Key，用于文本嵌入 | — |
| `DASHSCOPE_BASE_URL` | 否 | DashScope 服务地址 | `https://dashscope.aliyuncs.com/api/v1` |
| `QDRANT_URL` | 否 | Qdrant HTTP 地址 | `http://localhost:6333` |
| `KNOWLEDGE_MIN_FUSED_SCORE` | 否 | Stage B 确定性最低融合分阈值 | `0.0` |
| `KNOWLEDGE_SEARCH_TIMEOUT_SECONDS` | 否 | 单次知识工具总预算（秒） | `30` |

Windows 下 `os.getenv(...)` 对环境变量大小写不敏感。缺失 `DASHSCOPE_API_KEY` 时配置初始化会直接抛错（防止把“无法嵌入”误当作“无命中”）。

## Stage B 自适应检索与错误语义

`search_knowledge` 只接受 `question`。服务端固定最多两轮：首轮最多 3 条查询，只有 MULTI 且证据不足时才允许最多 2 条新补充查询；结构化模型调用总数最多 3，最终证据最多 6 段且不超过 3,000 tokens。30 秒总预算逐次向 Planner、Embedding、Qdrant 和 Assessor 下传，模型不能修改这些限制。

- `SEARCH_NOT_NEEDED`：`ok=true`，`data.result_code="SEARCH_NOT_NEEDED"`，`data.evidence_status="not_needed"`，表示纯业务事实问题不需要知识检索。
- `INSUFFICIENT_EVIDENCE`：检索正常完成但依据不足，必须拒绝肯定陈述并建议人工核实。
- `SEARCH_BUDGET_EXCEEDED`：总时间、轮数、查询数或模型调用预算耗尽。
- `EMBEDDING_UNAVAILABLE`：Embedding provider 或响应契约失败。
- `VECTOR_STORE_UNAVAILABLE`：Qdrant 连接、超时或请求失败；不得当作无命中。
- `SEARCH_INTERNAL_ERROR`：Planner/Assessor 的模型调用或结构化 provider 响应失败。

Retrieval event 只保存查询 SHA-256、片段 ID、分数、轮次和稳定错误阶段，不保存原始问题、prompt、模型原始输出或知识正文。聊天引用只从服务器返回的 C1..Cn 映射；未知编号和充分证据完全漏引会产生 `citation.invalid` 并设置 `answer_incomplete=true`。

## 3. 启动 Qdrant

```bash
docker compose up -d qdrant
```

- 服务名 `qdrant`，镜像 `qdrant/qdrant:v1.18.2`，暴露 `6333`（HTTP/gRPC Rest）与 `6334`（gRPC）。
- 数据存于 named volume `day06_qdrant_data`，挂载到容器 `/qdrant/storage`。
- 容器自带 healthcheck（每 5s 探测一次，`start_period` 5s）。

> **环境已知障碍（2026-08-08 实测）**：本机系统代理（`127.0.0.1:10793`）失效或 Docker Hub 拉取受限时，`docker compose up -d qdrant` 可能因**无法拉取固定的 `v1.18.2` 镜像**而失败（报错如 `error getting credentials - docker-credential-desktop not found` 或拉取超时）。此前机器上已缓存并可运行的 `qdrant/qdrant:latest` 镜像可正常承担本次评估与运维角色。**请勿**通过 `docker tag qdrant/qdrant:latest qdrant/qdrant:v1.18.2` 把运行版本伪装成 pin 版本——镜像版本契约必须如实声明。

## 4. 健康检查

```bash
# Qdrant 就绪探测（期望 200）
curl -sf http://localhost:6333/readyz

# 列出 collection（应包含 supportpilot_knowledge_te4_1024_v1）
curl -sf http://localhost:6333/collections

# 查看单 collection 详情（维度、point 数、payload schema）
curl -sf http://localhost:6333/collections/supportpilot_knowledge_te4_1024_v1
```

容器层面可查看 `docker compose ps qdrant`（`healthy` 即就绪）。

## 5. 7 个文档管理 API 的 curl 示例

所有接口均在 `/api/v1` 前缀下、由可信 `TenantContext` 提供 `organization_id`，请求体/表单**不接受也不允许**携带 `organization_id`（多余字段会被 422 拒绝）。写操作（上传、新版本、停用、启用）要求调用者为 `admin`。

以下示例假设 `TOKEN` 为访问令牌，`BASE=http://localhost:8000/api/v1`，`DOCID` 为文档 id，`JOBID` 为入库 Job id。

### 5.1 上传文档（创建）

```bash
curl -X POST "$BASE/knowledge/documents/" \
  -H "Authorization: Bearer $TOKEN" \
  -F "title=云舟商城退货政策" \
  -F "file=@./returns.md"
```

响应：`201`，返回 `IngestionReceiptResponse`（文档 id、最新版本 id、Job id、状态）。

**状态语义（入库已异步化）**：上传接口只做「校验 + 登记 + 入队」，**不再等待解析**，
因此正常返回体的 `status` 是 `queued`。DOCX/PDF 的外部解析实测约 27 秒，放在请求里
会拖到前端上传超时（默认 180 秒），所以解析、分块、Embedding、Qdrant 写入与激活
全部由进程内 worker 在后台推进。客户端应拿 `job_id` 轮询到终态（见 5.7）。

`status` 的取值含义：

| status | 含义 | 客户端动作 |
|---|---|---|
| `queued` | 已登记入队，等待 worker 执行（**正常路径**） | 轮询 5.7，间隔建议 5 秒 |
| `succeeded` | 内容与既有成功版本相同，服务端直接复用了该版本（此时 `deduplicated=true`） | 无需轮询 |
| `failed` | 罕见：复用了某个已失败任务（`deduplicated=true`） | 查询 5.7 看 `error_code` |

**依然同步失败的输入**（不会产生后台任务，错误直接返回）：

- 标题为空或超过 200 字符 → `422 INVALID_DOCUMENT`；
- 文件超过 `MAX_DOCUMENT_BYTES`（2 MiB）→ `422 INVALID_DOCUMENT`；
- 扩展名不受支持（含 `.doc` / `.pptx`）→ `422 INVALID_DOCUMENT`；
- Markdown/TXT 不是合法 UTF-8 或内容为空 → `422 INVALID_DOCUMENT`。
  **注意**：`.docx` / `.pdf` 是二进制格式，不做 UTF-8 校验；真实 docx（zip 包）
  与 pdf 的字节都不是合法 UTF-8，套用文本校验会把它们全部误判为「编码非法」。

**解析类失败改为落在任务上**：解析服务不可用不再返回 5xx，而是在 `job` 上以
`PARSING_UNAVAILABLE` 记录（见第 7 节）。这是有意的：上传本身是成功的，
失败的是后续解析能力，客户端通过任务状态拿到如实结论。

### 5.2 上传新版本

```bash
curl -X POST "$BASE/knowledge/documents/$DOCID/versions/" \
  -H "Authorization: Bearer $TOKEN" \
  -F "title=云舟商城退货政策" \
  -F "file=@./returns_v2.md"
```

响应：`201`，与 5.1 同语义（`queued`）。新版本在 Qdrant upsert 成功前不可见；
失败保留旧激活版本。

**重复上传不重复计费**：服务端按「上传原始字节 + 解析档位 + 解析器版本」做缓存，
并在上传期比对原始字节指纹。同一份文件重复上传会直接复用已预留的版本，
**不会二次调用解析服务**（LlamaParse 按页计费），也不会创建重复版本
（此时 `deduplicated=true`，`status` 为既有任务的状态）。

### 5.3 列出文档

```bash
curl -s "$BASE/knowledge/documents/" -H "Authorization: Bearer $TOKEN"
```

返回当前企业（org）下的文档摘要列表。

### 5.4 获取文档详情

```bash
curl -s "$BASE/knowledge/documents/$DOCID/" -H "Authorization: Bearer $TOKEN"
```

返回摘要 + 版本列表 + 最近一次成功 Job。跨租户的 `DOCID` 走通用 404。

### 5.5 停用文档

```bash
curl -X POST "$BASE/knowledge/documents/$DOCID/disable/" \
  -H "Authorization: Bearer $TOKEN"
```

语义：停用后该文档随即不可检索；若再次上传同标题内容，会触发新版本入库。重复停用幂等（返回 409 提示已停用）。

### 5.6 启用文档

```bash
curl -X POST "$BASE/knowledge/documents/$DOCID/enable/" \
  -H "Authorization: Bearer $TOKEN"
```

语义：启用恢复最近一次成功的激活版本，使其对检索可见。

### 5.7 查询入库 Job

```bash
curl -s "$BASE/knowledge/ingestion-jobs/$JOBID/" -H "Authorization: Bearer $TOKEN"
```

返回 Job 状态与错误信息。跨租户/不存在的 `JOBID` 统一 404（`INGESTION_JOB_NOT_FOUND`）。

异步入库下这是**唯一**能拿到入库终态与失败原因的接口，客户端应在
`status` 为 `queued`/`running` 时每 5 秒轮询一次，终态即停止。
前端已实现（`frontend/src/stores/knowledge.ts` 的 `startJobPolling` /
`pollJobOnce`，页面隐藏时暂停，企业切换/退出登录时停止）。

## 6. 停用/启用语义小结

- 停用：`document.status = DISABLED`，对应版本从可检索集合移除，Qdrant 查询层不再命中。
- 启用：恢复最近成功激活版本，重新进入 Qdrant 查询过滤（tenant + active version）。
- 切换本身不触发重新嵌入；只有内容变化（content hash 不同）才重新分块/嵌入/upsert，同一内容不重复处理。

## 7. 失败 Job 排查

入库是**异步**编排：上传接口登记入队后立刻返回，解析/分块/嵌入/写向量/激活
由进程内 worker 执行。因此**失败不再表现为 HTTP 错误，而是落在 Job 上**。排查步骤：

1. 用 `5.7` 查询 `JOBID` 查看 `status`、`error_code`、`error_message`。
2. 常见错误与处理：
   - `EMBEDDING_UNAVAILABLE`：DashScope 未配 Key、额度不足或服务不可达。先看 `第 2 节` 环境变量。
   - `VECTOR_STORE_UNAVAILABLE`：Qdrant 未启动或不可达。先看 `第 4 节` 健康检查。
   - `PARSING_UNAVAILABLE`：LlamaParse 不可达/超时/限流/额度耗尽，或未配置
     `LLAMA_CLOUD_API_KEY`。**这不是「文档非法」**，文档本身没有问题。
   - `INGESTION_SOURCE_MISSING`：该版本的暂存上传字节已不可用（例如被外部清理），
     需要重新上传。
   - `INGESTION_INTERRUPTED`：进程在解析途中退出且原始字节已丢失，任务无法续跑。
     重新上传即可；若原始字节仍在，重启时会自动重新排队而不是标此错误。
   - `INVALID_DOCUMENT`：见 5.1 的「依然同步失败的输入」——这类错误通常在上传时
     就以 422 返回，只有少数场景（如同一文档并发写入竞争）才会落到任务上。
   - `DUPLICATE_DOCUMENT_VERSION`：同内容重复上传（content hash 相同）。
   - `DOCUMENT_DISABLED`：对已停用文档执行停用。启用后再操作即可。
3. 上线原则：基础设施失败**不**被当作“无证据/无命中”，服务返回 5xx 以便暴露；
   只有标准库查询侧确实是空相关时才算“无命中”。

## 7.1 进程重启后的任务恢复

worker 是**进程内**的，因此进程退出会打断执行中的任务。恢复策略如下：

- 上传的原始字节随版本行一起落库，任务记录也持久化，**任务不会凭空消失**；
- 应用启动时（`main.py` 的 lifespan）会调用 `worker.recover()`：
  - 遗留的 `queued` 任务 → 重新入队；
  - 遗留的 `running` 任务，若版本仍暂存着原始字节 → 重置回 `queued` 并重新执行
    （外部解析另有缓存，续跑不会二次计费）；
  - 遗留的 `running` 任务，若原始字节已丢失、确实无法续跑 → 标记 `failed`，
    错误码 `INGESTION_INTERRUPTED`，**不会**留下永远 `running` 的僵尸任务。

因此「重启后任务卡在 running」不属于正常现象：看到该状态说明应用没有走完
lifespan 的恢复流程（例如被强杀后又用旧进程继续跑）。

## 8. Collection 模型/维度不可原地混用

Collection `supportpilot_knowledge_te4_1024_v1` 在与 `text-embedding-v4`、1024 维固定的前提下创建，需保证：

- 入库与检索使用**同一个** embedding 模型/维度（配置 `DASHSCOPE...`/模型名不应中途变更）；
- 若模型或维度变化，应新建一个命名承载新契约的 Collection，而**不是**在现有 Collection 内混入不同维度/不同模型的向量——向量维度必须一致，否则无法 point 级比对。

## 9. 停机维护

```bash
docker compose down
```

`docker compose down` **不会删除** named volume `day06_qdrant_data`，数据在下次 `up` 后仍在。

> 请**不要**把 `docker compose down -v` 当作例行命令——`-v` 会删除 named volume，导致全部向量数据丢失。仅在明确需要清空存储且已备份的前提下才允许使用。
