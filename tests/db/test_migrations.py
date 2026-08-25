import sqlite3

import pytest
from alembic import command
from alembic.config import Config

from app.db.migrations import DatabaseMigrationError, upgrade_database


BUSINESS_BASELINE_TABLES = {"users", "conversations", "messages"}
EXPECTED_BASELINE_TABLES = BUSINESS_BASELINE_TABLES | {"alembic_version"}


def table_names(database_path):
    with sqlite3.connect(database_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def alembic_config(database_path):
    config = Config("alembic.ini")
    config.attributes["database_path"] = database_path
    return config


def create_legacy_schema(database_path):
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                system_prompt TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                UNIQUE(conversation_id, seq)
            );
            """
        )


def test_fresh_database_upgrades_to_baseline(tmp_path):
    database_path = tmp_path / "fresh.db"

    upgrade_database(database_path)

    assert EXPECTED_BASELINE_TABLES <= table_names(database_path)


def test_existing_current_schema_is_stamped_without_data_loss(tmp_path):
    database_path = tmp_path / "legacy.db"
    create_legacy_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id, username, password_hash)
            VALUES ('user-1', 'alice', 'hash')
            """
        )

    upgrade_database(database_path)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT id, username FROM users"
        ).fetchone()
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]

    assert row == ("user-1", "alice")
    assert revision is not None


def test_partial_legacy_schema_is_rejected(tmp_path):
    database_path = tmp_path / "partial.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE users(id TEXT PRIMARY KEY)")

    with pytest.raises(DatabaseMigrationError):
        upgrade_database(database_path)


def test_complete_table_names_with_wrong_columns_are_rejected(tmp_path):
    database_path = tmp_path / "wrong-columns.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY);
            CREATE TABLE conversations(id TEXT PRIMARY KEY);
            CREATE TABLE messages(id INTEGER PRIMARY KEY);
            """
        )

    with pytest.raises(DatabaseMigrationError):
        upgrade_database(database_path)


def test_baseline_downgrades_to_empty_database(tmp_path):
    database_path = tmp_path / "rollback.db"
    upgrade_database(database_path)

    command.downgrade(alembic_config(database_path), "base")

    assert not BUSINESS_BASELINE_TABLES.intersection(
        table_names(database_path)
    )


def test_upgrade_creates_organization_and_membership_schema(tmp_path):
    database_path = tmp_path / "tenant-schema.db"

    upgrade_database(database_path)

    assert {"organizations", "memberships"} <= table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        membership_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(memberships)"
            ).fetchall()
        }

    assert membership_columns == {
        "organization_id",
        "user_id",
        "role",
        "created_at",
    }


def test_existing_users_receive_personal_admin_memberships(tmp_path):
    database_path = tmp_path / "legacy-users.db"
    create_legacy_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id, username, password_hash)
            VALUES ('user-1', 'alice', 'hash')
            """
        )

    upgrade_database(database_path)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT o.name, m.user_id, m.role
            FROM memberships AS m
            JOIN organizations AS o ON o.id = m.organization_id
            """
        ).fetchone()

    assert row == ("alice organization", "user-1", "admin")


def test_latest_tenant_migration_can_rollback_without_losing_users(tmp_path):
    database_path = tmp_path / "tenant-rollback.db"
    upgrade_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id, username, password_hash)
            VALUES ('user-1', 'alice', 'hash')
            """
        )

    command.downgrade(
        alembic_config(database_path),
        "0001_baseline",
    )

    assert "organizations" not in table_names(database_path)
    assert "memberships" not in table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT username FROM users WHERE id = 'user-1'"
        ).fetchone() == ("alice",)


def test_legacy_conversation_is_backfilled_to_users_personal_org(tmp_path):
    database_path = tmp_path / "conversation-backfill.db"
    create_legacy_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id, username, password_hash)
            VALUES ('user-1', 'alice', 'hash')
            """
        )
        connection.execute(
            """
            INSERT INTO conversations(id, user_id)
            VALUES ('conversation-1', 'user-1')
            """
        )

    upgrade_database(database_path)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT c.organization_id, m.user_id, m.role
            FROM conversations AS c
            JOIN memberships AS m
              ON m.organization_id = c.organization_id
             AND m.user_id = c.user_id
            WHERE c.id = 'conversation-1'
            """
        ).fetchone()

    assert row[0]
    assert row[1:] == ("user-1", "admin")


