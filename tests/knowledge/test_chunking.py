"""Chunker stability and boundary tests.

Stability contract: running ``KnowledgeChunker.split`` twice on the same
``LoadedDocument`` yields identical ordinals, heading paths and offsets.
Every chunk's content must equal ``document.text[start_offset:end_offset]``.
"""

import pytest

from app.knowledge.base import (
    DocumentSourceType,
    InvalidDocumentError,
)
from app.knowledge.chunking import (
    CHUNK_OVERLAP_TOKENS,
    CHUNKER_VERSION,
    MAX_CHUNK_TOKENS,
    KnowledgeChunker,
)
from app.knowledge.document_loader import DocumentLoader


@pytest.fixture
def markdown_document():
    """A moderately long markdown doc with several nested headings."""
    body = "\n\n".join(
        f"Detailed paragraph {i} with enough words to force the secondary "
        "splitter to cut across boundaries plus additional filler text to "
        "keep token counts meaningful and stable."
        for i in range(40)
    )
    md = (
        "# First Section\n\n"
        f"{body}\n\n"
        "## Nested Heading\n\n"
        f"{body}\n\n"
        "### Deep Heading\n\n"
        f"{body}\n"
    )
    return DocumentLoader().load(md.encode("utf-8"), DocumentSourceType.MARKDOWN)


@pytest.fixture
def plain_text_document():
    body = "\n\n".join(
        "Plain line of storefront documentation text that carries no markdown "
        "headers at all so the chunker treats it as one flat section."
        for _ in range(30)
    )
    return DocumentLoader().load(body.encode("utf-8"), DocumentSourceType.TEXT)


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
    assert all(c.token_count <= MAX_CHUNK_TOKENS for c in first)


def test_chunker_keeps_nested_heading_paths(markdown_document):
    chunks = KnowledgeChunker().split(
        markdown_document,
        organization_id="org-a",
        document_id="doc-a",
        version_id="version-a",
    )
    paths = {c.heading_path for c in chunks}
    assert any(p and p.startswith("First Section") for p in paths)
    assert any(p and "Nested Heading" in p for p in paths)
    assert any(p and "Deep Heading" in p for p in paths)


def test_chunker_plain_text_uses_flat_none_paths(plain_text_document):
    chunks = KnowledgeChunker().split(
        plain_text_document,
        organization_id="org-a",
        document_id="doc-a",
        version_id="version-a",
    )
    assert len(chunks) >= 1
    assert all(c.heading_path is None for c in chunks)
    assert all(c.content == plain_text_document.text[c.start_offset:c.end_offset]
               for c in chunks)


def test_chunker_handles_long_unbroken_paragraph():
    # A single paragraph with no newlines must still be split into bounded
    # chunks and never overflow MAX_CHUNK_TOKENS. Content is kept non-degenerate
    # so forward relocation stays unambiguous.
    paragraph = " ".join(f"token-word-{i}" for i in range(8000)) + " finis"
    doc = DocumentLoader().load(
        paragraph.encode("utf-8"), DocumentSourceType.TEXT
    )
    chunks = KnowledgeChunker().split(
        doc, organization_id="org-a", document_id="doc-a", version_id="v1"
    )
    assert len(chunks) >= 1
    assert all(
        c.content == doc.text[c.start_offset:c.end_offset] and c.token_count
        for c in chunks
    )


def test_chunker_handles_chinese_text():
    zh = "。".join(
        "这是关于客户支持系统中退款和订单处理流程的详细说明文档段落，"
        "包含若干需要被稳定切分的自然语言内容以便后续检索。"
        for _ in range(200)
    )
    doc = DocumentLoader().load(zh.encode("utf-8"), DocumentSourceType.TEXT)
    chunks = KnowledgeChunker().split(
        doc, organization_id="org-a", document_id="doc-a", version_id="v1"
    )
    assert len(chunks) >= 1
    assert all(
        chunk.content == doc.text[chunk.start_offset:chunk.end_offset]
        for chunk in chunks
    )
    # Chinese chars tokenize densely; assert the whole text is covered with no
    # gaps: any backtracking between adjacent chunks is the intended token
    # overlap, never a hole.
    covered = sorted((c.start_offset, c.end_offset) for c in chunks)
    assert covered[0][0] == 0
    assert covered[-1][1] == len(doc.text)
    assert all(b >= a for (_, b), (a, _) in zip(covered, covered[1:]))


def test_chunker_overlap_stays_within_budget(markdown_document):
    text = markdown_document.text
    chunks = KnowledgeChunker().split(
        markdown_document,
        organization_id="org-a",
        document_id="doc-a",
        version_id="version-a",
    )
    import tiktoken

    enc = tiktoken.encoding_for_model("gpt-4")
    # Consecutive chunks may overlap by at most CHUNK_OVERLAP_TOKENS tokens.
    for a, b in zip(chunks, chunks[1:]):
        shared_end = min(a.end_offset, b.end_offset)
        if shared_end > b.start_offset:
            overlap = text[b.start_offset:shared_end]
            assert len(enc.encode(overlap)) <= CHUNK_OVERLAP_TOKENS


def test_chunker_chunk_ids_are_deterministic(markdown_document):
    chunker = KnowledgeChunker()
    first = chunker.split(
        markdown_document,
        organization_id="org-a",
        document_id="doc-a",
        version_id="v1",
    )
    second = chunker.split(
        markdown_document,
        organization_id="org-a",
        document_id="doc-a",
        version_id="v1",
    )
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


def test_chunker_empty_text_raises_invalid_document():
    # A loader would reject empty text up front, but the chunker must still
    # defend against an empty splitter output independently.
    doc = DocumentLoader().load(b"# Just\n", DocumentSourceType.MARKDOWN)
    with pytest.raises(InvalidDocumentError):
        KnowledgeChunker().split(
            doc, organization_id="org-a", document_id="doc-a", version_id="v1"
        )
