from __future__ import annotations

import pytest

from app.evals.stage_c.models import (
    EvidenceAlternative,
    EvidenceGroup,
    ExpectedBehavior,
    KeyAnswerFact,
    StageCCase,
    StageCCategory,
    StageCScenario,
    TenantKey,
)
from app.evals.stage_c.scoring import (
    ChunkIdentity,
    IdentityIndex,
    StableCitation,
    aggregate_metrics,
    heading_matches,
    normalize_citations,
    score_variant,
)
from app.knowledge.base import DocumentStatus
from app.knowledge.results import Citation


RETURN_HEADING = "退货与换货政策/1.1 退货时限（按会员等级）"
VIP_HEADING = "VIP会员权益/3.1 无理由退货期限延长"
REFUND_HEADING = "退款政策/2.1 全额退款"


@pytest.fixture
def stage_case() -> StageCCase:
    return StageCCase(
        case_id="scoring-case",
        category=StageCCategory.MULTI_CONDITION_POLICY,
        scenario=StageCScenario.MAIN_ACTIVE,
        tenant_key=TenantKey.ORG_A,
        question="测试问题",
        reference_answer="测试答案",
        key_answer_facts=(
            KeyAnswerFact(fact_id="return-window", statement="退货期限"),
            KeyAnswerFact(fact_id="refund", statement="全额退款"),
        ),
        required_evidence_groups=(
            EvidenceGroup(
                group_id="return-window",
                supports_fact_ids=("return-window",),
                any_of=(
                    EvidenceAlternative(
                        document_key="returns_exchange", heading_path=RETURN_HEADING
                    ),
                    EvidenceAlternative(document_key="vip", heading_path=VIP_HEADING),
                ),
            ),
            EvidenceGroup(
                group_id="refund",
                supports_fact_ids=("refund",),
                any_of=(
                    EvidenceAlternative(
                        document_key="refunds", heading_path=REFUND_HEADING
                    ),
                ),
            ),
        ),
        should_have_answer=True,
        expected_behavior=ExpectedBehavior.ANSWER_GROUNDED,
        strategy_expectation=None,
        business_context=None,
        forbidden_tenant_keys=(TenantKey.ORG_B,),
        security_expectations=None,
        notes="",
    )


def stable_citation(
    document_key: str,
    heading_path: str,
    *,
    rank: int = 1,
    tenant_key: TenantKey = TenantKey.ORG_A,
    known: bool = True,
    consistent: bool = True,
    status: DocumentStatus | None = DocumentStatus.ACTIVE,
    active_version_id: str | None = "version-1",
) -> StableCitation:
    return StableCitation(
        citation_id=f"C{rank}",
        citation_rank=rank,
        tenant_key=tenant_key if known else None,
        document_key=document_key if known else None,
        heading_path=heading_path,
        document_id=f"document-{document_key}",
        version_id="version-1",
        chunk_id=f"chunk-{rank}",
        identity_known=known,
        identity_consistent=consistent,
        document_status=status if known else None,
        active_version_id=active_version_id if known else None,
    )


def irrelevant(rank: int) -> StableCitation:
    return stable_citation("irrelevant", "不相关/章节", rank=rank)


def test_groups_are_and_and_any_of_is_or(stage_case: StageCCase) -> None:
    """A wrong OR-across-groups branch must not report complete evidence."""
    citations = (
        stable_citation("returns_exchange", RETURN_HEADING),
        stable_citation("vip", VIP_HEADING, rank=2),
    )

    metrics = score_variant(stage_case, citations, top_k=5)

    assert metrics.covered_group_count == 1
    assert metrics.required_group_count == 2
    assert metrics.evidence_group_recall == 0.5
    assert metrics.complete_evidence_coverage is False


def test_single_any_of_source_only_covers_its_one_group(stage_case: StageCCase) -> None:
    """A citation matching one group must not satisfy a distinct required group."""
    metrics = score_variant(
        stage_case, (stable_citation("returns_exchange", RETURN_HEADING),), top_k=5
    )

    assert metrics.covered_group_count == 1
    assert metrics.required_group_count == 2
    assert metrics.evidence_group_recall == 0.5
    assert metrics.complete_evidence_coverage is False


