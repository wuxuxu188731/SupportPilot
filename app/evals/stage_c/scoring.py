"""Stable citation identity resolution and pure Stage C evidence metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.evals.stage_c.models import EvidenceGroup, StageCCase, TenantKey
from app.knowledge.base import DocumentStatus
from app.knowledge.results import Citation


@dataclass(frozen=True)
class ChunkIdentity:
    """Trusted, fixture-produced identity for one generated chunk."""

    chunk_id: str
    tenant_key: TenantKey | None = None
    document_key: str | None = None
    document_id: str | None = None
    version_id: str | None = None
    heading_path: str | None = None
    document_status: DocumentStatus | None = None
    active_version_id: str | None = None
    known: bool = True

    def __post_init__(self) -> None:
        if not self.chunk_id:
            raise ValueError("chunk_id must not be empty")
        if self.known:
            required = {
                "tenant_key": self.tenant_key,
                "document_key": self.document_key,
                "document_id": self.document_id,
                "version_id": self.version_id,
                "heading_path": self.heading_path,
                "document_status": self.document_status,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError(
                    "known ChunkIdentity requires " + ", ".join(missing)
                )
        elif any(
            value is not None
            for value in (
                self.tenant_key,
                self.document_key,
                self.document_id,
                self.version_id,
                self.heading_path,
                self.document_status,
                self.active_version_id,
            )
        ):
            raise ValueError("unknown ChunkIdentity must not carry trusted identity")

    @classmethod
    def unknown(cls, chunk_id: str) -> "ChunkIdentity":
        """Create an explicit safety failure for an unindexed returned chunk."""
        return cls(chunk_id=chunk_id, known=False)

    @property
    def is_active(self) -> bool:
        return bool(
            self.known
            and self.document_status is DocumentStatus.ACTIVE
            and self.version_id == self.active_version_id
        )


@dataclass(frozen=True)
class IdentityIndex:
    """Immutable trusted chunk lookup with no global tenant inference."""

    identities: tuple[ChunkIdentity, ...]
    _by_chunk_id: dict[str, ChunkIdentity] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        identities = tuple(self.identities)
        by_chunk_id: dict[str, ChunkIdentity] = {}
        for identity in identities:
            if identity.chunk_id in by_chunk_id:
                raise ValueError(f"duplicate chunk_id: {identity.chunk_id}")
            by_chunk_id[identity.chunk_id] = identity
        object.__setattr__(self, "identities", identities)
        object.__setattr__(self, "_by_chunk_id", by_chunk_id)

    def resolve(self, chunk_id: str) -> ChunkIdentity:
        identity = self._by_chunk_id.get(chunk_id)
        return identity if identity is not None else ChunkIdentity.unknown(chunk_id)

    def identities_for_version(self, version_id: str) -> tuple[ChunkIdentity, ...]:
        return tuple(
            identity
            for identity in self.identities
            if identity.known and identity.version_id == version_id
        )


@dataclass(frozen=True)
class StableCitation:
    """Citation metadata joined with trusted identity, without document content."""

    citation_id: str
    citation_rank: int
    tenant_key: TenantKey | None
    document_key: str | None
    heading_path: str | None
    document_id: str
    version_id: str
    chunk_id: str
    identity_known: bool
    identity_consistent: bool
    document_status: DocumentStatus | None
    active_version_id: str | None

    @property
    def is_active(self) -> bool:
        return bool(
            self.identity_known
            and self.identity_consistent
            and self.document_status is DocumentStatus.ACTIVE
            and self.version_id == self.active_version_id
        )


@dataclass(frozen=True)
class VariantMetrics:
    """Metric values for one retrieval variant and one Stage C case."""

    required_group_count: int
    covered_group_count: int
    evidence_group_recall: float | None
    evaluated_citation_count: int
    returned_full_count: int
    relevant_top5_count: int
    retrieval_precision: float | None
    complete_evidence_coverage: bool | None
    quality_scope_reason: str | None
    cross_tenant_leak: bool
    disabled_document_leak: bool
    inactive_version_leak: bool
    unknown_identity_count: int

    @classmethod
    def from_counts(
        cls,
        case: StageCCase,
        citations: Sequence[StableCitation],
        evaluated: Sequence[StableCitation],
        covered_ids: set[str],
        relevant_count: int,
    ) -> "VariantMetrics":
        required_group_count = len(case.required_evidence_groups)
        if required_group_count == 0:
            recall: float | None = None
            precision: float | None = None
            complete: bool | None = None
            quality_scope_reason: str | None = "no_required_evidence_groups"
        else:
            recall = len(covered_ids) / required_group_count
            precision = (
                relevant_count / len(evaluated) if evaluated else 0.0
            )
            complete = len(covered_ids) == required_group_count
            quality_scope_reason = None

        return cls(
            required_group_count=required_group_count,
            covered_group_count=len(covered_ids),
            evidence_group_recall=recall,
            evaluated_citation_count=len(evaluated),
            returned_full_count=len(citations),
            relevant_top5_count=relevant_count,
            retrieval_precision=precision,
            complete_evidence_coverage=complete,
            quality_scope_reason=quality_scope_reason,
            cross_tenant_leak=any(
                item.identity_known
                and item.tenant_key is not None
                and item.tenant_key != case.tenant_key
                for item in citations
            ),
            disabled_document_leak=any(
                item.identity_known
                and item.document_status is DocumentStatus.DISABLED
                for item in citations
            ),
            inactive_version_leak=any(
                item.identity_known
                and item.document_status is DocumentStatus.ACTIVE
                and item.version_id != item.active_version_id
                for item in citations
            ),
            unknown_identity_count=sum(
                not item.identity_known for item in citations
            ),
        )


def normalize_citations(
    citations: Sequence[Citation], identity_index: IdentityIndex
) -> tuple[StableCitation, ...]:
    """Join raw retrieval citations to trusted identities without dropping misses."""
    normalized: list[StableCitation] = []
    for rank, citation in enumerate(citations, start=1):
        identity = identity_index.resolve(citation.chunk_id)
        consistent = bool(
            identity.known
            and citation.document_id == identity.document_id
            and citation.version_id == identity.version_id
            and citation.heading_path == identity.heading_path
        )
        normalized.append(
            StableCitation(
                citation_id=citation.citation_id,
                citation_rank=rank,
                tenant_key=identity.tenant_key if identity.known else None,
                document_key=identity.document_key if identity.known else None,
                heading_path=(
                    identity.heading_path if identity.known else citation.heading_path
                ),
                document_id=(
                    identity.document_id if identity.known else citation.document_id
                ),
                version_id=(
                    identity.version_id if identity.known else citation.version_id
                ),
                chunk_id=citation.chunk_id,
                identity_known=identity.known,
                identity_consistent=consistent,
                document_status=(
                    identity.document_status if identity.known else None
                ),
                active_version_id=(
                    identity.active_version_id if identity.known else None
                ),
            )
        )
    return tuple(normalized)


def heading_matches(returned: str | None, expected: str) -> bool:
    """Accept an exact heading or a path whose final segment is expected."""
    return returned == expected or bool(returned and returned.endswith("/" + expected))


def citation_matches_group(
    citation: StableCitation,
    group: EvidenceGroup,
    *,
    tenant_key: TenantKey,
) -> bool:
    """Return whether trusted, current evidence supports one OR evidence group."""
    if (
        not citation.identity_known
        or not citation.identity_consistent
        or not citation.is_active
        or citation.tenant_key != tenant_key
        or citation.document_key is None
    ):
        return False
    return any(
        citation.document_key == alternative.document_key
        and heading_matches(citation.heading_path, alternative.heading_path)
        for alternative in group.any_of
    )


def score_variant(
    case: StageCCase,
    citations: Sequence[StableCitation],
    *,
    top_k: int = 5,
) -> VariantMetrics:
    """Score one returned citation sequence using only its deterministic Top-K."""
    if top_k < 0:
        raise ValueError("top_k must be non-negative")
    evaluated = tuple(citations[:top_k])
    covered_ids = {
        group.group_id
        for group in case.required_evidence_groups
        if any(
            citation_matches_group(item, group, tenant_key=case.tenant_key)
            for item in evaluated
        )
    }
    relevant_count = sum(
        any(
            citation_matches_group(item, group, tenant_key=case.tenant_key)
            for group in case.required_evidence_groups
        )
        for item in evaluated
    )
    return VariantMetrics.from_counts(
        case, citations, evaluated, covered_ids, relevant_count
    )


def _quality_triple(numerator: int, denominator: int) -> dict[str, int | float | str | None]:
    if denominator == 0:
        return {
            "numerator": numerator,
            "denominator": denominator,
            "value": None,
            "scope_reason": "no_measurable_cases",
        }
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator,
    }


def aggregate_metrics(metrics: Sequence[VariantMetrics]) -> dict[str, object]:
    """Aggregate count-backed quality fractions and safety findings for reports."""
    measurable = [item for item in metrics if item.required_group_count > 0]
    return {
        "case_count": len(metrics),
        "evidence_group_recall": _quality_triple(
            sum(item.covered_group_count for item in measurable),
            sum(item.required_group_count for item in measurable),
        ),
        "retrieval_precision": _quality_triple(
            sum(item.relevant_top5_count for item in measurable),
            sum(item.evaluated_citation_count for item in measurable),
        ),
        "complete_evidence_coverage": _quality_triple(
            sum(item.complete_evidence_coverage is True for item in measurable),
            len(measurable),
        ),
        "cross_tenant_leak_count": sum(item.cross_tenant_leak for item in metrics),
        "disabled_document_leak_count": sum(
            item.disabled_document_leak for item in metrics
        ),
        "inactive_version_leak_count": sum(
            item.inactive_version_leak for item in metrics
        ),
        "unknown_identity_count": sum(
            item.unknown_identity_count for item in metrics
        ),
    }
