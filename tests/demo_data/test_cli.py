import json

from app.demo_data.cli import main
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore


def test_cli_seeds_selected_organization(tmp_path, capsys):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    alice = users.create_user(username="alice", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Acme Support",
        admin_user_id=alice.user_id,
    )

    exit_code = main(
        [
            "--database-path",
            str(database_path),
            "--organization-id",
            organization.organization_id,
            "--actor-user-id",
            alice.user_id,
            "--reference-time",
            "2026-07-28T08:00:00+00:00",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["organization_id"] == organization.organization_id
    assert output["order_nos"] == [
        "ORD-DELAY-001",
        "ORD-TRANSIT-001",
        "ORD-DELIVERED-001",
    ]