@pytest.mark.parametrize(
    ("returned", "expected", "matches"),
    [
        (RETURN_HEADING, RETURN_HEADING, True),
        ("文档总览/" + RETURN_HEADING, RETURN_HEADING, True),
        ("会员等级）", RETURN_HEADING, False),
        ("前缀/" + RETURN_HEADING + "/附录", RETURN_HEADING, False),
    ],
)
def test_heading_matching_requires_exact_full_or_final_segment(
    returned: str, expected: str, matches: bool
) -> None:
    """Prefix or substring matching would falsely credit unrelated evidence."""
    assert heading_matches(returned, expected) is matches


def test_duplicate_equivalent_sources_cover_one_group_once(stage_case: StageCCase) -> None:
    """Duplicate relevant citations must not inflate a group numerator."""
    citations = (
        stable_citation("returns_exchange", RETURN_HEADING),
        stable_citation("returns_exchange", RETURN_HEADING, rank=2),
    )

    metrics = score_variant(stage_case, citations, top_k=5)

    assert metrics.covered_group_count == 1
    assert metrics.relevant_top5_count == 2
    assert metrics.retrieval_precision == 1.0


def test_sixth_adaptive_citation_does_not_change_primary_metrics(
    stage_case: StageCCase,
) -> None:
    """Only the returned Top-5, not extra adaptive results, drive quality."""
    citations = tuple(irrelevant(index) for index in range(1, 6)) + (
        stable_citation("returns_exchange", RETURN_HEADING, rank=6),
    )

    metrics = score_variant(stage_case, citations, top_k=5)

    assert metrics.returned_full_count == 6
    assert metrics.evaluated_citation_count == 5
    assert metrics.relevant_top5_count == 0
    assert metrics.covered_group_count == 0
    assert metrics.retrieval_precision == 0.0


def test_sixth_and_later_forbidden_citations_set_safety_not_quality(
    stage_case: StageCCase,
) -> None:
    """Safety scans the full response even though quality remains strict Top-5."""
    citations = tuple(irrelevant(index) for index in range(1, 6)) + (
        stable_citation(
            "returns_exchange",
            RETURN_HEADING,
            rank=6,
            tenant_key=TenantKey.ORG_B,
        ),
        stable_citation(
            "returns_exchange",
            RETURN_HEADING,
            rank=7,
            status=DocumentStatus.DISABLED,
        ),
        stable_citation(
            "returns_exchange",
            RETURN_HEADING,
            rank=8,
            active_version_id="version-2",
        ),
        stable_citation("unknown", "unknown", rank=9, known=False),
    )

    metrics = score_variant(stage_case, citations, top_k=5)

    assert metrics.evaluated_citation_count == 5
    assert metrics.relevant_top5_count == 0
    assert metrics.covered_group_count == 0
    assert metrics.cross_tenant_leak is True
    assert metrics.disabled_document_leak is True
    assert metrics.inactive_version_leak is True
    assert metrics.unknown_identity_count == 1


def test_no_golden_evidence_has_explicit_null_quality_scope(
    stage_case: StageCCase,
) -> None:
    """No-golden cases are outside quality denominators rather than perfect."""
    no_golden_case = stage_case.model_copy(
        update={
            "required_evidence_groups": (),
            "should_have_answer": False,
            "expected_behavior": ExpectedBehavior.ABSTAIN,
        }
    )

    metrics = score_variant(no_golden_case, (irrelevant(1),), top_k=5)

    assert metrics.required_group_count == 0
    assert metrics.evidence_group_recall is None
    assert metrics.retrieval_precision is None
    assert metrics.complete_evidence_coverage is None
    assert metrics.quality_scope_reason == "no_required_evidence_groups"


