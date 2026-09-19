import json
from datetime import datetime, timedelta

from app.demo_data.cli import main
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore


def build_organization(tmp_path):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    alice = users.create_user(username="alice", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Acme Support",
        admin_user_id=alice.user_id,
    )
    return database_path, organization, alice


def test_cli_seeds_selected_organization(tmp_path, capsys):
    database_path, organization, alice = build_organization(tmp_path)

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
    # 输出里带时间基准与新增计数：重复执行时能一眼看出没有新增。
    assert output["reference_at"] == "2026-07-28T08:00:00+00:00"
    assert output["created_counts"] == {
        "customers": 2,
        "orders": 3,
        "shipments": 2,
        "tickets": 2,
        "ticket_comments": 3,
    }


def test_cli_refresh_times_realigns_existing_demo_data(tmp_path, capsys):
    # 保护行为：--refresh-times 让「放久后过期」的演示时间重新对齐到新的
    # 时间基准；第二次运行不新增任何行（created_counts 全为 0）。
    database_path, organization, alice = build_organization(tmp_path)
    base_argv = [
        "--database-path",
        str(database_path),
        "--organization-id",
        organization.organization_id,
        "--actor-user-id",
        alice.user_id,
    ]

    assert main(base_argv + ["--reference-time", "2026-07-28T08:00:00+00:00"]) == 0
    capsys.readouterr()

    later = datetime.fromisoformat("2026-09-28T08:00:00+00:00")
    exit_code = main(
        base_argv
        + ["--reference-time", later.isoformat(), "--refresh-times"]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["created_counts"] == {
        "customers": 0,
        "orders": 0,
        "shipments": 0,
        "tickets": 0,
        "ticket_comments": 0,
    }

    orders = SQLiteOrderStore(database_path)
    delayed = orders.get_by_no(
        organization_id=organization.organization_id,
        order_no="ORD-DELAY-001",
    )
    assert datetime.fromisoformat(delayed.promised_ship_at) == (
        later - timedelta(days=3)
    )
