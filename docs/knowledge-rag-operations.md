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

Windows 下 `os.getenv(...)` 对环境变量大小写不敏感。缺失 `DASHSCOPE_API_KEY` 时配置初始化会直接抛错（防止把“无法嵌入”误当作“无命中”）。

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

### 5.2 上传新版本

```bash
curl -X POST "$BASE/knowledge/documents/$DOCID/versions/" \
  -H "Authorization: Bearer $TOKEN" \
  -F "title=云舟商城退货政策" \
  -F "file=@./returns_v2.md"
```

响应：`201`。新版本在 Qdrant upsert 成功前不可见；失败保留旧激活版本。

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

## 6. 停用/启用语义小结

- 停用：`document.status = DISABLED`，对应版本从可检索集合移除，Qdrant 查询层不再命中。
- 启用：恢复最近成功激活版本，重新进入 Qdrant 查询过滤（tenant + active version）。
- 切换本身不触发重新嵌入；只有内容变化（content hash 不同）才重新分块/嵌入/upsert，同一内容不重复处理。

## 7. 失败 Job 排查

Ingestion 是同步编排；失败会有对应 Job 记录。排查步骤：

1. 用 `5.7` 查询 `JOBID` 查看 `status`、`error_code`、`error_message`。
2. 常见错误与处理：
   - `EmbeddingUnavailableError` / `VectorStoreUnavailableError`（HTTP 503）：DashScope 未配 Key、Qdrant 未启动、Qdrant 不可达。先看 `第 2 节` 环境变量与 `第 4 节` 健康检查。
   - `INVALID_DOCUMENT`：文件超 2 MiB、非 UTF-8、非 `.md/.markdown/.txt` 后缀、表单字段不是恰好 `title` + `file`。
   - `DUPLICATE_DOCUMENT_VERSION`：同内容重复上传（content hash 相同）。
   - `DOCUMENT_DISABLED`：对已停用文档执行停用。启用后再操作即可。
3. 上线原则：基础设施失败**不**被当作“无证据/无命中”，服务返回 5xx 以便暴露；只有标准库查询侧确实是空相关时才算“无命中”。

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
