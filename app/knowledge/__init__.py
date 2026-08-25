"""Knowledge domain: contracts, models and tenant-scoped store protocol."""

from app.knowledge.base import (
    DocumentChunk,
    DocumentDisabledError,
    DocumentNotFoundError,
    DocumentSourceType,
    DocumentStatus,
    DocumentVersion,
    DuplicateDocumentVersionError,
    IngestionFailedError,
    IngestionJob,
    IngestionStatus,
    InsufficientEvidenceError,
    InvalidDocumentError,
    KnowledgeDocument,
    KnowledgeError,
    KnowledgeStore,
    RetrievalEvent,
)

__all__ = [
    "DocumentChunk",
    "DocumentDisabledError",
    "DocumentNotFoundError",
    "DocumentSourceType",
    "DocumentStatus",
    "DocumentVersion",
    "DuplicateDocumentVersionError",
    "IngestionFailedError",
    "IngestionJob",
    "IngestionStatus",
    "InsufficientEvidenceError",
    "InvalidDocumentError",
    "KnowledgeDocument",
    "KnowledgeError",
    "KnowledgeStore",
    "RetrievalEvent",
]
