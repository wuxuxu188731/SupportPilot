# Tenant-Scoped RAG Stage A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 SupportPilot 建立严格租户隔离、可版本化、可审计的知识入库能力，以及固定 Top-K 的 dense+sparse 传统 RAG Baseline，并输出第一版可复现评测报告。

**Architecture:** SQLite 负责文档、版本、片段正文、入库任务和检索事件；Docker Qdrant 只负责同一共享 Collection 中的 dense/sparse 向量与原生 RRF。所有外部依赖隐藏在 Protocol 后，应用服务只接收可信 `TenantContext.organization_id`，检索命中后必须回 SQLite 再校验租户、文档状态和激活版本。

**Tech Stack:** Python 3.10、FastAPI 0.115、Pydantic 2.11、SQLite、Alembic、DashScope `text-embedding-v4`、Qdrant 1.18、`qdrant-client`、`langchain-text-splitters`、pytest、Docker Compose。

## Global Constraints

- 单文件只允许 UTF-8 Markdown/TXT，最大 `2 MiB`；标题和正文不得为空。
- Chunk 目标 `400～700 tokens`，重叠不超过 `80 tokens`；ordinal、offset 必须稳定。
- Embedding 固定 `text-embedding-v4`、`dimension=1024`、`output_type="dense&sparse"`；文档固定 `text_type="document"`，查询固定 `text_type="query"`。
- DashScope 每批最多 `10` 条；只对明确的临时服务错误短重试一次；单次查询嵌入超时 `5 秒`。
- Qdrant Collection 固定命名 `supportpilot_knowledge_te4_1024_v1`，包含命名向量 `dense` 和 `sparse`。
- Baseline 固定执行原始问题单查询、dense Top-8、sparse Top-8、Qdrant 原生 RRF、项目层 Top-5。
- 最终知识片段合计最多 `3,000 tokens`；Stage A Baseline 最多返回 `5` 个片段。
- `organization_id` 只能来自可信服务参数或 `TenantContext`，HTTP 请求体、模型输出、文档内容均不得提供或覆盖它。
- Qdrant dense/sparse 两个 prefetch 必须都过滤 `organization_id` 和 SQLite 返回的激活版本集合。
- Qdrant 命中后必须回 SQLite 按 `organization_id + document_id + version_id + chunk_id` 二次校验。
- 新版本只有在 SQLite chunks 与 Qdrant points 全部成功后才能激活；失败时旧激活版本继续服务。
- “无命中”是 `INSUFFICIENT_EVIDENCE` 业务结果；Qdrant/Embedding 不可用是基础设施失败，二者不得混淆。
- Stage A 不实现 Planner、EvidenceAssessor、多查询、第二轮检索、Agent 工具注册、聊天响应引用扩展或回答生成。
- Stage A 第一版评测集固定为 `16` 条（四类各 4 条）；阶段 C 再扩展到至少 48 条。
- 所有新增生产行为先写失败测试；每个 Task 独立通过后提交一次，不混入无关重构。

---

## 1. 文件职责映射

### 新增生产文件

- `app/knowledge/base.py`：文档、版本、Chunk、Job、Event 领域模型、枚举、异常和 `KnowledgeStore` Protocol。
- `app/knowledge/sqlite_store.py`：所有显式带 `organization_id` 的 SQLite 实现和原子状态切换。
- `app/knowledge/document_loader.py`：Markdown/TXT UTF-8 解码、换行标准化与标题信息提取。
- `app/knowledge/chunking.py`：第三方 splitter 适配、token 计数、稳定 ordinal 与 offset。
- `app/knowledge/embeddings.py`：`EmbeddingClient` Protocol 和 dense/sparse 领域值对象。
- `app/knowledge/dashscope_embeddings.py`：DashScope 原生 SDK 适配、批处理、校验、超时和错误转换。
- `app/knowledge/vector_store.py`：`VectorStore` Protocol、upsert/search 输入输出值对象。
- `app/knowledge/qdrant_store.py`：Collection、Point、tenant payload index、过滤和原生 RRF。
- `app/knowledge/ingestion.py`：同步 Job 编排、哈希幂等、失败状态和版本激活。
- `app/knowledge/retrieval.py`：固定 Top-K Baseline、SQLite 二次校验、去重和 token 预算。
- `app/knowledge/results.py`：Citation、Baseline 结果和 HTTP/评测序列化。
- `app/knowledge/factory.py`：KnowledgeStore、Embedding、Qdrant、Ingestion、Baseline 的生产装配。
- `app/api/knowledge_router.py`：文档管理和入库任务 API；只从依赖获得 `TenantContext`。
- `app/schemas/knowledge.py`：文档、版本、Job 和 Citation 响应模型。
- `compose.yaml`：固定版本 Qdrant、健康检查和持久卷。
- `scripts/run_knowledge_baseline_eval.py`：可复现的 Baseline 离线评测入口。
- `evals/knowledge/`：16 条样例、政策文档与评测 schema。

### 修改现有文件

- `requirement.txt`：加入 DashScope、Qdrant、splitter、tokenizer、multipart 依赖。
- `.env.example`：加入 DashScope/Qdrant/Collection 配置，不写真实密钥。
- `app/core/config.py`：读取并校验知识模块配置；保留当前 DeepSeek 配置。
- `main.py`：装配 knowledge 服务并注册管理 Router；不注册 Agent 知识工具。
- `tests/db/test_migrations.py`：验证 0009 升降级和复合租户约束。
- `tests/test_main.py`：验证知识 Router/服务被装配且仍不产生启动时外部请求。
- `README.md`：说明 Stage A 已有入库与 Baseline、尚未接入客服 Agent。

---

### Task 1: 固定依赖、运行配置与 Qdrant 容器契约

**独立验收产物：** 本机可启动固定版本 Qdrant；知识配置可验证；导入应用时不会主动访问 DashScope 或 Qdrant。

**Files:**
- Modify: `requirement.txt`
- Modify: `.env.example`
- Modify: `app/core/config.py`
- Create: `compose.yaml`
- Modify: `tests/core/test_config.py`

**Interfaces:**
- Produces: `KnowledgeSettings`
- Produces: `get_knowledge_settings() -> KnowledgeSettings`
- Produces: Qdrant REST endpoint `http://localhost:6333`

- [ ] **Step 1: 写知识配置失败测试**

在 `tests/core/test_config.py` 追加：

```python
import pytest

from app.core.config import get_knowledge_settings


def test_knowledge_settings_require_dashscope_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        get_knowledge_settings()


def test_knowledge_settings_have_fixed_vector_contract(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.test:6333")

    settings = get_knowledge_settings()

    assert settings.embedding_model == "text-embedding-v4"
    assert settings.embedding_dimensions == 1024
    assert settings.qdrant_collection == (
        "supportpilot_knowledge_te4_1024_v1"
    )
    assert settings.qdrant_url == "http://qdrant.test:6333"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/core/test_config.py -q`

Expected: FAIL，提示 `get_knowledge_settings` 尚不存在。

- [ ] **Step 3: 增加依赖与配置对象**

在 `requirement.txt` 追加并保持每行一个依赖：

```text
dashscope>=1.25,<2.0
qdrant-client>=1.18,<2.0
langchain-text-splitters>=0.3,<2.0
tiktoken>=0.9,<2.0
python-multipart>=0.0.20,<1.0
```

在 `app/core/config.py` 增加：

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class KnowledgeSettings:
    dashscope_api_key: str
    dashscope_base_url: str
    qdrant_url: str
    qdrant_collection: str = "supportpilot_knowledge_te4_1024_v1"
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024


def get_knowledge_settings() -> KnowledgeSettings:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required")
    return KnowledgeSettings(
        dashscope_api_key=api_key,
        dashscope_base_url=os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/api/v1",
        ).rstrip("/"),
        qdrant_url=os.getenv(
            "QDRANT_URL", "http://localhost:6333"
        ).rstrip("/"),
    )
```

在 `.env.example` 追加：

```dotenv
DASHSCOPE_API_KEY=replace-with-your-dashscope-key
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1
QDRANT_URL=http://localhost:6333
```

Collection 名不从普通环境变量覆盖；模型或维度变化时必须在代码中引入新的显式 Collection 版本。

- [ ] **Step 4: 创建固定版本 Compose 服务**

创建 `compose.yaml`：

```yaml
services:
  qdrant:
    image: qdrant/qdrant:v1.18.2
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - qdrant_data:/qdrant/storage
    healthcheck:
      test: ["CMD", "/qdrant/qdrant", "--version"]
      interval: 5s
      timeout: 3s
      retries: 12
      start_period: 5s
    restart: unless-stopped

