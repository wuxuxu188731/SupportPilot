"""Loader boundary tests: byte-size guard, utf-8 decode, newline
normalisation and markdown heading-path extraction."""

import pytest

from app.knowledge.base import DocumentSourceType, InvalidDocumentError

from app.knowledge.document_loader import (
    MAX_DOCUMENT_BYTES,
    DocumentLoader,
)


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


def test_markdown_loader_heading_stack_resets_on_higher_level():
    # H3 nested under H2 keeps the full ancestor path; a later H1 resets the
    # stack back to the root. Sections are emitted only for text-bearing
    # bodies, so heading-only levels collapse onto the deepest body.
    loaded = DocumentLoader().load(
        b"# A\n## B\n### C\nbody\n# A2\nbody2",
        DocumentSourceType.MARKDOWN,
    )
    assert [s.heading_path for s in loaded.sections] == [
        "A/B/C",
        "A2",
    ]


def test_text_loader_builds_single_unnamed_section():
    loaded = DocumentLoader().load(b"just\nsome\ntext", DocumentSourceType.TEXT)
    assert loaded.text == "just\nsome\ntext"
    assert [s.heading_path for s in loaded.sections] == [None]


def test_text_loader_normalizes_bare_carriage_returns():
    loaded = DocumentLoader().load(b"one\rtwo\r\nthree", DocumentSourceType.TEXT)
    assert loaded.text == "one\ntwo\nthree"


@pytest.mark.parametrize("content", [b"", b" \r\n\t"])
def test_loader_rejects_empty_document(content):
    with pytest.raises(InvalidDocumentError):
        DocumentLoader().load(content, DocumentSourceType.TEXT)


def test_loader_rejects_invalid_utf8():
    with pytest.raises(InvalidDocumentError):
        DocumentLoader().load(b"\xff\xfe", DocumentSourceType.TEXT)


def test_loader_rejects_oversized_document():
    content = b"x" * (MAX_DOCUMENT_BYTES + 1)
    with pytest.raises(InvalidDocumentError):
        DocumentLoader().load(content, DocumentSourceType.TEXT)


def test_loader_accepts_exactly_max_size_document():
    content = b"x" * MAX_DOCUMENT_BYTES
    loaded = DocumentLoader().load(content, DocumentSourceType.TEXT)
    assert loaded.text == "x" * MAX_DOCUMENT_BYTES