def test_orphan_legacy_conversation_blocks_tenant_migration(tmp_path):
    database_path = tmp_path / "orphan.db"
    create_legacy_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO conversations(id, user_id)
            VALUES ('conversation-1', 'missing-user')
            """
        )

    with pytest.raises(RuntimeError, match="unowned legacy conversations"):
        upgrade_database(database_path)


def test_conversation_tenant_scope_can_rollback_to_memberships(tmp_path):
    database_path = tmp_path / "conversation-rollback.db"
    upgrade_database(database_path)

    command.downgrade(
        alembic_config(database_path),
        "0002_organization_memberships",
    )

    assert {"organizations", "memberships"} <= table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(conversations)"
            ).fetchall()
        }
    assert "organization_id" not in columns


def test_membership_rejects_unknown_role_and_duplicate_user(tmp_path):
    database_path = tmp_path / "constraints.db"
    upgrade_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO users(id, username, password_hash)
            VALUES ('user-1', 'alice', 'hash')
            """
        )
        connection.execute(
            """
            INSERT INTO organizations(id, name)
            VALUES ('org-1', 'Acme')
            """
        )
        connection.execute(
            """
            INSERT INTO memberships(organization_id, user_id, role)
            VALUES ('org-1', 'user-1', 'admin')
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO memberships(organization_id, user_id, role)
                VALUES ('org-1', 'user-1', 'agent')
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO memberships(organization_id, user_id, role)
                VALUES ('org-1', 'missing-user', 'owner')
                """
            )


def test_customer_migration_upgrade_and_rollback(tmp_path):
    database_path = tmp_path / "customers.db"
    config = alembic_config(database_path)

    command.upgrade(config, "0004_customers")

    assert "customers" in table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(customers)"
            ).fetchall()
        }
    assert columns == {
        "id",
        "organization_id",
        "customer_no",
        "name",
        "email",
        "phone",
        "created_at",
    }

    command.downgrade(config, "0003_conversation_tenant_scope")

    assert "customers" not in table_names(database_path)
    assert {"organizations", "memberships"} <= table_names(database_path)


def test_order_migration_upgrade_and_rollback(tmp_path):
    database_path = tmp_path / "orders.db"
    config = alembic_config(database_path)

    command.upgrade(config, "0005_orders")

    assert {"customers", "orders"} <= table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(orders)"
            ).fetchall()
        }
    assert columns == {
        "id",
        "organization_id",
        "order_no",
        "customer_id",
        "status",
        "item_summary",
        "total_amount_cents",
        "currency",
        "placed_at",
        "promised_ship_at",
        "created_at",
        "updated_at",
    }

    command.downgrade(config, "0004_customers")

    assert "orders" not in table_names(database_path)
    assert "customers" in table_names(database_path)


def test_ticket_migration_upgrade_and_rollback(tmp_path):
    database_path = tmp_path / "tickets.db"
    config = alembic_config(database_path)

    command.upgrade(config, "0007_tickets")

    assert {"customers", "orders", "tickets"} <= table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(tickets)"
            ).fetchall()
        }
    assert columns == {
        "id",
        "organization_id",
        "ticket_no",
        "customer_id",
        "order_id",
        "created_by_user_id",
        "assigned_to_user_id",
        "summary",
        "category",
        "priority",
        "status",
        "created_at",
        "updated_at",
    }

    command.downgrade(config, "0006_shipments")

    assert "tickets" not in table_names(database_path)
    assert {"orders", "shipments"} <= table_names(database_path)


def test_shipment_migration_upgrade_and_rollback(tmp_path):
    database_path = tmp_path / "shipments.db"
    config = alembic_config(database_path)

    command.upgrade(config, "0006_shipments")

    assert {"orders", "shipments"} <= table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(shipments)"
            ).fetchall()
        }
    assert columns == {
        "id",
        "organization_id",
        "shipment_no",
        "order_id",
        "carrier",
        "tracking_no",
        "status",
        "last_event",
        "shipped_at",
        "estimated_delivery_at",
        "delivered_at",
        "created_at",
        "updated_at",
    }

    command.downgrade(config, "0005_orders")

    assert "shipments" not in table_names(database_path)
    assert "orders" in table_names(database_path)


def test_ticket_comment_migration_upgrade_and_rollback(tmp_path):
    database_path = tmp_path / "ticket-comments.db"
    config = alembic_config(database_path)

    command.upgrade(config, "0008_ticket_comments")

    assert {"tickets", "ticket_comments"} <= table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(ticket_comments)"
            ).fetchall()
        }
    assert columns == {
        "id",
        "organization_id",
        "ticket_id",
        "seq",
        "author_user_id",
        "visibility",
        "content",
        "created_at",
    }

    command.downgrade(config, "0007_tickets")

    assert "ticket_comments" not in table_names(database_path)
    assert "tickets" in table_names(database_path)