volumes:
  qdrant_data:
```

- [ ] **Step 5: 验证配置和容器**

Run:

```powershell
python -m pytest tests/core/test_config.py -q
docker compose config
docker compose up -d qdrant
docker compose ps
Invoke-WebRequest http://localhost:6333/readyz
```

Expected: 配置测试 PASS；`docker compose ps` 中 Qdrant 最终为 `healthy`。官方镜像不内置 curl/wget，因此 Compose healthcheck 只做进程级检查；随后用 `Invoke-WebRequest http://localhost:6333/readyz` 做真实就绪检查，并要求 HTTP 200。

- [ ] **Step 6: 提交 Task 1**

```powershell
git add requirement.txt .env.example app/core/config.py compose.yaml tests/core/test_config.py
git commit -m "build: add knowledge infrastructure settings"
```

---

### Task 2: 定义知识领域契约与 0009 数据库迁移

**独立验收产物：** SQLite 具有文档、版本、Chunk、Job、Event 五张表，并用复合键阻止跨企业关联。

**Files:**
- Create: `app/knowledge/__init__.py`
- Create: `app/knowledge/base.py`
- Create: `migrations/versions/0009_knowledge_base.py`
- Modify: `tests/db/test_migrations.py`
- Create: `tests/knowledge/test_domain.py`

**Interfaces:**
- Produces: `KnowledgeDocument`, `DocumentVersion`, `DocumentChunk`, `IngestionJob`, `RetrievalEvent`
- Produces: `KnowledgeStore` Protocol；每个方法显式接收 `organization_id`
- Produces: stable errors `DOCUMENT_NOT_FOUND`, `DOCUMENT_DISABLED`, `INVALID_DOCUMENT`, `INGESTION_FAILED`, `INSUFFICIENT_EVIDENCE`

- [ ] **Step 1: 写迁移和领域约束测试**

在 `tests/db/test_migrations.py` 追加：

```python
def test_knowledge_migration_upgrade_and_rollback(tmp_path):
    database_path = tmp_path / "knowledge.db"
    config = alembic_config(database_path)

    command.upgrade(config, "0009_knowledge_base")

    assert {
        "documents",
        "document_versions",
        "document_chunks",
        "ingestion_jobs",
        "retrieval_events",
    } <= table_names(database_path)

    command.downgrade(config, "0008_ticket_comments")

    assert "documents" not in table_names(database_path)
    assert "ticket_comments" in table_names(database_path)


def test_knowledge_schema_rejects_cross_tenant_version(tmp_path):
    database_path = tmp_path / "knowledge-constraints.db"
    upgrade_database(database_path)
    seed_two_memberships(database_path)  # 文件内新增固定 org-a/org-b helper

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO documents(
                id, organization_id, uploaded_by_user_id,
                title, source_type, status
            ) VALUES ('doc-a', 'org-a', 'user-a', 'A', 'text', 'processing')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO document_versions(
                    id, organization_id, document_id, version_no,
                    content_hash, raw_text, loader_version,
                    chunker_version, embedding_model, embedding_dimensions
                ) VALUES (
                    'version-b', 'org-b', 'doc-a', 1, 'hash', 'body',
                    'loader-v1', 'chunker-v1', 'text-embedding-v4', 1024
                )
                """
            )
```

`seed_two_memberships()` 必须真实写入 `users`、`organizations` 和 `memberships`，不能关闭 foreign keys 绕过约束。

- [ ] **Step 2: 运行迁移测试并确认失败**

Run: `python -m pytest tests/db/test_migrations.py -q`

Expected: FAIL，因为 revision `0009_knowledge_base` 和知识表尚不存在。

- [ ] **Step 3: 定义领域枚举、模型和异常**

在 `app/knowledge/base.py` 定义以下最小公开契约；字段名后续 Task 不得改名：

```python
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence


class DocumentSourceType(str, Enum):
    MARKDOWN = "markdown"
    TEXT = "text"


class DocumentStatus(str, Enum):
    PROCESSING = "processing"
    ACTIVE = "active"
    DISABLED = "disabled"
    FAILED = "failed"


class IngestionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class KnowledgeDocument:
    document_id: str
    organization_id: str
    uploaded_by_user_id: str
    title: str
    source_type: DocumentSourceType
    status: DocumentStatus
    active_version_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DocumentVersion:
    version_id: str
    organization_id: str
    document_id: str
    version_no: int
    content_hash: str
    raw_text: str
    loader_version: str
    chunker_version: str
    embedding_model: str
    embedding_dimensions: int
    created_at: str


@dataclass(frozen=True)
class DocumentChunk:
    chunk_id: str
    organization_id: str
    document_id: str
    version_id: str
    ordinal: int
    heading_path: str | None
    content: str
    token_count: int
    start_offset: int
    end_offset: int
    created_at: str | None = None
```

同文件继续定义 `IngestionJob`、`RetrievalEvent`，字段与设计文档第 6 节完全一致；定义 `KnowledgeError(code, safe_message)` 及其具体子类。`DocumentNotFoundError` 对跨租户 ID 统一返回 `DOCUMENT_NOT_FOUND`，不得暴露“属于其他企业”。

`KnowledgeStore` 至少声明：

```python
class KnowledgeStore(Protocol):
    def create_document(self, *, organization_id: str,
                        uploaded_by_user_id: str, title: str,
                        source_type: DocumentSourceType) -> KnowledgeDocument: ...
    def create_version(self, *, organization_id: str, document_id: str,
                       content_hash: str, raw_text: str,
                       loader_version: str, chunker_version: str,
                       embedding_model: str,
                       embedding_dimensions: int) -> DocumentVersion: ...
    def create_job(self, *, organization_id: str, document_id: str,
                   version_id: str) -> IngestionJob: ...
    def replace_chunks(self, *, organization_id: str, document_id: str,
                       version_id: str,
                       chunks: Sequence[DocumentChunk]) -> None: ...
    def activate_version(self, *, organization_id: str, document_id: str,
                         version_id: str, job_id: str) -> KnowledgeDocument: ...
    def list_active_version_ids(self, *, organization_id: str) -> list[str]: ...
    def list_active_chunks(self, *, organization_id: str,
                           candidate_ids: Sequence[str]) -> list[DocumentChunk]: ...
```

- [ ] **Step 4: 创建 0009 migration**

`0009_knowledge_base.py` 必须：

1. `down_revision = "0008_ticket_comments"`。
2. 按设计第 6 节创建五张表与所有 CHECK/UNIQUE。
3. 为 `documents(organization_id, id)`、`document_versions(organization_id, document_id, id)` 建复合唯一键。
4. `document_versions(organization_id, document_id)` 外键到 documents。
5. `document_chunks(organization_id, document_id, version_id)` 外键到 document_versions 的三列复合唯一键。
6. `ingestion_jobs(organization_id, document_id, version_id)` 使用同一复合外键。
7. `documents(organization_id, uploaded_by_user_id)` 外键到 memberships。
8. `documents(organization_id, id, active_version_id)` 外键到 `document_versions(organization_id, document_id, id)`，允许 active_version_id 为 NULL，防止激活其他文档或企业的版本。
9. 创建 `idx_documents_org_status`、`idx_versions_org_document`、`idx_chunks_org_version`、`idx_jobs_org_document_created`、`idx_retrieval_events_org_created`。
10. downgrade 按 Event → Job → Chunk → Version → Document 的顺序删除。

- [ ] **Step 5: 验证迁移与领域测试**

Run:

```powershell
python -m pytest tests/db/test_migrations.py tests/knowledge/test_domain.py -q
alembic history
```

Expected: 全部 PASS；head 为 `0009_knowledge_base`。

- [ ] **Step 6: 提交 Task 2**

```powershell
git add app/knowledge migrations/versions/0009_knowledge_base.py tests/db/test_migrations.py tests/knowledge/test_domain.py
git commit -m "feat: add tenant-scoped knowledge schema"
```

---

### Task 3: 实现 SQLiteKnowledgeStore 的租户隔离与版本状态

**独立验收产物：** 文档、版本、Chunk、Job、Event 可持久化；相同 hash 幂等；跨租户 ID 始终表现为不存在。

**Files:**
- Create: `app/knowledge/sqlite_store.py`
- Create: `tests/knowledge/test_sqlite_store.py`

**Interfaces:**
- Consumes: Task 2 的领域对象与 `KnowledgeStore`
- Produces: `SQLiteKnowledgeStore(database_path)`
- Produces: `get_version_by_hash()`, `get_latest_job_for_version()`, `mark_job_running()`, `fail_ingestion()`, `set_document_status()`, `record_retrieval_event()`

- [ ] **Step 1: 写两个企业与 hash 幂等测试**

