"""入库异步化（阶段 D）的端到端测试。

覆盖三件事：
1. **上传接口不等解析**：``queue_new_document`` 只登记入队，立刻返回 queued，
   解析次数为 0；
2. **worker 把任务推进到终态**：``run_job`` 完成解析 → 分块 → 嵌入 → 写库 → 激活，
   并在成功时清掉暂存的上传原始字节；
3. **重启不留僵尸任务**：``recover_stale_jobs`` 把还能续跑的任务重新排队，
   原始字节已丢失的则如实标失败。

全程使用真实的 SQLite store、真实的 DocumentLoader/KnowledgeChunker 与注入的
假解析器，不联网、不碰 Qdrant。
"""

from __future__ import annotations

import asyncio

import pytest

from app.knowledge.base import (
    DocumentSourceType,
    DocumentStatus,
    IngestionStatus,
    InvalidDocumentError,
    ParsingUnavailableError,
)
from app.knowledge.chunking import KnowledgeChunker
from app.knowledge.document_loader import DocumentLoader
from app.knowledge.embeddings import EmbeddingVector, SparseValue
from app.knowledge.ingestion import KnowledgeIngestionService
from app.knowledge.ingestion_worker import (
    IngestionQueueFullError,
    KnowledgeIngestionWorker,
)
from app.knowledge.sqlite_store import SQLiteKnowledgeStore
from app.knowledge.vector_store import VectorPoint
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore

# 假解析器的固定产物：两个标题层级，足以验证 heading_path 真的被还原。
EXTRACTED_MARKDOWN = (
    "# 售后服务总则\n\n"
    "本总则适用于云舟商城全部售后场景。\n\n"
    "## 2. 售后申请与通用处理流程\n\n"
    "### 2.2 通用处理流程\n\n"
    "商城在收到售后申请后不超过 24 小时内完成响应。\n"
)


class RecordingExtractor:
    """记录调用次数的假解析器，可切换为失败，用来替代 LlamaParse。

    产物按输入字节派生：真实解析器对不同文档必然给出不同正文，若这里恒返回同一份
    Markdown，两份「上传字节不同」的文档会算出相同的 ``content_hash``，从而撞上
    真实存在的 ``(org, document, content_hash)`` 唯一约束——那是测试装置的假象，
    不是被测代码的问题。
    """

    def __init__(self, *, markdown: str | None = None, fail: bool = False):
        self._fixed_markdown = markdown
        self.fail = fail
        self.calls: list[str] = []

    def extract(self, content: bytes, *, suffix: str) -> str:
        self.calls.append(suffix)
        if self.fail:
            raise ParsingUnavailableError(reason="fake llamaparse is down")
        if self._fixed_markdown is not None:
            return self._fixed_markdown
        return (
            "# 售后服务总则\n\n"
            "本总则适用于云舟商城全部售后场景。\n\n"
            "## 2. 售后申请与通用处理流程\n\n"
            "### 2.2 通用处理流程\n\n"
            f"商城在收到售后申请后不超过 24 小时内完成响应。来源指纹 {content.hex()}。\n"
        )