def seed_two_memberships(database_path):
    """Insert org-a/user-a and org-b/user-b admin memberships for real.

    Foreign keys are enabled inside the same connection so memberships can
    only be created for users and organizations that actually exist.
    """
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for user_id in ("user-a", "user-b"):
            connection.execute(
                """
                INSERT INTO users(id, username, password_hash)
                VALUES (?, ?, 'hash')
                """,
                (user_id, user_id),
            )
        for org_id in ("org-a", "org-b"):
            connection.execute(
                """
                INSERT INTO organizations(id, name)
                VALUES (?, ?)
                """,
                (org_id, org_id),
            )
        for org_id, user_id in (("org-a", "user-a"), ("org-b", "user-b")):
            connection.execute(
                """
                INSERT INTO memberships(organization_id, user_id, role)
                VALUES (?, ?, 'admin')
                """,
                (org_id, user_id),
            )


def test_knowledge_migration_upgrade_and_rollback(tmp_path):
    database_path = tmp_path / "knowledge.db"
    config = alembic_config(database_path)

    command.upgrade(config, "0009_knowledge_base")

    assert {
        "documents",
        "document_versions",
        "document_chunks",
        "ingestion_jobs",
        "retrieval_events",
    } <= table_names(database_path)

    command.downgrade(config, "0008_ticket_comments")

    assert "documents" not in table_names(database_path)
    assert "ticket_comments" in table_names(database_path)


def test_knowledge_schema_rejects_cross_tenant_version(tmp_path):
    database_path = tmp_path / "knowledge-constraints.db"
    upgrade_database(database_path)
    seed_two_memberships(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO documents(
                id, organization_id, uploaded_by_user_id,
                title, source_type, status
            ) VALUES ('doc-a', 'org-a', 'user-a', 'A', 'text', 'processing')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO document_versions(
                    id, organization_id, document_id, version_no,
                    content_hash, raw_text, loader_version,
                    chunker_version, embedding_model, embedding_dimensions
                ) VALUES (
                    'version-b', 'org-b', 'doc-a', 1, 'hash', 'body',
                    'loader-v1', 'chunker-v1', 'text-embedding-v4', 1024
                )
                """
            )


def _insert_retrieval_event(connection, event_id, strategy):
    connection.execute(
        """
        INSERT INTO retrieval_events(
            id, organization_id, conversation_id, strategy, original_query,
            planned_queries_json, round_count, candidate_json,
            selected_chunk_ids_json, outcome, latency_ms, model_calls,
            estimated_tokens, created_at
        ) VALUES (?, 'org-a', NULL, ?, 'digest', '[]', 0, '{}', '[]',
                  'insufficient', 1, 0, 0, '2026-08-13T00:00:00Z')
        """,
        (event_id, strategy),
    )


def test_unplanned_strategy_migration_preserves_rows_and_index(tmp_path):
    database_path = tmp_path / "unplanned-upgrade.db"
    config = alembic_config(database_path)
    command.upgrade(config, "0009_knowledge_base")
    with sqlite3.connect(database_path) as connection:
        _insert_retrieval_event(connection, "event-baseline", "baseline")

    command.upgrade(config, "0010_retrieval_event_unplanned")

    with sqlite3.connect(database_path) as connection:
        _insert_retrieval_event(connection, "event-unplanned", "unplanned")
        strategies = connection.execute(
            "SELECT strategy FROM retrieval_events ORDER BY id"
        ).fetchall()
        indexes = connection.execute(
            "PRAGMA index_list(retrieval_events)"
        ).fetchall()
    assert strategies == [("baseline",), ("unplanned",)]
    assert "idx_retrieval_events_org_created" in {row[1] for row in indexes}


def test_unplanned_strategy_downgrade_restores_check_when_safe(tmp_path):
    database_path = tmp_path / "unplanned-safe-downgrade.db"
    config = alembic_config(database_path)
    command.upgrade(config, "0010_retrieval_event_unplanned")
    command.downgrade(config, "0009_knowledge_base")

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_retrieval_event(connection, "event-unplanned", "unplanned")


def test_unplanned_strategy_downgrade_refuses_data_loss(tmp_path):
    database_path = tmp_path / "unplanned-protected-downgrade.db"
    config = alembic_config(database_path)
    command.upgrade(config, "0010_retrieval_event_unplanned")
    with sqlite3.connect(database_path) as connection:
        _insert_retrieval_event(connection, "event-unplanned", "unplanned")

    with pytest.raises(RuntimeError, match="unplanned"):
        command.downgrade(config, "0009_knowledge_base")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT strategy FROM retrieval_events WHERE id='event-unplanned'"
        ).fetchone() == ("unplanned",)