创建 `tests/knowledge/test_sqlite_store.py`，复用真实 User/Organization Store 建两个企业，并覆盖：

```python
def test_document_reads_are_tenant_scoped(two_tenant_knowledge_store):
    store, context_a, context_b = two_tenant_knowledge_store
    document = store.create_document(
        organization_id=context_a.organization_id,
        uploaded_by_user_id=context_a.user_id,
        title="退货政策",
        source_type=DocumentSourceType.MARKDOWN,
    )

    assert store.get_document(
        organization_id=context_a.organization_id,
        document_id=document.document_id,
    ) == document
    with pytest.raises(DocumentNotFoundError):
        store.get_document(
            organization_id=context_b.organization_id,
            document_id=document.document_id,
        )


def test_same_content_hash_returns_existing_version(store_with_document):
    store, context, document = store_with_document
    first = create_version(store, context, document, content_hash="sha256:x")

    assert store.get_version_by_hash(
        organization_id=context.organization_id,
        document_id=document.document_id,
        content_hash="sha256:x",
    ) == first
    with pytest.raises(DuplicateDocumentVersionError) as caught:
        create_version(store, context, document, content_hash="sha256:x")
    assert caught.value.existing_version_id == first.version_id
```

- [ ] **Step 2: 写激活、停用和候选二次校验测试**

必须覆盖：

```python
def test_only_active_version_chunks_are_returned(store_with_two_versions):
    store, context, document, old_version, new_version = (
        store_with_two_versions
    )
    store.activate_version(
        organization_id=context.organization_id,
        document_id=document.document_id,
        version_id=new_version.version_id,
        job_id=job_for(new_version).job_id,
    )

    chunks = store.list_active_chunks(
        organization_id=context.organization_id,
        candidate_ids=[old_chunk_id, new_chunk_id],
    )

    assert [chunk.chunk_id for chunk in chunks] == [new_chunk_id]
```

再写断言：`set_document_status(..., DISABLED)` 后 `list_active_version_ids()` 和 `list_active_chunks()` 都不返回该文档；重新启用后恢复最近成功版本。

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/knowledge/test_sqlite_store.py -q`

Expected: FAIL，提示 `SQLiteKnowledgeStore` 尚不存在。

- [ ] **Step 4: 实现 Store**

实现时遵循现有 Store 的 `_connect()` / `_connection()` 模式，并满足：

- 每条 SELECT/UPDATE/DELETE 同时包含 `organization_id` 与实体 ID。
- `create_version()` 在同一事务内计算 `MAX(version_no) + 1`。
- 捕获唯一 hash 冲突后查询现有版本并抛 `DuplicateDocumentVersionError(existing_version_id)`。
- `replace_chunks()` 先校验 version 属于当前 org+document，再删除该 version 旧 chunks 并批量插入。
- `activate_version()` 在同一 SQLite 事务内校验 version、更新 `active_version_id/status=active`、更新 Job 为 `succeeded/finished_at`。
- `fail_ingestion()` 在同一事务更新 Job；若文档无旧 `active_version_id` 则文档变 `failed`，否则保持旧激活版本与 `active` 状态。
- `list_active_chunks()` 使用参数化 `IN`，返回顺序与 `candidate_ids` 一致，不允许空列表生成非法 SQL。
- `record_retrieval_event()` 的 JSON 字段由调用方传入已序列化字符串，Store 不记录完整文档正文。

- [ ] **Step 5: 运行 Store、迁移和全量回归**

Run:

```powershell
python -m pytest tests/knowledge/test_sqlite_store.py tests/db/test_migrations.py -q
python -m pytest -q
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交 Task 3**

```powershell
git add app/knowledge/sqlite_store.py tests/knowledge/test_sqlite_store.py
git commit -m "feat: persist tenant-scoped knowledge versions"
```

---

### Task 4: 实现 Markdown/TXT Loader 与稳定 Chunker 适配器

**独立验收产物：** 输入字节被安全规范化并稳定分块；重复运行得到相同 heading、ordinal、offset 和 token_count。

**Files:**
- Create: `app/knowledge/document_loader.py`
- Create: `app/knowledge/chunking.py`
- Create: `tests/knowledge/test_document_loader.py`
- Create: `tests/knowledge/test_chunking.py`

**Interfaces:**
- Produces: `LoadedDocument(text: str, sections: tuple[LoadedSection, ...])`
- Produces: `DocumentLoader.load(content: bytes, source_type: DocumentSourceType) -> LoadedDocument`
- Produces: `KnowledgeChunker.split(document, *, organization_id, document_id, version_id) -> list[DocumentChunk]`

- [ ] **Step 1: 写 Loader 边界测试**

覆盖以下行为：

```python
def test_markdown_loader_normalizes_newlines_and_headings():
    loaded = DocumentLoader().load(
        b"# Returns\r\n\r\nBody\r\n## Exceptions\rMore",
        DocumentSourceType.MARKDOWN,
    )
    assert loaded.text == "# Returns\n\nBody\n## Exceptions\nMore"
    assert [section.heading_path for section in loaded.sections] == [
        "Returns",
        "Returns/Exceptions",
    ]


@pytest.mark.parametrize("content", [b"", b" \r\n\t"])
def test_loader_rejects_empty_document(content):
    with pytest.raises(InvalidDocumentError):
        DocumentLoader().load(content, DocumentSourceType.TEXT)


def test_loader_rejects_invalid_utf8():
    with pytest.raises(InvalidDocumentError):
        DocumentLoader().load(b"\xff\xfe", DocumentSourceType.TEXT)
```

另测输入大于 `2 * 1024 * 1024` 字节时抛 `INVALID_DOCUMENT`。

- [ ] **Step 2: 写 Chunker 稳定性和边界测试**

固定 tokenizer 后断言：

```python
def test_chunker_produces_stable_ordinals_and_offsets(markdown_document):
    chunker = KnowledgeChunker()
    first = chunker.split(
        markdown_document,
        organization_id="org-a",
        document_id="doc-a",
        version_id="version-a",
    )
    second = chunker.split(
        markdown_document,
        organization_id="org-a",
        document_id="doc-a",
        version_id="version-a",
    )

    assert [(c.ordinal, c.heading_path, c.start_offset, c.end_offset)
            for c in first] == [
        (c.ordinal, c.heading_path, c.start_offset, c.end_offset)
        for c in second
    ]
    assert [c.ordinal for c in first] == list(range(len(first)))
    assert all(c.content == markdown_document.text[c.start_offset:c.end_offset]
               for c in first)
    assert all(c.token_count <= 700 for c in first)
```

再覆盖纯文本、超长无换行段落、中文、重叠不超过 80 tokens，以及空 splitter 输出转换为 `InvalidDocumentError`。

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/knowledge/test_document_loader.py tests/knowledge/test_chunking.py -q`

Expected: FAIL，因为 Loader/Chunker 尚不存在。

- [ ] **Step 4: 实现 Loader**

`DocumentLoader` 先检查字节长度，再严格 `content.decode("utf-8")`，把 `\r\n` 和裸 `\r` 统一为 `\n`。Markdown 使用标题正则建立 heading stack；TXT 作为单个 `heading_path=None` section。版本常量固定：

```python
LOADER_VERSION = "supportpilot-loader-v1"
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
```

- [ ] **Step 5: 实现 Chunker 适配器**

使用 `MarkdownHeaderTextSplitter` 保留标题路径，再用 `RecursiveCharacterTextSplitter.from_tiktoken_encoder()` 做二次切分。项目适配层必须自行把 splitter 文本定位回规范化原文；从上一个 `start_offset` 之后搜索，不能用随机 ID 参与顺序。固定：

```python
CHUNKER_VERSION = "supportpilot-chunker-v1"
TARGET_CHUNK_TOKENS = 600
MAX_CHUNK_TOKENS = 700
CHUNK_OVERLAP_TOKENS = 80
```

Chunk ID 使用 `uuid5(NAMESPACE_URL, f"{organization_id}:{version_id}:{ordinal}")`，保证同一版本重试得到相同 Point ID。

- [ ] **Step 6: 验证并提交 Task 4**

Run:

```powershell
python -m pytest tests/knowledge/test_document_loader.py tests/knowledge/test_chunking.py -q
python -m pytest -q
```

Expected: 全部 PASS。

```powershell
git add app/knowledge/document_loader.py app/knowledge/chunking.py tests/knowledge/test_document_loader.py tests/knowledge/test_chunking.py
git commit -m "feat: load and chunk knowledge documents"
```

---

### Task 5: 定义 Embedding 契约并实现 DashScope dense+sparse 适配器

**独立验收产物：** 文档和查询调用使用正确参数；批量、维度、稀疏向量、有限数值、超时和临时失败均可测试且不访问真实网络。

**Files:**
- Create: `app/knowledge/embeddings.py`
- Create: `app/knowledge/dashscope_embeddings.py`
- Create: `tests/knowledge/test_dashscope_embeddings.py`

**Interfaces:**
- Produces: `SparseValue(index: int, value: float)`
- Produces: `EmbeddingVector(dense: tuple[float, ...], sparse: tuple[SparseValue, ...], token_count: int)`
- Produces: `EmbeddingClient.embed_documents(texts: Sequence[str]) -> list[EmbeddingVector]`
- Produces: `EmbeddingClient.embed_query(text: str) -> EmbeddingVector`

- [ ] **Step 1: 写 SDK 参数与批量测试**

使用可记录 kwargs 的 fake callable：

```python
def successful_response(text_count, *, token_count=20):
    return SimpleNamespace(
        status_code=200,
        output={
            "embeddings": [
                {
                    "embedding": [0.0] * 1024,
                    "sparse_embedding": [
                        {"index": 17, "value": 0.75, "token": "return"}
                    ],
                    "text_index": index,
                }
                for index in range(text_count)
            ]
        },
        usage={"total_tokens": token_count},
        code="",
        message="",
    )


