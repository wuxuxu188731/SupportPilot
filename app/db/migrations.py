import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REVISION = "0001_baseline"
BASELINE_TABLES = {"users", "conversations", "messages"}
BASELINE_COLUMNS = {
    "users": {
        "id",
        "username",
        "password_hash",
        "created_at",
    },
    "conversations": {
        "id",
        "user_id",
        "system_prompt",
        "created_at",
        "updated_at",
    },
    "messages": {
        "id",
        "conversation_id",
        "seq",
        "role",
        "payload_json",
        "created_at",
    },
}


class DatabaseMigrationError(RuntimeError):
    pass


def _table_names(database_path: Path) -> set[str]:
    if not database_path.exists():
        return set()
    with sqlite3.connect(database_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def _config(database_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_path"] = database_path
    return config


def _validate_legacy_schema(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        for table_name, expected_columns in BASELINE_COLUMNS.items():
            actual_columns = {
                row[1]
                for row in connection.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            }
            if actual_columns != expected_columns:
                raise DatabaseMigrationError(
                    f"legacy table {table_name} does not match baseline"
                )


def upgrade_database(database_path: str | Path) -> None:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tables = _table_names(path)
    has_version_table = "alembic_version" in tables
    business_tables = tables.intersection(BASELINE_TABLES)

    if not has_version_table and business_tables:
        if business_tables != BASELINE_TABLES:
            raise DatabaseMigrationError(
                "partial legacy schema detected; restore a complete backup"
            )
        _validate_legacy_schema(path)
        command.stamp(_config(path), BASELINE_REVISION)

    command.upgrade(_config(path), "head")
