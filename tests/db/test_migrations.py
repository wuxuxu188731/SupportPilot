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