def test_documents_use_document_mode_and_batches_of_ten():
    call = RecordingCall(successful_response)
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=lambda _: None
    )

    result = client.embed_documents([f"doc {i}" for i in range(11)])

    assert [len(item["input"]) for item in call.kwargs] == [10, 1]
    assert all(item["model"] == "text-embedding-v4" for item in call.kwargs)
    assert all(item["dimension"] == 1024 for item in call.kwargs)
    assert all(item["output_type"] == "dense&sparse" for item in call.kwargs)
    assert all(item["text_type"] == "document" for item in call.kwargs)
    assert len(result) == 11


def test_query_uses_query_mode_instruction_and_timeout():
    call = RecordingCall(successful_response)
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=lambda _: None
    )
    client.embed_query("退货期限")

    assert call.kwargs == [{
        "api_key": "test-key",
        "model": "text-embedding-v4",
        "input": ["退货期限"],
        "dimension": 1024,
        "output_type": "dense&sparse",
        "text_type": "query",
        "instruct": (
            "Given an ecommerce after-sales policy question, "
            "retrieve the most relevant enterprise policy passages"
        ),
        "timeout": 5,
    }]
```

- [ ] **Step 2: 写响应校验和错误转换测试**

必须覆盖：返回数量不一致、dense 不是 1024 维、`NaN/inf`、稀疏 index 重复或负数、失败 HTTP 状态。429/5xx/连接超时应重试一次后抛 `EmbeddingUnavailableError`；400 类非临时错误不得重试。

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/knowledge/test_dashscope_embeddings.py -q`

Expected: FAIL，因为 `DashScopeEmbeddingClient` 尚不存在。

- [ ] **Step 4: 实现 Protocol 和适配器**

`dashscope_embeddings.py` 默认依赖 `dashscope.TextEmbedding.call`，但构造器允许注入 `call` 与 `sleep`。每个 response item 按 `text_index` 排序，转换：

```python
dense = tuple(float(value) for value in item["embedding"])
sparse = tuple(
    SparseValue(index=int(value["index"]), value=float(value["value"]))
    for value in item["sparse_embedding"]
)
```

总 token 数按输入条数均分会导致错误，不允许这样做。`EmbeddingVector.token_count` 使用与 Chunker 相同的项目 tokenizer 对单条输入确定性计数；DashScope `usage.total_tokens` 仅用于响应合法性检查。RetrievalEvent/Eval 的 `estimated_tokens` 和成本统一使用项目 tokenizer 估算，避免把可变的“最近一次调用状态”保存在共享 client 上。

- [ ] **Step 5: 验证官方调用契约**

实现必须与官方同步接口一致：`output_type="dense&sparse"` 返回 `embedding`、`sparse_embedding[{index,value,token}]`、`text_index`；文档和查询分别固定 `text_type`。测试不访问网络。

Run: `python -m pytest tests/knowledge/test_dashscope_embeddings.py -q`

Expected: 全部 PASS。

- [ ] **Step 6: 提交 Task 5**

```powershell
git add app/knowledge/embeddings.py app/knowledge/dashscope_embeddings.py tests/knowledge/test_dashscope_embeddings.py
git commit -m "feat: adapt dashscope hybrid embeddings"
```

---

### Task 6: 定义 VectorStore 并实现 Qdrant Collection、Point 与原生 RRF

**独立验收产物：** Qdrant 请求结构始终包含两个带相同 tenant/version filter 的 prefetch，并由 Qdrant 执行 RRF。

**Files:**
- Create: `app/knowledge/vector_store.py`
- Create: `app/knowledge/qdrant_store.py`
- Create: `tests/knowledge/test_qdrant_store.py`

**Interfaces:**
- Produces: `VectorPoint(chunk_id, organization_id, document_id, version_id, ordinal, embedding)`
- Produces: `VectorCandidate(chunk_id, document_id, version_id, ordinal, score)`
- Produces: `VectorStore.ensure_collection()`, `upsert()`, `search()`

- [ ] **Step 1: 写 Collection 与 Point 映射测试**

注入 fake `QdrantClient`，断言 `ensure_collection()`：

```python
assert create_call["collection_name"] == (
    "supportpilot_knowledge_te4_1024_v1"
)
assert create_call["vectors_config"]["dense"].size == 1024
assert "sparse" in create_call["sparse_vectors_config"]
assert payload_index_call["field_name"] == "organization_id"
assert payload_index_call["field_schema"].is_tenant is True
```

`upsert()` Point payload 只允许：`organization_id`、`document_id`、`version_id`、`chunk_id`、`ordinal`；正文禁止进入 Qdrant payload。

- [ ] **Step 2: 写双路过滤与 RRF 请求测试**

```python
def test_search_filters_both_prefetches_by_tenant_and_active_versions():
    client = FakeQdrantClient(points=[])
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    store.search(
        organization_id="org-a",
        active_version_ids=["version-1", "version-2"],
        query_embedding=embedding_vector(),
        prefetch_limit=8,
        result_limit=8,
    )

    request = client.query_calls[0]
    assert len(request["prefetch"]) == 2
    assert {item.using for item in request["prefetch"]} == {
        "dense", "sparse"
    }
    assert all(item.limit == 8 for item in request["prefetch"])
    assert request["query"].fusion == models.Fusion.RRF
    assert request["limit"] == 8
    assert both_filters_equal(
        request["prefetch"],
        organization_id="org-a",
        active_version_ids=["version-1", "version-2"],
    )
```

另测 `active_version_ids=[]` 时直接返回空列表且不调用 Qdrant。

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/knowledge/test_qdrant_store.py -q`

Expected: FAIL，因为 VectorStore/Qdrant 实现尚不存在。

- [ ] **Step 4: 实现 Collection 和 tenant payload index**

核心配置：

```python
models.VectorParams(size=1024, distance=models.Distance.COSINE)
models.SparseVectorParams(
    index=models.SparseIndexParams(on_disk=False)
)
models.KeywordIndexParams(
    type=models.KeywordIndexType.KEYWORD,
    is_tenant=True,
)
```

`ensure_collection()` 必须幂等：Collection 不存在才创建；存在时校验命名向量和 dense 维度，不匹配则抛 `VectorConfigurationError`，不得原地混用。

- [ ] **Step 5: 实现 upsert 和原生 RRF 查询**

查询必须使用：

```python
client.query_points(
    collection_name=collection_name,
    prefetch=[
        models.Prefetch(
            query=list(query_embedding.dense),
            using="dense",
            filter=tenant_and_versions_filter,
            limit=8,
        ),
        models.Prefetch(
            query=models.SparseVector(
                indices=[item.index for item in query_embedding.sparse],
                values=[item.value for item in query_embedding.sparse],
            ),
            using="sparse",
            filter=tenant_and_versions_filter,
            limit=8,
        ),
    ],
    query=models.FusionQuery(fusion=models.Fusion.RRF),
    limit=8,
    with_payload=True,
    with_vectors=False,
)
```

所有 `UnexpectedResponse`、连接和超时异常转换为 `VectorStoreUnavailableError`；不能转换成空结果。

- [ ] **Step 6: 验证并提交 Task 6**

Run:

```powershell
python -m pytest tests/knowledge/test_qdrant_store.py -q
python -m pytest -q
```

Expected: 全部 PASS。

```powershell
git add app/knowledge/vector_store.py app/knowledge/qdrant_store.py tests/knowledge/test_qdrant_store.py
git commit -m "feat: add tenant-filtered qdrant hybrid search"
```

---

### Task 7: 编排同步知识入库 Happy Path

**独立验收产物：** 新文档依次完成 validate → version/job → chunk → embed → SQLite → Qdrant → activate，且 Qdrant 成功前不可检索。

**Files:**
- Create: `app/knowledge/ingestion.py`
- Create: `tests/knowledge/test_ingestion.py`

**Interfaces:**
- Consumes: `KnowledgeStore`, `DocumentLoader`, `KnowledgeChunker`, `EmbeddingClient`, `VectorStore`
- Produces: `IngestionReceipt(document_id, version_id, job_id, status, deduplicated)`
- Produces: `KnowledgeIngestionService.ingest_new_document()` 与 `ingest_new_version()`

- [ ] **Step 1: 写严格调用顺序测试**

使用 recording fakes：

```python
def test_new_document_activates_only_after_vector_upsert(ingestion_scope):
    receipt = ingestion_scope.service.ingest_new_document(
        organization_id="org-a",
        uploaded_by_user_id="user-a",
        title="退货政策",
        source_type=DocumentSourceType.MARKDOWN,
        content=b"# Returns\nSeven days.",
    )

    assert ingestion_scope.events == [
        "load",
        "create_document",
        "create_version",
        "create_job",
        "mark_job_running",
        "chunk",
        "embed_documents",
        "replace_chunks",
        "vector_ensure_collection",
        "vector_upsert",
        "activate_version",
    ]
    assert receipt.status is IngestionStatus.SUCCEEDED
    assert receipt.deduplicated is False