def test_precision_uses_exact_integer_counts(stage_case: StageCCase) -> None:
    """Precision must divide relevant Top-5 citations by all evaluated citations."""
    citations = (
        stable_citation("returns_exchange", RETURN_HEADING),
        irrelevant(2),
        irrelevant(3),
    )

    metrics = score_variant(stage_case, citations, top_k=5)

    assert metrics.relevant_top5_count == 1
    assert metrics.evaluated_citation_count == 3
    assert metrics.retrieval_precision == pytest.approx(1 / 3)


def test_normalization_keeps_unknown_and_inconsistent_citations() -> None:
    """Unknown or forged returned identities stay visible to safety scoring."""
    identity = ChunkIdentity(
        chunk_id="known-chunk",
        tenant_key=TenantKey.ORG_A,
        document_key="returns_exchange",
        document_id="document-returns",
        version_id="version-1",
        heading_path=RETURN_HEADING,
        document_status=DocumentStatus.ACTIVE,
        active_version_id="version-1",
    )
    normalized = normalize_citations(
        (
            Citation(
                citation_id="C1",
                document_id="document-returns",
                version_id="version-1",
                chunk_id="known-chunk",
                title="标题",
                heading_path=RETURN_HEADING,
                content="内容",
            ),
            Citation(
                citation_id="C2",
                document_id="forged-document",
                version_id="version-1",
                chunk_id="known-chunk",
                title="标题",
                heading_path=RETURN_HEADING,
                content="内容",
            ),
            Citation(
                citation_id="C3",
                document_id="unknown-document",
                version_id="unknown-version",
                chunk_id="unknown-chunk",
                title="标题",
                heading_path=None,
                content="内容",
            ),
        ),
        IdentityIndex((identity,)),
    )

    assert [item.citation_rank for item in normalized] == [1, 2, 3]
    assert normalized[0].identity_consistent is True
    assert normalized[1].identity_consistent is False
    assert normalized[2].identity_known is False
    assert normalized[2].chunk_id == "unknown-chunk"
    assert normalized[2].document_key is None


def test_identity_index_rejects_duplicates_and_resolves_explicit_unknown() -> None:
    """Ambiguous index entries and absent chunks must never be silently accepted."""
    identity = ChunkIdentity(
        chunk_id="chunk-1",
        tenant_key=TenantKey.ORG_A,
        document_key="returns_exchange",
        document_id="document-returns",
        version_id="version-1",
        heading_path=RETURN_HEADING,
        document_status=DocumentStatus.ACTIVE,
        active_version_id="version-1",
    )

    with pytest.raises(ValueError, match="duplicate chunk_id"):
        IdentityIndex((identity, identity))

    unknown = IdentityIndex((identity,)).resolve("not-indexed")
    assert unknown.known is False
    assert unknown.chunk_id == "not-indexed"
    assert unknown.is_active is False


def test_identity_index_returns_only_the_requested_version() -> None:
    """Fixture setup must not accidentally attach chunks from another version."""
    current = ChunkIdentity(
        chunk_id="current",
        tenant_key=TenantKey.ORG_A,
        document_key="returns_exchange",
        document_id="document-returns",
        version_id="version-current",
        heading_path=RETURN_HEADING,
        document_status=DocumentStatus.ACTIVE,
        active_version_id="version-current",
    )
    old = ChunkIdentity(
        chunk_id="old",
        tenant_key=TenantKey.ORG_A,
        document_key="returns_exchange",
        document_id="document-returns",
        version_id="version-old",
        heading_path=RETURN_HEADING,
        document_status=DocumentStatus.ACTIVE,
        active_version_id="version-current",
    )

    selected = IdentityIndex((current, old)).identities_for_version("version-current")

    assert selected == (current,)


def test_security_metrics_include_unknown_and_untrusted_citations(
    stage_case: StageCCase,
) -> None:
    """Safety counters must expose cross-tenant, disabled, inactive, and unknown IDs."""
    citations = (
        stable_citation(
            "returns_exchange", RETURN_HEADING, tenant_key=TenantKey.ORG_B
        ),
        stable_citation(
            "returns_exchange",
            RETURN_HEADING,
            rank=2,
            status=DocumentStatus.DISABLED,
            active_version_id="version-1",
        ),
        stable_citation(
            "returns_exchange", RETURN_HEADING, rank=3, active_version_id="version-2"
        ),
        stable_citation("ignored", "ignored", rank=4, known=False),
    )

    metrics = score_variant(stage_case, citations, top_k=5)

    assert metrics.cross_tenant_leak is True
    assert metrics.disabled_document_leak is True
    assert metrics.inactive_version_leak is True
    assert metrics.unknown_identity_count == 1


