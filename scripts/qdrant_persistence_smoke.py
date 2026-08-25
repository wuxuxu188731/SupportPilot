"""Qdrant persistent-volume smoke script.

Proves a point written into Qdrant survives a container/restart (durability of
the underlying volume) and that ``read`` both verifies it and cleans up.

    python scripts/qdrant_persistence_smoke.py write
    docker compose restart qdrant
    python scripts/qdrant_persistence_smoke.py read

HARD SAFETY CONTRACT: the script only EVER operates on the single fixed
collection ``supportpilot_persistence_smoke``. It takes no collection-name
argument and refuses ANY other name — so an accidental run can never create or
delete a production index (e.g. ``supportpilot_knowledge_te4_1024_v1``). Any
extra positional argument is a hard error. Only the fixed collection is
written to or deleted from.

Connectivity targets ``QDRANT_URL`` (default ``http://localhost:6333``).
"""

from __future__ import annotations

import sys
from typing import no_type_check

from qdrant_client import QdrantClient
from qdrant_client import models

SMOKE_COLLECTION = "supportpilot_persistence_smoke"
# Real Qdrant only accepts unsigned-integer or UUID point IDs (a bare string is
# rejected with 400). Use a fixed UUID so ``write``/``read`` round-trip.
SMOKE_POINT_ID = "11111111-2222-4333-8444-555555555555"
# Dense vector must match the project's 1024-dim contract.
DENSE_SIZE = 1024


def _url() -> str:
    import os

    return os.getenv("QDRANT_URL", "http://localhost:6333").rstrip("/")


def _client() -> QdrantClient:
    return QdrantClient(url=_url(), timeout=10, check_compatibility=False)


def _guard_collection_only() -> None:
    """Refuse to accept an arbitrary collection name.

    The script takes exactly one subcommand (``write`` or ``read``) and no
    collection-name argument. If a caller slips an extra positional argument it
    is treated as a forbidden collection name and rejected, protecting the
    production index from accidental create/delete.
    """
    if len(sys.argv) != 2:
        extra = ", ".join(sys.argv[2:])
        raise SystemExit(
            f"usage: {sys.argv[0]} {{write|read}}\n"
            f"refusing extra positional args (would be a collection name): {extra!r}\n"
            f"The smoke script only touches the fixed collection "
            f"{SMOKE_COLLECTION!r} and never accepts arbitrary collection names."
        )


def _smoke_vector() -> dict:
    return {
        "dense": [0.7] * DENSE_SIZE,
        "sparse": models.SparseVector(indices=[13, 42], values=[1.0, 0.5]),
    }


def _write() -> None:
    client = _client()
    if SMOKE_COLLECTION in {c.name for c in client.get_collections().collections}:
        client.delete_collection(collection_name=SMOKE_COLLECTION)
    client.create_collection(
        collection_name=SMOKE_COLLECTION,
        vectors_config={
            "dense": models.VectorParams(
                size=DENSE_SIZE, distance=models.Distance.COSINE
            )
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(
                index=models.SparseIndexParams(on_disk=False)
            )
        },
    )
    client.create_payload_index(
        collection_name=SMOKE_COLLECTION,
        field_name="organization_id",
        field_schema=models.KeywordIndexParams(
            type=models.KeywordIndexType.KEYWORD,
            is_tenant=True,
        ),
    )
    client.upsert(
        collection_name=SMOKE_COLLECTION,
        points=[
            models.PointStruct(
                id=SMOKE_POINT_ID,
                vector=_smoke_vector(),
                payload={
                    "organization_id": "persistence-smoke",
                    "document_id": "smoke-doc",
                    "version_id": "smoke-v1",
                    "chunk_id": "smoke-chunk-0",
                    "ordinal": 0,
                },
            )
        ],
    )
    count = client.count(collection_name=SMOKE_COLLECTION, exact=True)
    print(f"OK wrote {count.count} point to {SMOKE_COLLECTION} on {_url()}")


def _read() -> int:
    client = _client()
    names = {c.name for c in client.get_collections().collections}
    if SMOKE_COLLECTION not in names:
        print(f"FAIL collection {SMOKE_COLLECTION} not found on {_url()}")
        return 1

    hits = client.retrieve(
        collection_name=SMOKE_COLLECTION,
        ids=[SMOKE_POINT_ID],
        with_payload=True,
        with_vectors=False,
    )
    if len(hits) != 1 or hits[0].id != SMOKE_POINT_ID:
        print(f"FAIL point {SMOKE_POINT_ID!r} missing after restart")
        return 1
    print(f"OK point {SMOKE_POINT_ID!r} survived restart on {_url()}")
    client.delete_collection(collection_name=SMOKE_COLLECTION)
    print(f"OK removed {SMOKE_COLLECTION} (cleanup complete)")
    return 0


@no_type_check
def main() -> int:
    _guard_collection_only()
    subcommand = sys.argv[1]
    if subcommand == "write":
        _write()
        return 0
    if subcommand == "read":
        return _read()
    raise SystemExit(
        f"unknown subcommand {subcommand!r}; expected 'write' or 'read'"
    )


if __name__ == "__main__":
    raise SystemExit(main())