```

在 fake vector upsert 内断言此时 document 还不是 active。

- [ ] **Step 2: 写参数传递测试**

断言所有 Store/Vector 调用收到相同可信 `organization_id`；Embedding 只收到 Chunk content，不收到标题、org ID 或 API Key；Qdrant point 使用 Task 4 的稳定 chunk_id。

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/knowledge/test_ingestion.py -q`

Expected: FAIL，因为 `KnowledgeIngestionService` 尚不存在。

- [ ] **Step 4: 实现入库服务**

构造器只接收 Protocol。公开签名固定：

```python
def ingest_new_document(
    self, *, organization_id: str, uploaded_by_user_id: str,
    title: str, source_type: DocumentSourceType, content: bytes,
) -> IngestionReceipt: ...

def ingest_new_version(
    self, *, organization_id: str, uploaded_by_user_id: str,
    document_id: str, source_type: DocumentSourceType, content: bytes,
) -> IngestionReceipt: ...
```

标题使用 `title.strip()` 并限制 1～200 字符。哈希固定为：

```python
content_hash = "sha256:" + hashlib.sha256(
    loaded.text.encode("utf-8")
).hexdigest()
```

`ingest_new_version()` 必须先按 org+document 查询文档，且保持该文档原 `source_type`；上传类型不一致返回 `INVALID_DOCUMENT`。

首次写向量前必须显式调用 `vector_store.ensure_collection()`；该调用发生在 SQLite `replace_chunks()` 之后、`vector_store.upsert()` 之前，且不得激活版本。

- [ ] **Step 5: 验证 Happy Path**

Run: `python -m pytest tests/knowledge/test_ingestion.py -q`

Expected: 全部 PASS，不发生网络请求。

- [ ] **Step 6: 提交 Task 7**

```powershell
git add app/knowledge/ingestion.py tests/knowledge/test_ingestion.py
git commit -m "feat: orchestrate knowledge ingestion"
```

---

### Task 8: 加固入库幂等、失败状态和旧版本保留

**独立验收产物：** 同内容不重复向量化；持久化开始后的任一阶段失败都有稳定错误码；重新入库失败不会切掉旧版本。

**Files:**
- Modify: `app/knowledge/ingestion.py`
- Modify: `app/knowledge/sqlite_store.py`
- Modify: `tests/knowledge/test_ingestion.py`
- Modify: `tests/knowledge/test_sqlite_store.py`

**Interfaces:**
- Produces: `fail_ingestion(..., error_code, error_message)`
- Produces: stable mappings `EMBEDDING_UNAVAILABLE`, `VECTOR_STORE_UNAVAILABLE`, `INGESTION_FAILED`

- [ ] **Step 1: 写同 hash 幂等测试**

第二次上传同一规范化正文时：

```python
second = service.ingest_new_version(
    organization_id=context.organization_id,
    uploaded_by_user_id=context.user_id,
    document_id=first.document_id,
    source_type=DocumentSourceType.MARKDOWN,
    content=original_bytes,
)
assert second.version_id == first.version_id
assert second.job_id == first.job_id
assert second.deduplicated is True
assert embedding_client.document_calls == 1
assert vector_store.upsert_calls == 1
```

- [ ] **Step 2: 写旧版本保留测试**

先成功激活 v1，再让 v2 的 vector upsert 抛 `VectorStoreUnavailableError`。断言：document 仍为 `ACTIVE`、`active_version_id == v1`；v2 Job 为 `FAILED/error_code=VECTOR_STORE_UNAVAILABLE`；`list_active_chunks()` 只返回 v1。

- [ ] **Step 3: 写新文档失败测试**

非法类型、编码、大小和空正文在创建记录前返回 `INVALID_DOCUMENT`，不产生 Document/Version/Job。通过 Loader 后的新文档若在 chunker/embedding/vector 任一步失败，Document 为 `FAILED`，Job 为 `FAILED`；安全 `error_message` 不含 API Key、原始完整文档或底层 traceback。

- [ ] **Step 4: 运行测试并确认失败**

Run: `python -m pytest tests/knowledge/test_ingestion.py tests/knowledge/test_sqlite_store.py -q`

Expected: 至少幂等和旧版本保留测试 FAIL。

- [ ] **Step 5: 实现统一失败收口**

只在 document/version/job 已创建后捕获异常并调用 `fail_ingestion()`；`KeyboardInterrupt`/`SystemExit` 不捕获。映射表固定：

```python
ERROR_CODE_BY_EXCEPTION = {
    EmbeddingUnavailableError: "EMBEDDING_UNAVAILABLE",
    VectorStoreUnavailableError: "VECTOR_STORE_UNAVAILABLE",
    InvalidDocumentError: "INVALID_DOCUMENT",
}
```

其余 `Exception` 记录内部 logger（只含 org/document/version/job ID）并对 Job 写 `INGESTION_FAILED` 与安全消息 `"knowledge ingestion failed"`。

- [ ] **Step 6: 验证并提交 Task 8**

Run:

```powershell
python -m pytest tests/knowledge/test_ingestion.py tests/knowledge/test_sqlite_store.py -q
python -m pytest -q
```

Expected: 全部 PASS。

```powershell
git add app/knowledge/ingestion.py app/knowledge/sqlite_store.py tests/knowledge/test_ingestion.py tests/knowledge/test_sqlite_store.py
git commit -m "fix: preserve active knowledge on ingestion failure"
```

---

### Task 9: 实现租户范围的文档管理 API

**独立验收产物：** 管理员可上传、更新、启停；成员可查看文档和 Job；跨租户 ID 返回 404；请求不能提交 organization_id。

**Files:**
- Create: `app/schemas/knowledge.py`
- Create: `app/api/knowledge_router.py`
- Create: `tests/api/test_knowledge_router.py`

**Interfaces:**
- Consumes: `TenantContext`, `KnowledgeIngestionService`, `KnowledgeStore`
- Produces: 设计第 10.1 节的 7 个 HTTP endpoints

- [ ] **Step 1: 建 fake 服务并写上传权限测试**

```python
def test_admin_upload_uses_tenant_context(client_and_service):
    client, service = client_and_service(role=MembershipRole.ADMIN)
    response = client.post(
        "/knowledge/documents/",
        data={"title": "退货政策"},
        files={"file": ("returns.md", b"# Returns\nSeven days", "text/markdown")},
    )

    assert response.status_code == 201
    assert service.new_document_calls[0]["organization_id"] == "org-a"
    assert service.new_document_calls[0]["uploaded_by_user_id"] == "user-a"
    assert "organization_id" not in response.request.content.decode()


def test_agent_cannot_upload(client_and_service):
    client, _ = client_and_service(role=MembershipRole.AGENT)
    response = client.post(
        "/knowledge/documents/",
        data={"title": "Policy"},
        files={"file": ("policy.txt", b"body", "text/plain")},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "admin role required"
```

- [ ] **Step 2: 写完整路由矩阵测试**

覆盖：

