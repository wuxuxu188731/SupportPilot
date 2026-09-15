"""外部解析适配层与解析缓存的测试。

全部离线：用假客户端代替 llama-cloud，用 ``tmp_path`` 代替真实缓存目录。
测试要钉住的核心行为是「重复上传不重复计费」与「失败语义不伪装成文档非法」。
"""

from pathlib import Path

import pytest

from app.knowledge.base import InvalidDocumentError, ParsingUnavailableError
from app.knowledge.llamaparse_cache import LlamaParseCache, cache_key
from app.knowledge.llamaparse_extractor import LlamaParseExtractor


MARKDOWN = "# 标题\n\n正文内容\n"


class FakePage:
    """模拟 LlamaParse 的逐页 markdown 视图。"""

    def __init__(self, markdown: str) -> None:
        self.markdown = markdown


class FakeMarkdownView:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages


class FakeResult:
    def __init__(self, pages: list[FakePage]) -> None:
        self.markdown = FakeMarkdownView(pages)


class FakeUploaded:
    def __init__(self, file_id: str) -> None:
        self.id = file_id


class FakeFiles:
    def __init__(self, recorder: "FakeClient") -> None:
        self._recorder = recorder

    def create(self, *, file, purpose):
        self._recorder.upload_count += 1
        self._recorder.uploaded_names.append(file[0])
        return FakeUploaded(f"file-{self._recorder.upload_count}")


class FakeParsing:
    def __init__(self, recorder: "FakeClient") -> None:
        self._recorder = recorder

    def parse(self, **kwargs):
        self._recorder.parse_count += 1
        if self._recorder.fail_with is not None:
            raise self._recorder.fail_with
        return FakeResult([FakePage(self._recorder.markdown)])


class FakeClient:
    """记录上传/解析次数的假 llama-cloud 客户端。"""

    def __init__(self, markdown: str = MARKDOWN) -> None:
        self.markdown = markdown
        self.fail_with: Exception | None = None
        self.upload_count = 0
        self.parse_count = 0
        self.uploaded_names: list[str] = []
        self.files = FakeFiles(self)
        self.parsing = FakeParsing(self)


def _extractor(client: FakeClient, **kwargs) -> LlamaParseExtractor:
    return LlamaParseExtractor(api_key="llx-test", client=client, **kwargs)


# --------------------------------------------------------------- 基础解析


def test_extract_returns_joined_page_markdown_and_uploads_suffix():
    # 保护行为：逐页 markdown 按顺序拼接，且上报的文件名带正确扩展名
    # ——解析服务靠扩展名选择解析器。
    client = FakeClient()
    client.markdown = "第一页"
    extractor = LlamaParseExtractor(api_key="llx-test", client=client)

    class TwoPageParsing(FakeParsing):
        def parse(self, **kwargs):
            self._recorder.parse_count += 1
            return FakeResult([FakePage("第一页"), FakePage("第二页")])

    client.parsing = TwoPageParsing(client)

    assert extractor.extract(b"%PDF-1.7", suffix=".pdf") == "第一页\n\n第二页"
    assert client.uploaded_names == ["upload.pdf"]


def test_extract_maps_upstream_failure_to_parsing_unavailable():
    # 保护行为：上游任何失败都必须收敛成 PARSING_UNAVAILABLE（解析能力不可用），
    # **不能**伪装成 InvalidDocumentError —— 那等于拿「文档非法」指责用户。
    client = FakeClient()
    client.fail_with = RuntimeError("connection reset")
    extractor = _extractor(client)

    with pytest.raises(ParsingUnavailableError) as excinfo:
        extractor.extract(b"%PDF-1.7", suffix=".pdf")

    assert excinfo.value.code == "PARSING_UNAVAILABLE"
    # reason 只带异常类型名，不带上游响应体。
    assert "RuntimeError" in excinfo.value.reason
    assert "connection reset" not in excinfo.value.reason
    assert "llx-test" not in excinfo.value.reason


def test_extract_rejects_empty_parse_result():
    # 边界情况：解析服务返回空白内容时报「文档为空」，
    # 而不是返回一个空字符串让下游产出无 chunk 的空文档。
    client = FakeClient(markdown="   \n\n  ")
    extractor = _extractor(client)

    with pytest.raises(InvalidDocumentError):
        extractor.extract(b"%PDF-1.7", suffix=".pdf")


def test_extractor_requires_a_non_blank_api_key():
    # 边界情况：空密钥在构造期就拒绝，避免带着无效凭据发起注定失败的上传。
    with pytest.raises(ValueError):
        LlamaParseExtractor(api_key="   ")


# ----------------------------------------------------------------- 缓存


