"""LlamaParse 文档提取适配器：把 docx/pdf 字节提取成 Markdown 文本。

为什么单独一层
--------------
现有 ``DocumentLoader`` 只能本地解码 Markdown/TXT，PDF 完全处理不了，
DOCX 也只能靠 python-docx 拿到「一整块没有标题层级的正文」。本模块把
「调用外部解析服务」这件事收敛成一个可注入的小接口：

* ``DocumentLoader`` 只依赖 ``extract(content, suffix) -> str`` 这个形状，
  生产环境注入真实的 LlamaParse 客户端，测试注入假实现，**单测不需要联网**；
* 解析失败统一收敛成 :class:`ParsingUnavailableError`，不会伪装成
  「文档非法」或「文档为空」。

取哪个视图
----------
只取 ``markdown`` 视图。实测 ``text`` 视图会丢掉全部标题标记
（heading 数 = 0），并引入 PDF 硬换行与孤立页码行；而本项目 RAG 的证据定位
依赖 ``heading_path``，用 text 视图会让它整体退化成 ``None``。
"""

from __future__ import annotations

from typing import Protocol

from app.knowledge.base import InvalidDocumentError, ParsingUnavailableError

# 默认解析档位：agentic 是面向复杂版式的高质量档位，也是本次评估实际验证过的档位。
DEFAULT_TIER = "agentic"
DEFAULT_VERSION = "latest"
# 单份文档的解析超时；解析是同步轮询等待，需要给大文档留足时间。
DEFAULT_TIMEOUT_SECONDS = 1800.0
# 请求的视图集合。只取 markdown，理由见模块 docstring。
_EXPAND_VIEWS = ["markdown"]
# 上传给解析服务的文件名：服务端靠扩展名判断文档格式，因此必须与来源类型一致。
UPLOAD_STEM = "upload"


class DocumentExtractor(Protocol):
    """把富文本文档的原始字节提取成 Markdown 文本。"""

    def extract(self, content: bytes, *, suffix: str) -> str:
        """返回文档的 Markdown 文本；失败时抛 :class:`ParsingUnavailableError`。"""
        raise NotImplementedError


class LlamaParseExtractor:
    """基于 llama-cloud 的文档提取器。

    ``client`` 可注入：测试用假客户端即可覆盖上传/解析/异常处理三条分支，
    无需网络与真实密钥。未注入时按需构造真实客户端，密钥仅从调用方传入，
    本类不读取任何环境变量，也不记录密钥。
    """

    def __init__(
        self,
        *,
        api_key: str,
        tier: str = DEFAULT_TIER,
        version: str = DEFAULT_VERSION,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: object | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("LLAMA_CLOUD_API_KEY is required")
        self._api_key = api_key
        self._tier = tier
        self._version = version
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def tier(self) -> str:
        """当前解析档位，供装配层与运维日志读取。"""
        return self._tier

    def _resolve_client(self):
        """延迟导入并构造客户端。

        延迟导入有两个好处：未安装 llama-cloud 时其余功能照常可用；
        ``create_knowledge_services`` 在装配期不会因为导入而做任何网络 I/O。
        """
        if self._client is not None:
            return self._client
        from llama_cloud import LlamaCloud

        self._client = LlamaCloud(api_key=self._api_key)
        return self._client

    def extract(self, content: bytes, *, suffix: str) -> str:
        """上传并同步解析一份文档，返回其 Markdown 视图。

        上传时带上 ``(文件名, 字节)`` 二元组而不是裸字节：解析服务靠扩展名
        选择解析器，裸字节会让它无从判断格式。
        """
        client = self._resolve_client()
        filename = f"{UPLOAD_STEM}{suffix}"
        try:
            uploaded = client.files.create(
                file=(filename, content),
                purpose="parse",
            )
            result = client.parsing.parse(
                file_id=uploaded.id,
                tier=self._tier,
                version=self._version,
                expand=list(_EXPAND_VIEWS),
                timeout=self._timeout_seconds,
            )
        except Exception as exc:
            # 上游的任何失败（网络、鉴权、限流、额度、超时）都是「解析能力
            # 不可用」，而不是「用户传了坏文件」。reason 只带异常类型名，
            # 不带上游响应体，避免密钥或文档内容经由错误信息外泄。
            raise ParsingUnavailableError(
                reason=f"llamaparse request failed ({type(exc).__name__})"
            ) from exc

        markdown = self._join_markdown_pages(result)
        if not markdown.strip():
            raise InvalidDocumentError(
                reason="document parsing produced no text"
            )
        return markdown

    @staticmethod
    def _join_markdown_pages(result: object) -> str:
        """把逐页 markdown 拼成整篇文本，页与页之间保留一个空行。"""
        markdown_view = getattr(result, "markdown", None)
        pages = list(getattr(markdown_view, "pages", []) or []) if markdown_view else []
        chunks = [
            getattr(page, "markdown", "") or ""
            for page in pages
        ]
        return "\n\n".join(chunk for chunk in chunks if chunk.strip())