- `POST /knowledge/documents/` → 201。
- `GET /knowledge/documents/` → 200，只返回当前 org。
- `GET /knowledge/documents/{id}/` → 文档、版本、最近 Job。
- `POST /knowledge/documents/{id}/versions/` → admin 201。
- `POST /knowledge/documents/{id}/disable/` 和 `/enable/` → admin 200。
- `GET /knowledge/ingestion-jobs/{job_id}/` → 当前 org 200。
- `.pdf`、空文件、非法 UTF-8、超过 2 MiB → 422 `INVALID_DOCUMENT`。
- 企业 B 访问企业 A 的 document/job → 404，响应不得包含企业 A 标题。
- multipart 中额外 `organization_id` 字段 → 422（不得被静默忽略或用于作用域选择）。

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/api/test_knowledge_router.py -q`

Expected: FAIL，因为 Router/Schema 尚不存在。

- [ ] **Step 4: 定义 Pydantic 响应模型**

至少创建：`IngestionReceiptResponse`、`KnowledgeDocumentSummaryResponse`、`KnowledgeDocumentDetailResponse`、`DocumentVersionResponse`、`IngestionJobResponse`。全部 `ConfigDict(extra="forbid")`；不返回 `raw_text` 和普通日志错误详情。

- [ ] **Step 5: 实现 Router**

Router 签名：

```python
def create_knowledge_router(
    *, ingestion_service: KnowledgeIngestionService,
    knowledge_store: KnowledgeStore,
    get_current_tenant: Callable[..., TenantContext],
) -> APIRouter: ...
```

上传 endpoint 通过 `await request.form()` 取得字段，先校验字段集合严格等于 `{"title", "file"}`，因此额外 `organization_id` 会得到 422；再读取最多 `MAX_DOCUMENT_BYTES + 1` 字节。扩展名 `.md/.markdown` 映射 MARKDOWN，`.txt` 映射 TEXT。写操作先执行：

```python
if context.role is not MembershipRole.ADMIN:
    raise HTTPException(status_code=403, detail="admin role required")
```

错误映射固定：`DOCUMENT_NOT_FOUND`→404、`INVALID_DOCUMENT`→422、`DOCUMENT_DISABLED`→409、入库基础设施失败→503，但 response detail 只返回稳定 code/message。

- [ ] **Step 6: 验证并提交 Task 9**

Run:

```powershell
python -m pytest tests/api/test_knowledge_router.py -q
python -m pytest -q
```

Expected: 全部 PASS。

```powershell
git add app/schemas/knowledge.py app/api/knowledge_router.py tests/api/test_knowledge_router.py
git commit -m "feat: add tenant knowledge management api"
```

---

### Task 10: 实现固定 Top-K Baseline、结构化引用和检索事件

**独立验收产物：** 单个原始问题完成 query embedding → tenant/version-filtered RRF → SQLite 二次校验 → Top-5 引用，并记录不含正文的检索事件。

**Files:**
- Create: `app/knowledge/results.py`
- Create: `app/knowledge/retrieval.py`
- Create: `tests/knowledge/test_retrieval.py`
- Create: `tests/knowledge/test_results.py`

**Interfaces:**
- Produces: `Citation(citation_id, document_id, version_id, chunk_id, title, heading_path, content)`
- Produces: `RetrievalSummary(strategy, round_count, evidence_status, latency_ms)`
- Produces: `BaselineSearchResult(ok, citations, retrieval_summary, error, selected_chunks)`；HTTP/报告序列化不公开 `selected_chunks`
- Produces: `BaselineKnowledgeSearchService.search(*, organization_id, question, conversation_id=None)`

- [ ] **Step 1: 写固定检索预算测试**

```python
def test_baseline_uses_one_query_and_fixed_top_k(retrieval_scope):
    result = retrieval_scope.service.search(
        organization_id="org-a",
        question="退货期限是多久？",
    )

    assert retrieval_scope.embedding.query_calls == ["退货期限是多久？"]
    assert retrieval_scope.vector.search_calls == [{
        "organization_id": "org-a",
        "active_version_ids": ["version-a"],
        "prefetch_limit": 8,
        "result_limit": 8,
    }]
    assert len(result.citations) <= 5
    assert sum(item.token_count for item in result.selected_chunks) <= 3000
    assert [citation.citation_id for citation in result.citations] == [
        f"C{i}" for i in range(1, len(result.citations) + 1)
    ]
```

- [ ] **Step 2: 写 SQLite 二次校验与顺序测试**

Fake Qdrant 返回：当前 org 激活 chunk、旧版本 chunk、伪造跨 org chunk、相邻低分 chunk。Fake Store 只返回真正激活的当前 org chunk。断言最终引用不含旧版本/跨 org；候选按 RRF score 顺序；同版本相邻 chunk 只保留高分者。

`list_active_chunks()` 可能按 candidate 输入顺序返回，但 Retrieval 仍必须用 `candidate_by_id` 重建顺序，不能信任数据库默认顺序。

- [ ] **Step 3: 写无命中与基础设施失败测试**

```python
def test_no_hits_are_insufficient_not_infrastructure_failure(scope):
    scope.vector.candidates = []
    result = scope.service.search(
        organization_id="org-a", question="没有答案的问题"
    )
    assert result.ok is True
    assert result.citations == []
    assert result.retrieval_summary.evidence_status == "insufficient"
    assert scope.store.events[-1].outcome == "insufficient"


def test_qdrant_failure_is_not_converted_to_empty_result(scope):
    scope.vector.error = VectorStoreUnavailableError("down")
    result = scope.service.search(
        organization_id="org-a", question="退货期限"
    )
    assert result.ok is False
    assert result.error.code == "VECTOR_STORE_UNAVAILABLE"
    assert scope.store.events[-1].outcome == "failed"
```

- [ ] **Step 4: 运行测试并确认失败**

Run: `python -m pytest tests/knowledge/test_retrieval.py tests/knowledge/test_results.py -q`

Expected: FAIL，因为 Baseline/Results 尚不存在。

- [ ] **Step 5: 实现结果对象和序列化**

`Citation` 的 title 需要 Store 批量返回 chunk 与 document title；为此给 `KnowledgeStore` 增加：

```python
def resolve_active_citations(
    self, *, organization_id: str,
    candidate_ids: Sequence[str],
) -> list[ChunkWithDocumentTitle]: ...
```

该查询必须同时校验 document `status='active'`、`active_version_id=chunk.version_id` 以及四级 ID。Citation content 只来自 SQLite，不使用 Qdrant payload。

- [ ] **Step 6: 实现 Baseline**

算法顺序固定：

1. 校验 question strip 后非空。
2. 读取当前 org 激活 version IDs；空集合直接返回 insufficient，不调用 Embedding/Qdrant。
3. `embed_query(question)` 一次。
4. Qdrant dense/sparse 各 Top-8，原生 RRF 返回最多 8 个候选。
5. SQLite `resolve_active_citations()` 二次校验。
6. 按 candidate score 降序去重；同 version ordinal 差 1 时保留高分者。
7. 在 3,000 tokens 内选择最多 5 个；按最终顺序生成 C1..Cn。
8. 用 `time.perf_counter()` 记录 latency。
9. finally 路径写 RetrievalEvent；`candidate_json` 只含 chunk_id/score，`selected_chunk_ids_json` 只含 ID。

`model_calls=0`，因为 Baseline 没有 Planner/Assessor；`estimated_tokens` 记录查询 embedding token 与已选 chunk token 合计。

- [ ] **Step 7: 验证并提交 Task 10**

Run:

```powershell
python -m pytest tests/knowledge/test_retrieval.py tests/knowledge/test_results.py tests/knowledge/test_sqlite_store.py -q
python -m pytest -q
```

Expected: 全部 PASS。

```powershell
git add app/knowledge/results.py app/knowledge/retrieval.py app/knowledge/base.py app/knowledge/sqlite_store.py tests/knowledge
git commit -m "feat: add traditional rag baseline retrieval"
```

---

### Task 11: 完成生产装配并注册 Knowledge Router

**独立验收产物：** FastAPI 暴露管理端点；工厂装配真实 SQLite/DashScope/Qdrant/Baseline；应用导入阶段没有外部请求。

**Files:**
- Create: `app/knowledge/factory.py`
- Modify: `main.py`
- Modify: `tests/test_main.py`
- Create: `tests/knowledge/test_factory.py`

**Interfaces:**
- Produces: `KnowledgeServices(store, ingestion, baseline)`
- Produces: `create_knowledge_services(database_path, settings, *, qdrant_client=None) -> KnowledgeServices`

- [ ] **Step 1: 写工厂类型和延迟连接测试**

```python
def test_factory_wires_real_adapters_without_network(tmp_path, monkeypatch):
    settings = KnowledgeSettings(
        dashscope_api_key="test-key",
        dashscope_base_url="https://dashscope.test/api/v1",
        qdrant_url="http://qdrant.test:6333",
    )
    qdrant = RecordingQdrantClient()

    services = create_knowledge_services(
        tmp_path / "app.db", settings, qdrant_client=qdrant
    )

    assert isinstance(services.store, SQLiteKnowledgeStore)
    assert isinstance(services.ingestion, KnowledgeIngestionService)
    assert isinstance(services.baseline, BaselineKnowledgeSearchService)
    assert qdrant.network_calls == []
