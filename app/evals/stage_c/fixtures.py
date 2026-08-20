"""Tenant-scoped, reproducible corpus assembly for Stage C evaluation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from app.evals.stage_c.models import StageCCase, StageCScenario, TenantKey
from app.evals.stage_c.scoring import ChunkIdentity, IdentityIndex
from app.knowledge.base import DocumentSourceType, DocumentStatus, KnowledgeStore
from app.knowledge.chunking import KnowledgeChunker
from app.knowledge.document_loader import DocumentLoader
from app.knowledge.ingestion import KnowledgeIngestionService


_OLD_RETURN_ROW = b"| \xe6\x99\xae\xe9\x80\x9a\xe4\xbc\x9a\xe5\x91\x98 | 7 \xe5\xa4\xa9 |"
_OLD_RETURN_REPLACEMENT = b"| \xe6\x99\xae\xe9\x80\x9a\xe4\xbc\x9a\xe5\x91\x98 | 10 \xe5\xa4\xa9 |"
_UPLOADED_BY_USER_ID = "stage-c-fixture"


@dataclass(frozen=True)
class CorpusDocumentSpec:
    """One checked-in source document and its stable fixture identity."""

    tenant_key: TenantKey
    document_key: str
    title: str
    path: Path


@dataclass(frozen=True)
class FixtureDocument:
    """Stable database IDs for one prepared tenant document."""

    tenant_key: TenantKey
    document_key: str
    title: str
    document_id: str
    active_version_id: str
    old_version_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "tenant_key": self.tenant_key.value,
            "document_key": self.document_key,
            "title": self.title,
            "document_id": self.document_id,
            "active_version_id": self.active_version_id,
            "old_version_id": self.old_version_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "FixtureDocument":
        return cls(
            tenant_key=TenantKey(str(raw["tenant_key"])),
            document_key=str(raw["document_key"]),
            title=str(raw["title"]),
            document_id=str(raw["document_id"]),
            active_version_id=str(raw["active_version_id"]),
            old_version_id=(
                None
                if raw.get("old_version_id") is None
                else str(raw["old_version_id"])
            ),
        )


@dataclass(frozen=True)
class FixtureManifest:
    """Immutable, JSON-round-trippable IDs for a prepared Stage C corpus."""

    documents: tuple[FixtureDocument, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        documents = tuple(self.documents)
        keys = [(item.tenant_key, item.document_key) for item in documents]
        if len(keys) != len(set(keys)):
            raise ValueError("fixture manifest contains duplicate tenant/document keys")
        object.__setattr__(self, "documents", documents)

    def document(
        self, tenant_key: TenantKey | str, document_key: str
    ) -> FixtureDocument:
        tenant = TenantKey(tenant_key)
        for document in self.documents:
            if document.tenant_key is tenant and document.document_key == document_key:
                return document
        raise KeyError(f"fixture document not found: {tenant.value}/{document_key}")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "documents": [document.to_dict() for document in self.documents],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "FixtureManifest":
        documents = raw.get("documents")
        if not isinstance(documents, list):
            raise ValueError("fixture manifest documents must be a list")
        schema_version = raw.get("schema_version")
        if schema_version != 1:
            raise ValueError(f"unsupported fixture manifest schema: {schema_version!r}")
        return cls(
            documents=tuple(
                FixtureDocument.from_dict(item)
                for item in documents
                if isinstance(item, Mapping)
            ),
            schema_version=1,
        )


class GoldenEvidenceMappingError(ValueError):
    """Raised when a golden evidence reference has no real corpus section."""


def corpus_specs(repo_root: str | Path) -> tuple[CorpusDocumentSpec, ...]:
    """Return the exact eight org-A and three conflicting org-B documents."""

    root = Path(repo_root)
    org_a = (
        ("general_service", "01-\u552e\u540e\u670d\u52a1\u603b\u5219.md", "\u552e\u540e\u670d\u52a1\u603b\u5219"),
        ("returns_exchange", "02-\u9000\u8d27\u4e0e\u6362\u8d27\u653f\u7b56.md", "\u9000\u8d27\u4e0e\u6362\u8d27\u653f\u7b56"),
        ("refunds", "03-\u9000\u6b3e\u653f\u7b56.md", "\u9000\u6b3e\u653f\u7b56"),
        ("logistics", "04-\u7269\u6d41\u914d\u9001\u4e0e\u5f02\u5e38\u5904\u7406.md", "\u7269\u6d41\u914d\u9001\u4e0e\u5f02\u5e38\u5904\u7406"),
        ("compensation", "05-\u5ef6\u8fdf\u8d54\u507f\u4e0e\u552e\u540e\u8865\u507f.md", "\u5ef6\u8fdf\u8d54\u507f\u4e0e\u552e\u540e\u8865\u507f"),
        ("warranty", "06-\u5546\u54c1\u8d28\u91cf\u4e0e\u4fdd\u4fee\u653f\u7b56.md", "\u5546\u54c1\u8d28\u91cf\u4e0e\u4fdd\u4fee\u653f\u7b56"),
        ("order_changes", "07-\u8ba2\u5355\u53d6\u6d88\u4e0e\u4fee\u6539\u653f\u7b56.md", "\u8ba2\u5355\u53d6\u6d88\u4e0e\u4fee\u6539\u653f\u7b56"),
        ("vip", "08-VIP\u4f1a\u5458\u6743\u76ca.md", "VIP\u4f1a\u5458\u6743\u76ca"),
    )
    org_b = (
        ("returns_exchange", "returns.md", "\u9000\u8d27\u4e0e\u6362\u8d27\u653f\u7b56\uff08\u661f\u6cb3\u5546\u57ce\uff09"),
        ("compensation", "compensation.md", "\u5ef6\u8fdf\u8d54\u507f\u4e0e\u552e\u540e\u8865\u507f\uff08\u661f\u6cb3\u5546\u57ce\uff09"),
        ("warranty", "warranty.md", "\u5546\u54c1\u8d28\u91cf\u4e0e\u4fdd\u4fee\u653f\u7b56\uff08\u661f\u6cb3\u5546\u57ce\uff09"),
    )
    return tuple(
        CorpusDocumentSpec(
            tenant_key=TenantKey.ORG_A,
            document_key=document_key,
            title=f"Stage C org_a | {title}",
            path=root / "docs" / "knowledge" / filename,
        )
        for document_key, filename, title in org_a
    ) + tuple(
        CorpusDocumentSpec(
            tenant_key=TenantKey.ORG_B,
            document_key=document_key,
            title=f"Stage C org_b | {title}",
            path=(
                root
                / "evals"
                / "knowledge"
                / "stage_c"
                / "documents"
                / "org_b"
                / filename
            ),
        )
        for document_key, filename, title in org_b
    )


def _old_returns_bytes(current: bytes) -> bytes:
    count = current.count(_OLD_RETURN_ROW)
    if count != 1:
        raise ValueError(
            "org_a returns fixture must contain exactly one ordinary-member "
            f"7-day row; found {count}"
        )
    return current.replace(_OLD_RETURN_ROW, _OLD_RETURN_REPLACEMENT, 1)


def validate_golden_headings(
    cases: Sequence[StageCCase], specs: Sequence[CorpusDocumentSpec]
) -> None:
    """Preflight every golden tenant/document/heading against real bytes."""

    loader = DocumentLoader()
    headings: dict[tuple[TenantKey, str], set[str]] = {}
    for spec in specs:
        key = (spec.tenant_key, spec.document_key)
        if key in headings:
            raise GoldenEvidenceMappingError(
                f"duplicate corpus mapping for {spec.tenant_key.value}/{spec.document_key}"
            )
        loaded = loader.load(spec.path.read_bytes(), DocumentSourceType.MARKDOWN)
        headings[key] = {
            section.heading_path
            for section in loaded.sections
            if section.heading_path is not None
        }

    for case in cases:
        for group in case.required_evidence_groups:
            for alternative in group.any_of:
                key = (case.tenant_key, alternative.document_key)
                if alternative.heading_path not in headings.get(key, set()):
                    raise GoldenEvidenceMappingError(
                        f"case_id={case.case_id}: unmapped golden evidence "
                        f"{case.tenant_key.value}/{alternative.document_key}/"
                        f"{alternative.heading_path}"
                    )


def build_identity_index(
    manifest: FixtureManifest,
    specs: Sequence[CorpusDocumentSpec],
    *,
    store: KnowledgeStore,
    loader: DocumentLoader,
    chunker: KnowledgeChunker,
) -> IdentityIndex:
    """Rebuild trusted chunk IDs without any unscoped document or tenant read."""

    spec_by_key = {(spec.tenant_key, spec.document_key): spec for spec in specs}
    identities: list[ChunkIdentity] = []
    for fixture_document in manifest.documents:
        key = (fixture_document.tenant_key, fixture_document.document_key)
        try:
            spec = spec_by_key[key]
        except KeyError as exc:
            raise ValueError(
                "manifest document has no corpus spec: "
                f"{fixture_document.tenant_key.value}/{fixture_document.document_key}"
            ) from exc
        stored_document = store.get_document(
            organization_id=fixture_document.tenant_key.value,
            document_id=fixture_document.document_id,
        )
        versions = (
            (fixture_document.active_version_id, spec.path.read_bytes()),
        )
        if fixture_document.old_version_id is not None:
            versions += (
                (
                    fixture_document.old_version_id,
                    _old_returns_bytes(spec.path.read_bytes()),
                ),
            )
        for version_id, content in versions:
            loaded = loader.load(content, DocumentSourceType.MARKDOWN)
            chunks = chunker.split(
                loaded,
                organization_id=fixture_document.tenant_key.value,
                document_id=fixture_document.document_id,
                version_id=version_id,
            )
            identities.extend(
                ChunkIdentity(
                    chunk_id=chunk.chunk_id,
                    tenant_key=fixture_document.tenant_key,
                    document_key=fixture_document.document_key,
                    document_id=fixture_document.document_id,
                    version_id=version_id,
                    heading_path=chunk.heading_path,
                    document_status=stored_document.status,
                    active_version_id=stored_document.active_version_id,
                )
                for chunk in chunks
            )
    return IdentityIndex(tuple(identities))


class StageCFixtureManager:
    """Prepare and temporarily mutate the real Stage C ingestion fixture."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        ingestion: KnowledgeIngestionService,
        loader: DocumentLoader,
        chunker: KnowledgeChunker,
    ) -> None:
        self._store = store
        self._ingestion = ingestion
        self._loader = loader
        self._chunker = chunker
        self._manifest: FixtureManifest | None = None
        self._specs: tuple[CorpusDocumentSpec, ...] = ()

    @property
    def manifest(self) -> FixtureManifest:
        if self._manifest is None:
            raise RuntimeError("Stage C fixture has not been prepared")
        return self._manifest

    @property
    def identity_index(self) -> IdentityIndex:
        return build_identity_index(
            self.manifest,
            self._specs,
            store=self._store,
            loader=self._loader,
            chunker=self._chunker,
        )

    def prepare(
        self,
        *,
        cases: Sequence[StageCCase],
        specs: Sequence[CorpusDocumentSpec],
    ) -> FixtureManifest:
        """Idempotently ingest real bytes and leave every current version active."""

        specs = tuple(specs)
        validate_golden_headings(cases, specs)
        prepared: list[FixtureDocument] = []
        existing_by_tenant = {
            tenant: {
                document.title: document
                for document in self._store.list_documents(
                    organization_id=tenant.value
                )
            }
            for tenant in TenantKey
        }
        for spec in specs:
            existing = existing_by_tenant[spec.tenant_key].get(spec.title)
            current = spec.path.read_bytes()
            old_receipt = None
            if existing is None:
                initial = (
                    _old_returns_bytes(current)
                    if self._has_old_version(spec)
                    else current
                )
                initial_receipt = self._ingestion.ingest_new_document(
                    organization_id=spec.tenant_key.value,
                    uploaded_by_user_id=_UPLOADED_BY_USER_ID,
                    title=spec.title,
                    source_type=DocumentSourceType.MARKDOWN,
                    content=initial,
                )
                document_id = initial_receipt.document_id
                if self._has_old_version(spec):
                    old_receipt = initial_receipt
                    current_receipt = self._ingestion.ingest_new_version(
                        organization_id=spec.tenant_key.value,
                        uploaded_by_user_id=_UPLOADED_BY_USER_ID,
                        document_id=document_id,
                        source_type=DocumentSourceType.MARKDOWN,
                        content=current,
                    )
                else:
                    current_receipt = initial_receipt
            else:
                document_id = existing.document_id
                if self._has_old_version(spec):
                    old_receipt = self._ingestion.ingest_new_version(
                        organization_id=spec.tenant_key.value,
                        uploaded_by_user_id=_UPLOADED_BY_USER_ID,
                        document_id=document_id,
                        source_type=DocumentSourceType.MARKDOWN,
                        content=_old_returns_bytes(current),
                    )
                current_receipt = self._ingestion.ingest_new_version(
                    organization_id=spec.tenant_key.value,
                    uploaded_by_user_id=_UPLOADED_BY_USER_ID,
                    document_id=document_id,
                    source_type=DocumentSourceType.MARKDOWN,
                    content=current,
                )

            self._store.activate_version(
                organization_id=spec.tenant_key.value,
                document_id=document_id,
                version_id=current_receipt.version_id,
                job_id=current_receipt.job_id,
            )
            prepared.append(
                FixtureDocument(
                    tenant_key=spec.tenant_key,
                    document_key=spec.document_key,
                    title=spec.title,
                    document_id=document_id,
                    active_version_id=current_receipt.version_id,
                    old_version_id=(
                        old_receipt.version_id if old_receipt is not None else None
                    ),
                )
            )

        self._specs = specs
        self._manifest = FixtureManifest(tuple(prepared))
        # Force one complete reconstruction during preparation so bad receipt IDs
        # or drift in the current loader/chunker fail before a run starts.
        self.identity_index
        return self._manifest

    def document(
        self, tenant_key: TenantKey | str, document_key: str
    ) -> FixtureDocument:
        return self.manifest.document(tenant_key, document_key)

    def status(self, tenant_key: TenantKey | str, document_key: str) -> str:
        fixture_document = self.document(tenant_key, document_key)
        return self._store.get_document(
            organization_id=fixture_document.tenant_key.value,
            document_id=fixture_document.document_id,
        ).status.value

    @contextmanager
    def scenario(self, case: StageCCase) -> Iterator[None]:
        """Apply only the disabled-document scenario and always restore it."""

        if case.scenario is not StageCScenario.DOCUMENT_DISABLED:
            yield
            return

        targets = (
            self.document(TenantKey.ORG_A, "returns_exchange"),
            self.document(TenantKey.ORG_A, "vip"),
        )
        prior = tuple(
            self._store.get_document(
                organization_id=document.tenant_key.value,
                document_id=document.document_id,
            ).status
            for document in targets
        )
        try:
            for document in targets:
                self._store.set_document_status(
                    organization_id=document.tenant_key.value,
                    document_id=document.document_id,
                    status=DocumentStatus.DISABLED,
                )
            yield
        finally:
            for document, status in zip(targets, prior):
                self._store.set_document_status(
                    organization_id=document.tenant_key.value,
                    document_id=document.document_id,
                    status=status,
                )

    @staticmethod
    def _has_old_version(spec: CorpusDocumentSpec) -> bool:
        return (
            spec.tenant_key is TenantKey.ORG_A
            and spec.document_key == "returns_exchange"
        )
