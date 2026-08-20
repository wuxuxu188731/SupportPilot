"""Behavior tests for the tenant-scoped Stage C corpus fixture."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.evals.stage_c.fixtures import (
    FixtureManifest,
    GoldenEvidenceMappingError,
    StageCFixtureManager,
    corpus_specs,
    validate_golden_headings,
)
from app.evals.stage_c.models import StageCScenario, load_stage_c_cases
from app.knowledge.base import DocumentStatus
from app.knowledge.chunking import KnowledgeChunker
from app.knowledge.document_loader import DocumentLoader
from app.knowledge.ingestion import KnowledgeIngestionService
from app.knowledge.sqlite_store import SQLiteKnowledgeStore


REPO_ROOT = Path(__file__).resolve().parents[3]
CASES_PATH = REPO_ROOT / "evals" / "knowledge" / "stage_c" / "cases.jsonl"


class DeterministicEmbedding:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]


class RecordingVectorStore:
    def __init__(self) -> None:
        self.points = []

    def ensure_collection(self) -> None:
        return None

    def upsert(self, *, points) -> None:
        self.points.extend(points)


@pytest.fixture(scope="module")
def valid_cases():
    return load_stage_c_cases(CASES_PATH)


@pytest.fixture(scope="module")
def specs():
    return corpus_specs(REPO_ROOT)


@pytest.fixture(scope="module")
def prepared_fixture(tmp_path_factory, valid_cases, specs):
    database_path = tmp_path_factory.mktemp("stage-c-fixture") / "stage-c.db"
    store = SQLiteKnowledgeStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO users(id, username, password_hash) VALUES (?, ?, ?)",
            ("stage-c-fixture", "stage-c-fixture", "not-used"),
        )
        connection.executemany(
            "INSERT INTO organizations(id, name) VALUES (?, ?)",
            (("org_a", "Stage C Org A"), ("org_b", "Stage C Org B")),
        )
        connection.executemany(
            """INSERT INTO memberships(organization_id, user_id, role)
               VALUES (?, 'stage-c-fixture', 'admin')""",
            (("org_a",), ("org_b",)),
        )
    loader = DocumentLoader()
    chunker = KnowledgeChunker()
    ingestion = KnowledgeIngestionService(
        store=store,
        loader=loader,
        chunker=chunker,
        embedding=DeterministicEmbedding(),
        vector_store=RecordingVectorStore(),
        embedding_model="stage-c-test",
        embedding_dimensions=2,
    )
    manager = StageCFixtureManager(
        store=store,
        ingestion=ingestion,
        loader=loader,
        chunker=chunker,
    )
    manager.prepare(cases=valid_cases, specs=specs)
    return manager


def test_corpus_uses_all_real_org_a_and_conflicting_org_b_documents(specs):
    assert {s.document_key for s in specs if s.tenant_key.value == "org_a"} == {
        "general_service",
        "returns_exchange",
        "refunds",
        "logistics",
        "compensation",
        "warranty",
        "order_changes",
        "vip",
    }
    assert {s.document_key for s in specs if s.tenant_key.value == "org_b"} == {
        "returns_exchange",
        "compensation",
        "warranty",
    }
    assert all(spec.path.is_file() for spec in specs)


def test_preflight_accepts_every_real_golden_heading(valid_cases, specs):
    validate_golden_headings(valid_cases, specs)


def test_preflight_rejects_unmapped_heading(valid_cases, specs):
    first = valid_cases[0]
    group = first.required_evidence_groups[0]
    broken_alternative = group.any_of[0].model_copy(
        update={"heading_path": "not/a/real/heading"}
    )
    broken_group = group.model_copy(update={"any_of": (broken_alternative,)})
    broken_case = first.model_copy(
        update={"required_evidence_groups": (broken_group,)}
    )

    with pytest.raises(GoldenEvidenceMappingError, match=first.case_id):
        validate_golden_headings((broken_case, *valid_cases[1:]), specs)


def test_prepare_rejects_loader_only_heading_before_ingestion(valid_cases, specs):
    """A loader section absent from real chunks must fail before external work."""

    class EmptyChunker:
        def __init__(self) -> None:
            self.calls = 0

        def split(self, document, **identity):
            self.calls += 1
            return []

    class EmptyStore:
        def list_documents(self, *, organization_id):
            return []

    class FailIfCalledIngestion:
        def __init__(self) -> None:
            self.calls = 0

        def ingest_new_document(self, **kwargs):
            self.calls += 1
            raise AssertionError("ingestion ran before heading preflight")

    first_case = valid_cases[0]
    first_spec = next(
        spec
        for spec in specs
        if spec.tenant_key == first_case.tenant_key
        and spec.document_key
        == first_case.required_evidence_groups[0].any_of[0].document_key
    )
    chunker = EmptyChunker()
    ingestion = FailIfCalledIngestion()
    manager = StageCFixtureManager(
        store=EmptyStore(),
        ingestion=ingestion,
        loader=DocumentLoader(),
        chunker=chunker,
    )

    with pytest.raises(GoldenEvidenceMappingError, match=first_case.case_id):
        manager.prepare(cases=(first_case,), specs=(first_spec,))

    assert chunker.calls == 1
    assert ingestion.calls == 0


def test_manifest_is_immutable_and_json_round_trips(prepared_fixture):
    manifest = prepared_fixture.manifest
    encoded = json.dumps(manifest.to_dict(), ensure_ascii=False)

    restored = FixtureManifest.from_dict(json.loads(encoded))

    assert restored == manifest
    with pytest.raises((AttributeError, TypeError)):
        manifest.documents = ()


def test_returns_old_version_is_indexed_but_inactive(prepared_fixture):
    returns = prepared_fixture.document("org_a", "returns_exchange")

    assert returns.old_version_id is not None
    assert returns.old_version_id != returns.active_version_id
    old_identities = prepared_fixture.identity_index.identities_for_version(
        returns.old_version_id
    )
    active_identities = prepared_fixture.identity_index.identities_for_version(
        returns.active_version_id
    )
    assert old_identities
    assert all(identity.is_active is False for identity in old_identities)
    assert active_identities
    assert all(identity.is_active is True for identity in active_identities)


def test_identity_index_keeps_same_document_key_tenant_scoped(prepared_fixture):
    org_a = prepared_fixture.document("org_a", "returns_exchange")
    org_b = prepared_fixture.document("org_b", "returns_exchange")

    org_a_identities = prepared_fixture.identity_index.identities_for_version(
        org_a.active_version_id
    )
    org_b_identities = prepared_fixture.identity_index.identities_for_version(
        org_b.active_version_id
    )

    assert {identity.tenant_key.value for identity in org_a_identities} == {"org_a"}
    assert {identity.tenant_key.value for identity in org_b_identities} == {"org_b"}
    assert {identity.document_id for identity in org_a_identities}.isdisjoint(
        identity.document_id for identity in org_b_identities
    )


def test_prepare_is_resumable_without_duplicate_documents(prepared_fixture, valid_cases, specs):
    original = prepared_fixture.manifest

    resumed = prepared_fixture.prepare(cases=valid_cases, specs=specs)

    assert resumed == original


def test_restore_reuses_a_matching_manifest_without_ingestion(
    prepared_fixture, specs
):
    original = prepared_fixture.manifest

    restored = prepared_fixture.restore(original, specs=specs)

    assert restored == original
    assert prepared_fixture.identity_index.identities


def test_restore_rejects_active_version_drift(prepared_fixture, specs):
    original = prepared_fixture.manifest
    first = original.documents[0]
    broken = FixtureManifest(
        (replace(first, active_version_id="missing-version"), *original.documents[1:])
    )

    with pytest.raises(ValueError, match="active version drift"):
        prepared_fixture.restore(broken, specs=specs)


def test_restore_reactivates_document_left_disabled_by_interruption(
    prepared_fixture, specs
):
    document = prepared_fixture.document("org_a", "returns_exchange")
    prepared_fixture._store.set_document_status(
        organization_id=document.tenant_key.value,
        document_id=document.document_id,
        status=DocumentStatus.DISABLED,
    )

    prepared_fixture.restore(prepared_fixture.manifest, specs=specs)

    assert prepared_fixture.status("org_a", "returns_exchange") == "active"


def test_restore_rejects_missing_active_chunks(prepared_fixture, specs):
    class MissingChunkStore:
        def __init__(self, delegate):
            self._delegate = delegate

        def __getattr__(self, name):
            return getattr(self._delegate, name)

        def list_active_chunks(self, *, organization_id, candidate_ids):
            return []

    manager = StageCFixtureManager(
        store=MissingChunkStore(prepared_fixture._store),
        ingestion=prepared_fixture._ingestion,
        loader=DocumentLoader(),
        chunker=KnowledgeChunker(),
    )

    with pytest.raises(ValueError, match="active chunk drift"):
        manager.restore(prepared_fixture.manifest, specs=specs)


def test_disabled_scenario_restores_even_after_error(prepared_fixture, valid_cases):
    disabled_case = next(
        case
        for case in valid_cases
        if case.scenario is StageCScenario.DOCUMENT_DISABLED
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        with prepared_fixture.scenario(disabled_case):
            assert prepared_fixture.status("org_a", "returns_exchange") == "disabled"
            assert prepared_fixture.status("org_a", "vip") == "disabled"
            assert all(
                not identity.is_active
                for key in ("returns_exchange", "vip")
                for identity in prepared_fixture.identity_index.identities_for_version(
                    prepared_fixture.document("org_a", key).active_version_id
                )
            )
            raise RuntimeError("provider failed")

    assert prepared_fixture.status("org_a", "returns_exchange") == "active"
    assert prepared_fixture.status("org_a", "vip") == "active"


def test_non_disabled_scenario_does_not_change_document_status(
    prepared_fixture, valid_cases
):
    ordinary_case = next(
        case
        for case in valid_cases
        if case.scenario is StageCScenario.MAIN_ACTIVE
    )
    before = prepared_fixture.status("org_a", "returns_exchange")

    with prepared_fixture.scenario(ordinary_case):
        assert prepared_fixture.status("org_a", "returns_exchange") == before

    assert prepared_fixture.status("org_a", "returns_exchange") == before