class FakeEmbedding:
    """确定性 1024 维稠密+稀疏向量，按内容哈希区分，避免任何外部调用。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def _vector(self, text: str) -> EmbeddingVector:
        if self.fail:
            from app.knowledge.base import EmbeddingUnavailableError

            raise EmbeddingUnavailableError(reason="fake embedding is down")
        bias = 0.5 + (len(text) % 7) / 100.0
        return EmbeddingVector(
            dense=tuple(bias for _ in range(1024)),
            sparse=(SparseValue(index=1, value=1.0),),
            token_count=len(text),
        )

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)


class RecordingVectorStore:
    """只记录写入点，不做任何网络 I/O；可切换为不可用以模拟向量库故障。"""

    def __init__(self) -> None:
        self.ensure_collections = 0
        self.upserts: list[list[VectorPoint]] = []
        self.fail = False

    def ensure_collection(self) -> None:
        self.ensure_collections += 1

    def upsert(self, *, points) -> None:
        if self.fail:
            from app.knowledge.base import VectorStoreUnavailableError

            raise VectorStoreUnavailableError(reason="fake qdrant is down")
        self.upserts.append(list(points))

    def search(self, **kwargs):
        return []


class Harness:
    """一份装好的异步入库测试装置。"""

    def __init__(self, tmp_path, *, extractor=None, embedding=None):
        self.database_path = tmp_path / "knowledge.db"
        self.store = SQLiteKnowledgeStore(self.database_path)
        users = SQLiteUserStore(self.database_path)
        organizations = SQLiteOrganizationStore(self.database_path)
        user = users.create_user(username="admin", password_hash="hash")
        organization = organizations.create_with_admin(
            name="云舟商城", admin_user_id=user.user_id
        )
        self.organization_id = organization.organization_id
        self.user_id = user.user_id
        self.extractor = extractor or RecordingExtractor()
        self.embedding = embedding or FakeEmbedding()
        self.vectors = RecordingVectorStore()
        self.loader = DocumentLoader(document_extractor=self.extractor)
        self.service = KnowledgeIngestionService(
            store=self.store,
            loader=self.loader,
            chunker=KnowledgeChunker(),
            embedding=self.embedding,
            vector_store=self.vectors,
        )

    def queue_document(self, *, title="售后服务总则", source_type=DocumentSourceType.WORD, content=b"PK\x03\x04docx"):
        return self.service.queue_new_document(
            organization_id=self.organization_id,
            uploaded_by_user_id=self.user_id,
            title=title,
            source_type=source_type,
            content=content,
        )

    def queue_version(self, document_id, *, source_type=DocumentSourceType.WORD, content=b"PK\x03\x04docx"):
        """对同一份文档再传一版 —— 上传期去重只作用于文档内部。"""
        return self.service.queue_new_version(
            organization_id=self.organization_id,
            uploaded_by_user_id=self.user_id,
            document_id=document_id,
            source_type=source_type,
            content=content,
        )

    def job(self, job_id):
        return self.store.get_latest_job_for_version(
            organization_id=self.organization_id,
            document_id="",
            version_id="",
            job_id=job_id,
        )

    def version(self, version_id, document_id):
        return self.store.get_version_by_id(
            organization_id=self.organization_id,
            document_id=document_id,
            version_id=version_id,
        )


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


# ---------------------------------------------------------------- queue 阶段


def test_queue_new_document_returns_queued_without_parsing(harness):
    # 保护行为：上传只登记入队就返回，解析次数必须为 0 —— 这正是「接口不再
    # 等待 27 秒解析」的核心，也是大文档不再撞上传超时的原因。
    receipt = harness.queue_document()

    assert receipt.status is IngestionStatus.QUEUED
    assert receipt.deduplicated is False
    assert harness.extractor.calls == []

    # 任务确实落库为 queued，且原始字节已暂存待 worker 取用。
    job = harness.job(receipt.job_id)
    assert job.status is IngestionStatus.QUEUED
    assert job.version_id == receipt.version_id
    assert (
        harness.store.read_version_raw_bytes(
            organization_id=harness.organization_id,
            document_id=receipt.document_id,
            version_id=receipt.version_id,
        )
        == b"PK\x03\x04docx"
    )
    # 文档处于 processing，尚未激活任何版本。
    document = harness.store.get_document(
        organization_id=harness.organization_id,
        document_id=receipt.document_id,
    )
    assert document.status is DocumentStatus.PROCESSING
    assert document.active_version_id is None


def test_queue_rejects_non_utf8_text_before_creating_rows(harness):
    # 边界情况：文本类文档的编码校验必须仍在上传期完成，否则用户要等一个
    # 必然失败的后台任务才知道文件有问题，而库里会多出一份永远无法入库的文档。
    with pytest.raises(InvalidDocumentError):
        harness.queue_document(
            source_type=DocumentSourceType.MARKDOWN,
            content=b"\xff\xfe",
        )

    assert harness.store.list_documents(organization_id=harness.organization_id) == []


def test_queue_rejects_empty_payload(harness):
    # 边界情况：空文件同样是上传期错误，不应创建一个必然失败的任务。
    with pytest.raises(InvalidDocumentError):
        harness.queue_document(source_type=DocumentSourceType.PDF, content=b"   ")

    assert harness.store.list_documents(organization_id=harness.organization_id) == []


# ----------------------------------------------------------------- run_job


def test_run_job_parses_chunks_embeds_and_activates(harness):
    # 保护行为：worker 执行任务后，版本激活、分块落库、向量写入，
    # 并且标题层级被真实还原（heading_path 是本项目证据定位的硬依赖）。
    receipt = harness.queue_document()

    result = harness.service.run_job(
        organization_id=harness.organization_id,
        job_id=receipt.job_id,
    )

    assert result is not None
    assert result.status is IngestionStatus.SUCCEEDED
    assert result.deduplicated is False
    assert harness.extractor.calls == [".docx"]

    job = harness.job(receipt.job_id)
    assert job.status is IngestionStatus.SUCCEEDED
    assert job.error_code is None
    assert job.attempt_count == 1

    document = harness.store.get_document(
        organization_id=harness.organization_id,
        document_id=receipt.document_id,
    )
    assert document.status is DocumentStatus.ACTIVE
    assert document.active_version_id == receipt.version_id

    chunks = harness.store.list_version_chunks(
        organization_id=harness.organization_id,
        document_id=receipt.document_id,
        version_id=receipt.version_id,
    )
    assert chunks, "解析结果必须真的被切成 chunk"
    assert any(
        chunk.heading_path == "售后服务总则/2. 售后申请与通用处理流程/2. 通用处理流程"
        or chunk.heading_path
        == "售后服务总则/2. 售后申请与通用处理流程/2.2 通用处理流程"
        for chunk in chunks
    )
    assert harness.vectors.upserts and len(harness.vectors.upserts[0]) == len(chunks)


def test_successful_run_clears_staged_upload_bytes(harness):
    # 保护行为：解析成功后必须清空暂存的上传原始字节 —— 否则每份上传（最大 2MiB）
    # 会永远留在数据库里，把库撑大。
    receipt = harness.queue_document()
    harness.service.run_job(
        organization_id=harness.organization_id, job_id=receipt.job_id
    )

    assert (
        harness.store.read_version_raw_bytes(
            organization_id=harness.organization_id,
            document_id=receipt.document_id,
            version_id=receipt.version_id,
        )
        is None
    )
    version = harness.version(receipt.version_id, receipt.document_id)
    assert version.content_hash is not None
    assert version.raw_text is not None


def test_run_job_is_idempotent_for_a_finished_job(harness):
    # 边界情况：同一个任务被重复投递（worker 重启后的重新入队）时，
    # 第二次必须直接返回 None，绝不能二次解析、二次嵌入或二次写向量。
    receipt = harness.queue_document()
    harness.service.run_job(
        organization_id=harness.organization_id, job_id=receipt.job_id
    )
    parses_after_first_run = len(harness.extractor.calls)

    again = harness.service.run_job(
        organization_id=harness.organization_id, job_id=receipt.job_id
    )

    assert again is None
    assert harness.job(receipt.job_id).attempt_count == 1
    assert len(harness.extractor.calls) == parses_after_first_run
    assert len(harness.vectors.upserts) == 1


def test_run_job_unknown_job_returns_none(harness):
    # 边界情况：不存在的任务 id 不应抛异常（投递方拿不到任务时可能投错）。
    assert (
        harness.service.run_job(
            organization_id=harness.organization_id, job_id="missing-job"
        )
        is None
    )


def test_parse_failure_is_recorded_on_the_job_not_on_the_upload(harness):
    # 保护行为：解析服务不可用必须在**任务记录**上留下稳定错误码
    # PARSING_UNAVAILABLE，而不是被当成「文档非法」（INVALID_DOCUMENT）——
    # 用户上传的文件没有任何问题。
    harness.extractor.fail = True
    receipt = harness.queue_document()

    with pytest.raises(ParsingUnavailableError):
        harness.service.run_job(
            organization_id=harness.organization_id, job_id=receipt.job_id
        )

    job = harness.job(receipt.job_id)
    assert job.status is IngestionStatus.FAILED
    assert job.error_code == "PARSING_UNAVAILABLE"
    assert job.attempt_count == 1
    assert "llamaparse" not in (job.error_message or "")

    document = harness.store.get_document(
        organization_id=harness.organization_id,
        document_id=receipt.document_id,
    )
    assert document.status is DocumentStatus.FAILED
    # 原始字节保留：这是一次可重试的失败，重试不该再要求用户传一遍。
    assert (
        harness.store.read_version_raw_bytes(
            organization_id=harness.organization_id,
            document_id=receipt.document_id,
            version_id=receipt.version_id,
        )
        is not None
    )


def test_retry_after_a_post_parse_failure_reuses_stored_text_without_reparsing(
    harness,
):
    # 保护行为：解析成功之后的失败（这里让向量库不可用）重跑时，必须直接复用已落库
    # 的正文，绝不重新解析 —— 外部解析按页计费，重复解析就是重复花钱。
    #
    # 用同一个版本行重跑是**真实的重试路径**：Celery 式的「失败任务重新入队」会走
    # ``claim_job`` → ``run_job``，而版本行上的 ``content_hash`` 已经回填，
    # ``_load_version_content`` 因此走「已解析」分支。
    harness.vectors.fail = True
    receipt = harness.queue_document()
    with pytest.raises(Exception):
        harness.service.run_job(
            organization_id=harness.organization_id, job_id=receipt.job_id
        )
    assert len(harness.extractor.calls) == 1, "第一次必须真的解析过"
    failed_job = harness.job(receipt.job_id)
    assert failed_job.status is IngestionStatus.FAILED
    assert failed_job.error_code == "VECTOR_STORE_UNAVAILABLE"
    assert failed_job.attempt_count == 1

    # 解析产物已经落库：正文在、原始字节已清空。
    version = harness.version(receipt.version_id, receipt.document_id)
    assert version.content_hash is not None
    assert version.raw_text is not None
    assert (
        harness.store.read_version_raw_bytes(
            organization_id=harness.organization_id,
            document_id=receipt.document_id,
            version_id=receipt.version_id,
        )
        is None
    )

    # 环境恢复 + 重新入队：同一个任务重跑，必须直接复用正文而不是重新解析。
    harness.vectors.fail = False
    harness.store.recover_stale_jobs()
    assert failed_job.status is IngestionStatus.FAILED
    with harness.store._connection() as connection:  # noqa: SLF001 - 模拟重新入队
        connection.execute(
            "UPDATE ingestion_jobs SET status = 'queued' WHERE id = ?",
            (receipt.job_id,),
        )
    harness.service.run_job(
        organization_id=harness.organization_id, job_id=receipt.job_id
    )

    assert len(harness.extractor.calls) == 1, "重试不得重新解析"
    assert harness.job(receipt.job_id).status is IngestionStatus.SUCCEEDED
    assert harness.job(receipt.job_id).attempt_count == 2
    document = harness.store.get_document(
        organization_id=harness.organization_id,
        document_id=receipt.document_id,
    )
    assert document.active_version_id == receipt.version_id


def test_missing_staged_bytes_fail_with_a_dedicated_code(harness):
    # 边界情况：版本行既没有已解析正文、也读不到暂存字节时（数据被外部清理），
    # 必须给出「上传内容已不可用」这一独立编码，而不是误报成文档非法或解析不可用。
    receipt = harness.queue_document()
    with harness.store._connection() as connection:  # noqa: SLF001 - 模拟外部清库
        connection.execute(
            "UPDATE document_versions SET raw_bytes = NULL WHERE id = ?",
            (receipt.version_id,),
        )

    with pytest.raises(Exception):
        harness.service.run_job(
            organization_id=harness.organization_id, job_id=receipt.job_id
        )

    job = harness.job(receipt.job_id)
    assert job.status is IngestionStatus.FAILED
    assert job.error_code == "INGESTION_SOURCE_MISSING"


# ------------------------------------------------------- 去重（不重复计费）


def test_reuploading_same_bytes_reuses_the_reserved_version_without_reparsing(harness):
    # 保护行为：同一份文件重复上传不得触发第二次解析（LlamaParse 按页计费），
    # 也不得产生第二个版本行。
    first = harness.queue_document()
    harness.service.run_job(
        organization_id=harness.organization_id, job_id=first.job_id
    )
    assert len(harness.extractor.calls) == 1

    second = harness.queue_version(first.document_id)

    assert second.deduplicated is True
    assert second.version_id == first.version_id
    assert second.status is IngestionStatus.SUCCEEDED
    assert len(harness.extractor.calls) == 1


def test_identical_bytes_in_a_new_version_are_deduplicated(harness):
    # 保护行为：对已有文档上传「字节完全相同」的新版本，同样应识别为重复，
    # 不再解析、不再新建版本。
    first = harness.queue_document()
    harness.service.run_job(
        organization_id=harness.organization_id, job_id=first.job_id
    )

    duplicate = harness.queue_version(first.document_id)

    assert duplicate.deduplicated is True
    assert duplicate.version_id == first.version_id
    assert len(harness.extractor.calls) == 1


def test_new_version_with_different_bytes_creates_a_new_parse(harness):
    # 保护行为：内容变了就必须重新解析并生成新版本，去重不能把真实变更吞掉。
    first = harness.queue_document()
    harness.service.run_job(
        organization_id=harness.organization_id, job_id=first.job_id
    )

    second = harness.queue_version(
        first.document_id, content=b"PK\x03\x04docx-v2-changed"
    )
    assert second.version_id != first.version_id

    harness.service.run_job(
        organization_id=harness.organization_id, job_id=second.job_id
    )

    assert len(harness.extractor.calls) == 2
    document = harness.store.get_document(
        organization_id=harness.organization_id,
        document_id=first.document_id,
    )
    assert document.active_version_id == second.version_id


# ------------------------------------------------------------ 重启恢复


def test_recover_requeues_a_running_job_that_can_still_be_resumed(harness):
    # 保护行为：进程在解析途中退出后留下的 running 任务，只要暂存字节还在，
    # 就必须回到 queued 并被重新排队 —— 这是「进程内 worker 会丢任务」的补丁。
    receipt = harness.queue_document()
    assert (
        harness.store.claim_job(
            organization_id=harness.organization_id, job_id=receipt.job_id
        )
        is not None
    )
    assert harness.job(receipt.job_id).status is IngestionStatus.RUNNING

    stale = harness.store.recover_stale_jobs()

    assert stale == [(harness.organization_id, receipt.job_id)]
    assert harness.job(receipt.job_id).status is IngestionStatus.QUEUED

    # 重新执行后能正常跑完（续跑而不是被判死）。
    harness.service.run_job(
        organization_id=harness.organization_id, job_id=receipt.job_id
    )
    assert harness.job(receipt.job_id).status is IngestionStatus.SUCCEEDED


def test_recover_fails_a_running_job_whose_bytes_are_gone(harness):
    # 边界情况：running 且暂存字节已丢失的任务确实无法续跑，必须如实标失败，
    # 不能留下永远 running 的僵尸任务。
    receipt = harness.queue_document()
    harness.store.claim_job(
        organization_id=harness.organization_id, job_id=receipt.job_id
    )
    with harness.store._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE document_versions SET raw_bytes = NULL WHERE id = ?",
            (receipt.version_id,),
        )

    stale = harness.store.recover_stale_jobs()

    assert stale == []
    job = harness.job(receipt.job_id)
    assert job.status is IngestionStatus.FAILED
    assert job.error_code == "INGESTION_INTERRUPTED"


def test_recover_is_a_noop_when_every_job_is_terminal(harness):
    # 边界情况：没有遗留任务时恢复必须是空操作，不能误改已成功的任务。
    receipt = harness.queue_document()
    harness.service.run_job(
        organization_id=harness.organization_id, job_id=receipt.job_id
    )

    assert harness.store.recover_stale_jobs() == []
    assert harness.job(receipt.job_id).status is IngestionStatus.SUCCEEDED


# ---------------------------------------------------------------- worker


def test_worker_end_to_end_queues_and_settles_the_job(harness):
    # 保护行为：把 worker 真正跑起来后，入队的任务会被消费并推进到终态，
    # 这是「上传立刻返回 queued、随后由轮询看到 succeeded」的完整链路。
    worker = KnowledgeIngestionWorker(
        service=harness.service, store=harness.store
    )
    harness.service.set_dispatcher(worker)

    async def scenario():
        worker.bind_loop()
        try:
            receipt = harness.queue_document()
            assert receipt.status is IngestionStatus.QUEUED
            await asyncio.wait_for(worker.drain(), timeout=30)
            return receipt
        finally:
            await worker.stop()

    receipt = asyncio.run(scenario())

    assert harness.job(receipt.job_id).status is IngestionStatus.SUCCEEDED
    document = harness.store.get_document(
        organization_id=harness.organization_id,
        document_id=receipt.document_id,
    )
    assert document.active_version_id == receipt.version_id


def test_worker_enqueue_without_a_bound_loop_is_a_safe_noop(harness):
    # 边界情况：装配完成但事件循环还没绑定时（测试/脚本场景），入队必须是
    # 安全的空操作，任务留在 queued 等下一次 recover()，而不是抛错或丢任务。
    worker = KnowledgeIngestionWorker(
        service=harness.service, store=harness.store
    )
    harness.service.set_dispatcher(worker)

    receipt = harness.queue_document()

    assert receipt.status is IngestionStatus.QUEUED
    assert worker.pending == 0
    assert harness.job(receipt.job_id).status is IngestionStatus.QUEUED


def test_worker_recover_requeues_and_executes_stale_jobs(harness):
    # 保护行为：重启恢复不仅要重新排队，还要真的把它跑完 —— 只排队不消费
    # 等于任务换个状态继续卡住。
    stale_receipt = harness.queue_document()
    harness.store.claim_job(
        organization_id=harness.organization_id, job_id=stale_receipt.job_id
    )

    worker = KnowledgeIngestionWorker(
        service=harness.service, store=harness.store
    )
    harness.service.set_dispatcher(worker)

    async def scenario():
        worker.bind_loop()
        try:
            recovered = worker.recover()
            await asyncio.wait_for(worker.drain(), timeout=30)
            return recovered
        finally:
            await worker.stop()

    recovered = asyncio.run(scenario())

    assert recovered == [(harness.organization_id, stale_receipt.job_id)]
    assert harness.job(stale_receipt.job_id).status is IngestionStatus.SUCCEEDED
    assert harness.job(stale_receipt.job_id).attempt_count == 2


# 保护行为：保留同步入库入口时，真实任务记录仍应只计一次执行。
def test_synchronous_ingestion_counts_one_attempt(harness):
    receipt = harness.service.ingest_new_document(
        organization_id=harness.organization_id,
        uploaded_by_user_id=harness.user_id,
        title="同步入库",
        source_type=DocumentSourceType.MARKDOWN,
        content=b"# Policy\n\nBody",
    )
    assert harness.job(receipt.job_id).attempt_count == 1


def test_worker_keeps_consuming_after_a_job_fails(harness):
    # 边界情况：一个任务失败不能打死消费者 —— 否则后续上传会永远停在 queued。
    harness.extractor.fail = True
    worker = KnowledgeIngestionWorker(
        service=harness.service, store=harness.store
    )
    harness.service.set_dispatcher(worker)

    async def scenario():
        worker.bind_loop()
        try:
            # 必须在绑定事件循环之后入队：worker 未绑定时的入队是刻意的空操作。
            failing = harness.queue_document(content=b"PK\x03\x04broken")
            await asyncio.wait_for(worker.drain(), timeout=30)
            # 修好解析器后再传一份，消费者必须仍然活着。
            harness.extractor.fail = False
            healthy = harness.queue_document(content=b"PK\x03\x04healthy")
            await asyncio.wait_for(worker.drain(), timeout=30)
            return failing, healthy
        finally:
            await worker.stop()

    failing, healthy = asyncio.run(scenario())

    assert harness.job(failing.job_id).status is IngestionStatus.FAILED
    assert harness.job(failing.job_id).error_code == "PARSING_UNAVAILABLE"
    assert harness.job(healthy.job_id).status is IngestionStatus.SUCCEEDED


def test_worker_rejects_enqueue_when_the_queue_is_full(harness):
    # 边界情况：队列满时必须明确抛出，而不是静默丢任务 —— 静默丢弃会让上传
    # 永远停在 queued 且没有任何线索。
    worker = KnowledgeIngestionWorker(
        service=harness.service, store=harness.store, capacity=1
    )
    harness.service.set_dispatcher(worker)

    async def scenario():
        worker.bind_loop()
        try:
            worker.enqueue(harness.organization_id, "job-a")
            with pytest.raises(IngestionQueueFullError):
                worker.enqueue(harness.organization_id, "job-b")
        finally:
            await worker.stop()

    asyncio.run(scenario())