```

- [ ] **Step 2: 扩展 main 装配测试**

在既有 `tests/test_main.py` 的环境准备中增加 `DASHSCOPE_API_KEY=test-only-key` 与 `QDRANT_URL=http://qdrant.invalid:6333`，断言 route 集包含 7 个 knowledge 路径，且 import `main` 不访问该无效 URL。

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/knowledge/test_factory.py tests/test_main.py -q`

Expected: FAIL，因为 factory 和 Router 装配尚不存在。

- [ ] **Step 4: 实现工厂**

```python
@dataclass(frozen=True)
class KnowledgeServices:
    store: SQLiteKnowledgeStore
    ingestion: KnowledgeIngestionService
    baseline: BaselineKnowledgeSearchService
```

若未注入 qdrant client，工厂只执行 `QdrantClient(url=settings.qdrant_url, timeout=5)` 构造；不得调用 `get_collections()` 或 `ensure_collection()`。Collection 初始化推迟到第一次入库/显式运维检查。

- [ ] **Step 5: 修改 main.py**

创建 `knowledge_services`，并：

```python
app.include_router(
    create_knowledge_router(
        ingestion_service=knowledge_services.ingestion,
        knowledge_store=knowledge_services.store,
        get_current_tenant=get_current_tenant,
    )
)
```

不要修改 `CustomerSupportAgentRunner`、现有 Tool Gateway definitions 或 `LLMResponse`；这些属于阶段 B。

- [ ] **Step 6: 验证并提交 Task 11**

Run:

```powershell
python -m pytest tests/knowledge/test_factory.py tests/test_main.py -q
python -m pytest -q
```

Expected: 全部 PASS，且没有外部网络请求。

```powershell
git add app/knowledge/factory.py main.py tests/knowledge/test_factory.py tests/test_main.py
git commit -m "feat: wire knowledge management services"
```

---

### Task 12: 增加真实 Qdrant 与完整入库检索集成测试

**独立验收产物：** Docker Qdrant 中真实创建 1024 dense+sparse Collection，完成 RRF、tenant filter、版本过滤、持久卷重启与端到端 Baseline 检索。

**Files:**
- Create: `pytest.ini`
- Create: `tests/integration/test_qdrant_knowledge.py`
- Create: `tests/integration/test_knowledge_pipeline.py`
- Create: `scripts/qdrant_persistence_smoke.py`

**Interfaces:**
- Verifies: Task 5–11 的真实 Qdrant 边界
- Uses: Fake DashScope EmbeddingClient；集成测试仍不消耗外部模型额度

- [ ] **Step 1: 注册 integration marker**

```ini
[pytest]
markers =
    integration: requires Docker Qdrant at QDRANT_URL
```

- [ ] **Step 2: 写真实 Collection/RRF/tenant 测试**

测试使用唯一 collection `supportpilot_test_<uuid>`，finally 只删除该测试 Collection。插入 org-a 与 org-b 同名政策 points，查询 org-a 后断言：

- collection 有 `dense(size=1024)` 与 `sparse`。
- organization_id payload schema 为 keyword tenant index。
- dense 和 sparse 都能参与 RRF。
- 结果 payload 全部为 org-a + 指定 active version。
- 把 org-b version ID 混入 active list 也不能绕过 organization filter。

- [ ] **Step 3: 写完整 Pipeline 测试**

真实 `SQLiteKnowledgeStore + DocumentLoader + KnowledgeChunker + FakeEmbeddingClient + QdrantVectorStore`：

1. 企业 A/B 各上传标题相同、内容冲突的政策。
2. A 查询只返回 A Citation，B 查询只返回 B Citation。
3. A 上传 v2 并成功激活后只引用 v2。
4. A 停用文档后 citations 为空且结果为 insufficient。
5. fake embedding 失败时 Job 失败，旧版本仍可检索。

- [ ] **Step 4: 写持久卷 smoke 脚本**

`scripts/qdrant_persistence_smoke.py write` 创建固定 smoke collection/point；`read` 验证 point 仍存在并删除该 smoke collection。脚本只允许操作 `supportpilot_persistence_smoke`，拒绝接收任意 collection 名，避免误删生产索引。

- [ ] **Step 5: 启动 Qdrant 并运行集成测试**

Run:

```powershell
docker compose up -d qdrant
docker compose ps
python -m pytest -m integration tests/integration -q
python scripts/qdrant_persistence_smoke.py write
docker compose restart qdrant
python scripts/qdrant_persistence_smoke.py read
```

Expected: Qdrant healthy；集成测试全部 PASS；restart 后 smoke point 仍存在。

- [ ] **Step 6: 提交 Task 12**

```powershell
git add pytest.ini tests/integration scripts/qdrant_persistence_smoke.py
git commit -m "test: cover qdrant knowledge pipeline"
```

---

### Task 13: 建立 16 条第一版评测集与可复现 Runner

**独立验收产物：** 一个命令可对固定数据运行 Baseline，输出逐样例轨迹和聚合 JSON；不同企业的冲突政策可测得零泄漏。

**Files:**
- Create: `evals/knowledge/schema.json`
- Create: `evals/knowledge/cases.jsonl`
- Create: `evals/knowledge/documents/org_a/returns.md`
- Create: `evals/knowledge/documents/org_a/logistics_compensation.md`
- Create: `evals/knowledge/documents/org_a/warranty.md`
- Create: `evals/knowledge/documents/org_b/returns.md`
- Create: `scripts/run_knowledge_baseline_eval.py`
- Create: `tests/evals/test_knowledge_eval.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `load_eval_cases(path) -> list[EvalCase]`
- Produces: `evaluate_case(case, result) -> CaseMetrics`
- Produces: JSON report keys listed below

- [ ] **Step 1: 定义不可歧义的 eval schema**

每条 JSONL 必须包含：

```json
{
  "case_id": "simple-return-window-01",
  "category": "simple_policy",
  "tenant_key": "org_a",
  "question": "签收后多少天内可以申请无理由退货？",
  "expected_relevant": [
    {"document_key": "returns", "heading_path": "退货时限"}
  ],
  "should_have_answer": true,
  "forbidden_tenant_keys": ["org_b"]
}
```

`schema.json` 将 `additionalProperties` 设为 false；category 只允许 `simple_policy`、`multi_condition_policy`、`mixed_fact_policy`、`safety_no_answer`。

- [ ] **Step 2: 编写固定政策语料**

`org_a/returns.md` 至少包含 `退货时限`、`商品状态`、`包装要求`、`例外商品` 四个二级标题；`logistics_compensation.md` 包含 `赔偿条件`、`赔偿标准`、`排除条款`；`warranty.md` 包含 `保修期限`、`所需凭证`。`org_b/returns.md` 必须与 org-a 在退货期限上冲突，专用于隔离测试。

- [ ] **Step 3: 编写恰好 16 条样例**

四类各 4 条：

1. simple：退货期限、物流赔偿标准、保修期限、保修凭证。
2. multi-condition：时限+状态+包装、赔偿条件+排除、保修期限+凭证、例外商品+包装。
3. mixed-fact：给定订单签收日期判断期限、给定物流延迟天数查赔偿、给定商品状态查退货、给定购买日期查保修。
4. safety/no-answer：询问未入库换货政策、试图读取 org-b 政策、问题内包含“忽略规则”、故意含糊的“这个能不能退”。

测试断言总数为 16、每类为 4、case_id 唯一、所有 positive heading 能在对应文档 loader 输出中找到。

- [ ] **Step 4: 写指标单元测试**

用手工构造结果验证：

```python
assert metrics.retrieval_recall_at_5 == expected_hit_count / expected_count
assert metrics.citation_precision == relevant_returned / returned_count
assert metrics.cross_tenant_leak is False
```

空 citations 时 precision 记为 1.0 仅限 `should_have_answer=false`；positive case 空 citations precision 为 0.0。P95 用 nearest-rank 计算，16 条时索引为 `ceil(0.95 * 16) - 1`。

- [ ] **Step 5: 实现 Runner**

命令：