def test_cache_key_depends_on_content_tier_and_version():
    # 保护行为：缓存键必须同时包含内容、档位与版本——只按内容做键会让
    # 「改了档位却读到旧档位结果」这种静默错误发生。
    base = cache_key(b"body", tier="agentic", version="latest")

    assert base == cache_key(b"body", tier="agentic", version="latest")
    assert base != cache_key(b"other", tier="agentic", version="latest")
    assert base != cache_key(b"body", tier="fast", version="latest")
    assert base != cache_key(b"body", tier="agentic", version="2026-07-24")


def test_second_extract_of_identical_bytes_hits_cache(tmp_path):
    # 保护行为：同一份字节第二次提取不得再发网络请求——LlamaParse 按页计费，
    # 而失败重试与重复上传都会走到这里。
    client = FakeClient()
    extractor = _extractor(client, cache=LlamaParseCache(tmp_path))

    first = extractor.extract(b"%PDF-1.7 same-bytes", suffix=".pdf")
    second = extractor.extract(b"%PDF-1.7 same-bytes", suffix=".pdf")

    assert first == second == MARKDOWN
    assert client.upload_count == 1
    assert client.parse_count == 1


def test_different_bytes_do_not_share_a_cache_entry(tmp_path):
    # 边界情况：内容不同必须各解析一次，不能因为「同一个文档目录」而串味。
    client = FakeClient()
    extractor = _extractor(client, cache=LlamaParseCache(tmp_path))

    extractor.extract(b"%PDF-1.7 first", suffix=".pdf")
    extractor.extract(b"%PDF-1.7 second", suffix=".pdf")

    assert client.parse_count == 2


def test_changing_tier_bypasses_the_cache(tmp_path):
    # 保护行为：换档位后必须重新解析，而不是复用另一档位的结果。
    cache = LlamaParseCache(tmp_path)
    fast_client = FakeClient(markdown="fast 结果")
    agentic_client = FakeClient(markdown="agentic 结果")

    fast = _extractor(fast_client, tier="fast", cache=cache)
    agentic = _extractor(agentic_client, tier="agentic", cache=cache)

    assert fast.extract(b"%PDF-1.7 same", suffix=".pdf") == "fast 结果"
    assert agentic.extract(b"%PDF-1.7 same", suffix=".pdf") == "agentic 结果"
    assert agentic_client.parse_count == 1


def test_failed_parse_is_not_cached(tmp_path):
    # 边界情况：失败的解析绝不能写进缓存，否则一次网络抖动会永久污染该文档。
    client = FakeClient()
    client.fail_with = RuntimeError("timeout")
    extractor = _extractor(client, cache=LlamaParseCache(tmp_path))

    with pytest.raises(ParsingUnavailableError):
        extractor.extract(b"%PDF-1.7 same", suffix=".pdf")

    client.fail_with = None
    assert extractor.extract(b"%PDF-1.7 same", suffix=".pdf") == MARKDOWN
    assert client.parse_count == 2


def test_cache_roundtrip_and_miss(tmp_path):
    # 保护行为：写入后能读回；未命中的键返回 None。
    cache = LlamaParseCache(tmp_path)

    assert cache.get("a" * 64) is None
    cache.put("a" * 64, MARKDOWN)
    assert cache.get("a" * 64) == MARKDOWN


def test_cache_ignores_blank_content_and_unreadable_entries(tmp_path):
    # 边界情况：空内容不写入；缓存文件损坏（非法 UTF-8）按未命中处理——
    # 缓存只是优化，它的任何问题都不该让一次本可成功的上传失败。
    cache = LlamaParseCache(tmp_path)

    cache.put("b" * 64, "   \n ")
    assert cache.get("b" * 64) is None

    cache.put("c" * 64, MARKDOWN)
    cache.path_for("c" * 64).write_bytes(b"\xff\xfe\x00")
    assert cache.get("c" * 64) is None


def test_cache_shards_entries_by_digest_prefix(tmp_path):
    # 保护行为：缓存按摘要前两位分片存放，避免单目录堆积过多文件。
    cache = LlamaParseCache(tmp_path)

    path = cache.path_for("ab" + "0" * 62)

    assert path.parent.name == "ab"
    assert path.name == "ab" + "0" * 62 + ".md"
    assert cache.root == Path(tmp_path)


def test_extractor_without_cache_always_parses():
    # 边界情况：未配置缓存时必须每次都真的解析（保持改造前的行为）。
    client = FakeClient()
    extractor = _extractor(client)

    extractor.extract(b"%PDF-1.7 same", suffix=".pdf")
    extractor.extract(b"%PDF-1.7 same", suffix=".pdf")

    assert client.parse_count == 2