def test_forged_known_citation_metadata_cannot_hide_trusted_safety_leaks(
    stage_case: StageCCase,
) -> None:
    """Known chunks retain trusted leak findings even when returned fields disagree."""
    identities = IdentityIndex(
        (
            ChunkIdentity(
                chunk_id="cross-tenant",
                tenant_key=TenantKey.ORG_B,
                document_key="returns_exchange",
                document_id="trusted-cross-tenant-document",
                version_id="cross-version",
                heading_path=RETURN_HEADING,
                document_status=DocumentStatus.ACTIVE,
                active_version_id="cross-version",
            ),
            ChunkIdentity(
                chunk_id="disabled",
                tenant_key=TenantKey.ORG_A,
                document_key="returns_exchange",
                document_id="trusted-disabled-document",
                version_id="disabled-version",
                heading_path=RETURN_HEADING,
                document_status=DocumentStatus.DISABLED,
                active_version_id="disabled-version",
            ),
            ChunkIdentity(
                chunk_id="inactive",
                tenant_key=TenantKey.ORG_A,
                document_key="returns_exchange",
                document_id="trusted-inactive-document",
                version_id="old-version",
                heading_path=RETURN_HEADING,
                document_status=DocumentStatus.ACTIVE,
                active_version_id="current-version",
            ),
        )
    )
    citations = normalize_citations(
        (
            Citation(
                citation_id="C1",
                document_id="forged-cross-tenant-document",
                version_id="cross-version",
                chunk_id="cross-tenant",
                title="标题",
                heading_path=RETURN_HEADING,
                content="内容",
            ),
            Citation(
                citation_id="C2",
                document_id="forged-disabled-document",
                version_id="disabled-version",
                chunk_id="disabled",
                title="标题",
                heading_path=RETURN_HEADING,
                content="内容",
            ),
            Citation(
                citation_id="C3",
                document_id="forged-inactive-document",
                version_id="current-version",
                chunk_id="inactive",
                title="标题",
                heading_path=RETURN_HEADING,
                content="内容",
            ),
        ),
        identities,
    )

    metrics = score_variant(stage_case, citations, top_k=5)

    assert all(item.identity_consistent is False for item in citations)
    assert metrics.relevant_top5_count == 0
    assert metrics.cross_tenant_leak is True
    assert metrics.disabled_document_leak is True
    assert metrics.inactive_version_leak is True


def test_aggregate_metrics_exposes_exact_quality_fractions(stage_case: StageCCase) -> None:
    """Aggregate denominators must be counts, never an average of averages."""
    partial = score_variant(
        stage_case,
        (stable_citation("returns_exchange", RETURN_HEADING), irrelevant(2)),
        top_k=5,
    )
    complete = score_variant(
        stage_case,
        (
            stable_citation("returns_exchange", RETURN_HEADING),
            stable_citation("refunds", REFUND_HEADING, rank=2),
        ),
        top_k=5,
    )

    aggregate = aggregate_metrics((partial, complete))

    assert aggregate["evidence_group_recall"] == {
        "numerator": 3,
        "denominator": 4,
        "value": 0.75,
    }
    assert aggregate["retrieval_precision"] == {
        "numerator": 3,
        "denominator": 4,
        "value": 0.75,
    }
    assert aggregate["complete_evidence_coverage"] == {
        "numerator": 1,
        "denominator": 2,
        "value": 0.5,
    }


def test_aggregate_empty_quality_reports_null_scope() -> None:
    """An aggregate with no measurable cases must not invent zero quality."""
    aggregate = aggregate_metrics(())

    assert aggregate["evidence_group_recall"] == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
        "scope_reason": "no_measurable_cases",
    }