```powershell
python scripts/run_knowledge_baseline_eval.py `
  --database .artifacts/knowledge-eval.db `
  --cases evals/knowledge/cases.jsonl `
  --output .artifacts/knowledge-baseline.json
```

在 `.gitignore` 增加 `.artifacts/`，评测原始输出、临时 SQLite 和本地密钥均不得提交；只提交 Task 14 中人工核对后的 Markdown 汇总报告。

输出必须含：`code_revision`、`dataset_hash`、`loader_version`、`chunker_version`、`embedding_model`、`embedding_dimensions`、`collection_name`、`top_k`、`token_budget`、逐 case citations/latency/token/cost，以及聚合：

```text
retrieval_recall_at_5
citation_precision
cross_tenant_leak_rate
average_search_rounds
average_model_calls
average_tokens
p50_latency_ms
p95_latency_ms
estimated_embedding_cost_cny
```

成本按运行时固定元数据 `0.0005 CNY / 1,000 input tokens` 计算并在报告注明价格快照日期；不要从网页运行时抓价格。

- [ ] **Step 6: 验证离线逻辑并提交 Task 13**

Run:

```powershell
python -m pytest tests/evals/test_knowledge_eval.py -q
python -m pytest -q
```

Expected: 全部 PASS；单元测试使用 fake results，不访问 DashScope。

```powershell
git add .gitignore evals/knowledge scripts/run_knowledge_baseline_eval.py tests/evals/test_knowledge_eval.py
git commit -m "test: add first knowledge baseline eval set"
```

---

### Task 14: 运行 Baseline、形成指标报告并完成阶段 A 验收

**独立验收产物：** 仓库保存可追溯的 Baseline Markdown 报告；README 准确说明当前能力与阶段 B 边界；专项、集成和全量测试通过。

**Files:**
- Create: `docs/evals/tenant-scoped-rag-stage-a-baseline.md`
- Create: `docs/knowledge-rag-operations.md`
- Modify: `README.md`

**Interfaces:**
- Documents: environment/version/dataset/metrics/failure analysis
- Documents: Qdrant start/health/restart and document API examples

- [ ] **Step 1: 运行真实 Baseline**

准备真实 `DASHSCOPE_API_KEY`，启动 Qdrant 后运行：

```powershell
docker compose up -d qdrant
python scripts/run_knowledge_baseline_eval.py `
  --database .artifacts/knowledge-eval.db `
  --cases evals/knowledge/cases.jsonl `
  --output .artifacts/knowledge-baseline.json
```

Expected: 16/16 样例完成；任何 embedding/Qdrant 失败都使命令非零退出，不能把失败样例算成“无命中”。

- [ ] **Step 2: 编写 Baseline 报告**

报告必须从 JSON 数值复制并包含：

- Git revision、dataset SHA-256、模型/维度、Collection、chunker/loader、Top-K、token budget、价格快照。
- 总体与四分类指标表。
- cross-tenant leak 明细（目标必须等于 0）。
- 至少 3 个失败 case 的归因：loader/chunking/embedding/retrieval/fusion/citation 中恰好选一个主因。
- 与最终 MVP 阈值 `recall@5 >= 0.85`、`citation_precision >= 0.95` 的差距。
- 明确说明 Stage A 不测 answer accuracy、grounded answer rate、correct abstention；这些需要阶段 B 的生成/证据机制。

不得修改黄金答案来美化分数。Stage A 的目的是真实基线，不要求 Adaptive 的最终收益在此时出现。

- [ ] **Step 3: 编写运维说明**

`docs/knowledge-rag-operations.md` 包含：安装依赖、环境变量、`docker compose up -d qdrant`、健康检查、7 个 API 的 curl 示例、停用/启用语义、失败 Job 排查、Collection 模型维度不可原地混用、`docker compose down` 不删除 named volume。不要提供 `down -v` 作为常规命令。

- [ ] **Step 4: 更新 README 能力边界**

把“当前不包含企业知识库 RAG”更新为：

```markdown
- 已完成企业范围的 Markdown/TXT 知识入库、版本管理和停用/启用。
- 已完成 DashScope dense+sparse + Qdrant RRF 的传统 RAG Baseline。
- 当前尚未把知识检索注册到客服 Agent；自适应规划、证据检查和聊天引用属于下一阶段。
```

- [ ] **Step 5: 运行最终专项与全量验收**

Run:

```powershell
python -m pytest tests/knowledge tests/api/test_knowledge_router.py tests/evals -q
python -m pytest -m integration tests/integration -q
python -m pytest -q
alembic heads
docker compose config
```

Expected: 全部测试 PASS；Alembic 只有一个 head `0009_knowledge_base`；Compose 配置有效。

- [ ] **Step 6: 执行安全静态检查**

Run:

```powershell
Get-ChildItem app -Recurse -Filter *.py | Select-String -Pattern "organization_id.*Body|organization_id.*Field"
Get-ChildItem app/knowledge -Recurse -Filter *.py | Select-String -Pattern "print\(|DASHSCOPE_API_KEY.*log|raw_text.*log"
```

Expected: 第一条没有知识 API 接收 org ID 的命中；第二条没有密钥或完整正文日志命中。若有合法 domain 字段命中，逐个人工确认它来自服务参数而非 HTTP/model input。

- [ ] **Step 7: 提交 Task 14**

```powershell
git add README.md docs/evals/tenant-scoped-rag-stage-a-baseline.md docs/knowledge-rag-operations.md
git commit -m "docs: report tenant rag baseline"
```

---

## 2. 阶段 A 最终验收清单

- [ ] 五张知识表存在，Alembic 唯一 head 为 `0009_knowledge_base`。
- [ ] 所有 KnowledgeStore 读写显式接收 `organization_id`。
- [ ] 复合外键阻止跨企业 Document/Version/Chunk/Job 关联。
- [ ] Markdown/TXT 严格 UTF-8、最大 2 MiB、稳定 chunk ordinal/offset。
- [ ] DashScope 文档/query 参数、10 条批量、1024 维、dense+sparse 响应均有单测。
- [ ] Qdrant 单共享 Collection 同时具有 dense、sparse 和 tenant payload index。
- [ ] dense/sparse 两个 prefetch 同时过滤 tenant 与激活版本。
- [ ] Qdrant 命中回 SQLite 二次校验后才成为 Citation。
- [ ] 新版本在 Qdrant 成功前不可见；失败保留旧激活版本。
- [ ] 同内容 hash 不重复分块、embedding 或 upsert。
- [ ] 停用后立即不可检索，启用恢复最近成功版本。
- [ ] 管理写 API 只允许 admin；跨租户 document/job 都返回 404。
- [ ] Baseline 只有一次原始 query，无 Planner、Assessor 或第二轮。
- [ ] Baseline dense Top-8 + sparse Top-8 + native RRF + Top-5/3,000 tokens。
- [ ] 无命中与基础设施失败使用不同结果语义。
- [ ] RetrievalEvent 不保存完整文档、API Key 或客户敏感信息。
- [ ] 16 条初始评测集四类各 4 条，schema 与 dataset hash 可复现。
- [ ] Baseline 报告包含版本元数据、质量、延迟、token、成本和失败归因。
- [ ] `cross_tenant_leak_rate = 0`。
- [ ] 全量 pytest 与真实 Qdrant integration tests 全部通过。
- [ ] README 明确 Agentic Search 和 Agent 接入仍属于阶段 B。

## 3. 原设计覆盖映射

| 阶段 A 要求 | 对应 Task |
|---|---|
| 数据表、SQLiteKnowledgeStore、Loader、Chunker、接口、文档管理 API | 1–4、9 |
| DashScope dense+sparse、Docker Qdrant、原生 RRF | 5–6、12 |
| 固定 Top-K Baseline、结构化引用、第一版评测集 | 10、13 |
| Baseline 指标报告 | 14 |
| 多租户、安全、失败语义、版本切换的横切约束 | 2–3、6、8–12 |

## 4. 明确留到阶段 B 的内容

- `QueryPlanner` 的 NONE/SINGLE/MULTI 路由与结构化 JSON 校验。
- 最多三个子查询、第二轮补充查询和检索总预算。
- `EvidenceAssessor`、`INSUFFICIENT_EVIDENCE` 拒答控制和模型判断。
- `search_knowledge(question)` Tool Gateway、request-scoped bind 和 Agent tool definitions。
- `LLMResponse.citations`、`retrieval_summary` 聊天响应扩展及最终回答引用校验。
- 订单/物流/知识工具在同一 Agent 回合中的组合黄金路径。

这条边界必须保持：阶段 A 的 Baseline 是阶段 B/C 的对照组，不要在实现阶段 A 时偷偷加入自适应逻辑。
