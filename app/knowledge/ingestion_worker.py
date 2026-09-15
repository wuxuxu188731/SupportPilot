"""进程内入库 worker：把解析与入库从 HTTP 请求线程里搬出去。

为什么需要
----------
上传接口原来是 ``async def`` 却同步调用整条入库流水线，**直接阻塞事件循环**；
而单份 DOCX/PDF 的外部解析实测约 27 秒，更大的 PDF 必然撞上前端 180 秒上传超时。
接线后接口只做「登记 + 入队」并立刻返回 ``queued``，真正的重活由本模块执行。

为什么是「进程内」
------------------
任务清单把 worker 形态列为二选一：进程内后台任务（简单，但进程重启丢任务）
或独立 worker 进程（要处理抢占与超时回收）。这里按「先用前者打通」落地，并补上了
前者最容易丢的那部分能力：

* **任务不丢**：上传的原始字节随版本行一起落库，进程重启后
  :meth:`KnowledgeIngestionWorker.recover` 能把遗留任务重新排队，重新解析并入库；
* **不重复解析**：外部解析结果另有按「原始字节 + 档位 + 版本」的缓存，
  重试不会二次计费（见 :mod:`app.knowledge.llamaparse_cache`）；
* **不产生僵尸任务**：字节已经丢失、确实无法续跑的任务会被如实标失败，
  而不是永远停在 ``running``。

串行执行的取舍
--------------
消费者只有一个，且任务跑在只有单线程的执行器里：入库是「解析 + embedding +
写 SQLite + 写 Qdrant」的重活，SQLite 又只有单写者。串行让写锁竞争与 Qdrant 写入
顺序都变得确定，代价是并发上传排队等待——对本项目的单机部署完全够用。要提吞吐应
改为独立 worker 进程 + 任务抢占，而不是简单地把线程数调大。
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from app.knowledge.base import KnowledgeStore
from app.knowledge.ingestion import KnowledgeIngestionService

_logger = logging.getLogger(__name__)

# 队列容量：单线程执行器一次只跑一个任务，队列只需要容纳「已登记但还没轮到」的任务。
# 取一个足够大的固定值而不是无界队列，避免上传洪水把内存吃满。
DEFAULT_QUEUE_CAPACITY = 256
# 关闭时等待当前任务收尾的秒数：外部解析可能长达数分钟，不能无限期拖住进程退出。
# 超时后当前任务会被放弃，但它已经是 running 状态且原始字节仍在库里，
# 因此下次启动会被 recover() 重新排队，不会静默丢失。
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 30.0


class IngestionQueueFullError(RuntimeError):
    """Raised when the in-process queue cannot accept another job.

    上传已经落库（document/version/job 都建好了），只是暂时排不进执行队列。
    调用方应把它当作「稍后重试」而不是「上传失败」——任务记录仍在，
    重启恢复或下一次重试都能把它捡回来。
    """


class KnowledgeIngestionWorker:
    """Runs queued ingestion jobs one at a time on the application's event loop.

    Usage::

        worker = KnowledgeIngestionWorker(service=ingestion, store=store)
        worker.bind_loop()          # inside the running event loop (lifespan)
        worker.recover()            # re-queue jobs left by the previous process
        await worker.stop()         # on shutdown

    ``service`` must already have this worker injected as its dispatcher; that is
    what :func:`app.knowledge.factory.create_knowledge_services` wires up.
    """

    def __init__(
        self,
        *,
        service: KnowledgeIngestionService,
        store: KnowledgeStore,
        capacity: int = DEFAULT_QUEUE_CAPACITY,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self._service = service
        self._store = store
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(
            maxsize=capacity
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._consumer: asyncio.Task[None] | None = None
        # 专用单线程执行器：入库任务又慢又重，不应占用 asyncio 的默认线程池
        # （那会拖慢其它 to_thread 调用），同时保证任意时刻只有一个任务在写库。
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="knowledge-ingest"
        )

    # ------------------------------------------------------------- lifecycle

    @property
    def pending(self) -> int:
        """当前排队中的任务数（仅诊断与测试使用）。"""
        return self._queue.qsize()

    @property
    def running(self) -> bool:
        """消费者是否已经启动（仅诊断与测试使用）。"""
        return self._consumer is not None and not self._consumer.done()

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Capture the event loop and start the single consumer task.

        Must be called from inside a running loop (the application lifespan).
        """
        self._loop = loop or asyncio.get_running_loop()
        if self._consumer is None or self._consumer.done():
            self._consumer = self._loop.create_task(self._consume())

    def enqueue(self, organization_id: str, job_id: str) -> None:
        """Schedule a job for execution. Never blocks on the work itself.

        Called synchronously from the upload path. When no loop has been bound
        yet (tests, scripts, or a worker-less deployment) this is a deliberate
        no-op: the job row stays ``queued`` and is picked up by :meth:`recover`
        the next time a worker starts. That keeps the upload path correct even
        when nothing is consuming the queue yet.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            self._queue.put_nowait((organization_id, job_id))
        except asyncio.QueueFull as exc:
            raise IngestionQueueFullError(
                "ingestion queue is full; retry shortly"
            ) from exc

    def recover(self) -> list[tuple[str, str]]:
        """Re-queue jobs left behind by a previous process.

        Returns the ``(organization_id, job_id)`` pairs handed back to the queue,
        so callers (and tests) can assert exactly what was resumed.
        """
        stale = self._store.recover_stale_jobs()
        for organization_id, job_id in stale:
            self.enqueue(organization_id, job_id)
        if stale:
            _logger.warning(
                "knowledge ingestion recovered %d stale job(s) after restart",
                len(stale),
            )
        return stale

    async def drain(self) -> None:
        """Wait until every already-enqueued job has been executed.

        Used on shutdown (and by tests) so work accepted before the stop is not
        abandoned mid-queue.
        """
        await self._queue.join()

    async def stop(self) -> None:
        """Stop the consumer, giving the in-flight job a bounded grace period.

        Anything still queued is durable in SQLite, so a job abandoned here is
        recovered by :meth:`recover` on the next start rather than lost.
        """
        if self._consumer is not None:
            self._consumer.cancel()
            try:
                await self._consumer
            except asyncio.CancelledError:
                pass
            self._consumer = None
        # wait=False：外部解析可能远超关闭预算，用 shutdown 的 join 施加超时，
        # 而不是让进程退出无限期挂住。
        self._executor.shutdown(wait=False, cancel_futures=False)
        self._loop = None

    # -------------------------------------------------------------- internal

    async def _consume(self) -> None:
        """Consume the queue forever, one job at a time."""
        while True:
            organization_id, job_id = await self._queue.get()
            try:
                await self._execute(organization_id, job_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单个任务失败不能打死消费者
                _logger.exception(
                    "knowledge ingestion worker failed a job; "
                    "organization_id=%s job_id=%s",
                    organization_id,
                    job_id,
                )
            finally:
                self._queue.task_done()

    async def _execute(self, organization_id: str, job_id: str) -> None:
        """Run one job in the dedicated thread so the event loop stays free.

        ``run_in_executor`` 只接位置参数，而 ``run_job`` 的关键字是 keyword-only，
        因此用 ``partial`` 绑定，而不是把参数改成位置形式（那会破坏调用方的可读性）。
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor,
            partial(
                self._service.run_job,
                organization_id=organization_id,
                job_id=job_id,
            ),
        )
