"""动作工作流 0011 迁移的数据模型约束测试。

这些测试直接使用 SQLite 连接验证数据库层的租户隔离、唯一约束、
部分唯一索引与状态枚举检查约束，不依赖尚未实现的 SQLiteActionStore。
"""

import sqlite3

import pytest

from app.db.migrations import upgrade_database


# —— 构造测试数据的辅助函数 ——


def _fresh_database(tmp_path):
    database_path = tmp_path / "actions.db"
    upgrade_database(database_path)
    return database_path


def _connect(database_path):
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _seed_tenant(connection, org_id, user_id, role="admin"):
    connection.execute(
        "INSERT INTO users(id, username, password_hash) VALUES (?, ?, 'hash')",
        (user_id, user_id),
    )
    connection.execute(
        "INSERT INTO organizations(id, name) VALUES (?, ?)",
        (org_id, org_id),
    )
    connection.execute(
        "INSERT INTO memberships(organization_id, user_id, role) VALUES (?, ?, ?)",
        (org_id, user_id, role),
    )


def _seed_customer(connection, org_id, customer_id):
    connection.execute(
        """
        INSERT INTO customers(id, organization_id, customer_no, name, email, phone)
        VALUES (?, ?, ?, 'name', NULL, NULL)
        """,
        (customer_id, org_id, customer_id),
    )


def _seed_order(connection, org_id, order_id, customer_id, total_cents=10000):
    connection.execute(
        """
        INSERT INTO orders(
            id, organization_id, order_no, customer_id, status,
            item_summary, total_amount_cents, currency, placed_at
        ) VALUES (?, ?, ?, ?, 'paid', 'item', ?, 'CNY', '2026-08-28T00:00:00Z')
        """,
        (order_id, org_id, order_id, customer_id, total_cents),
    )


def _seed_scope(connection, org_id, user_id):
    """创建企业、用户、成员、客户与订单的完整前置数据链。"""
    _seed_tenant(connection, org_id, user_id)
    _seed_customer(connection, org_id, f"cust-{org_id}")
    _seed_order(connection, org_id, f"ord-{org_id}", f"cust-{org_id}")
    return f"ord-{org_id}"


def _insert_run(
    connection,
    run_id,
    org_id,
    user_id,
    workflow_type="refund",
    status="awaiting_approval",
    proposal_id=None,
):
    connection.execute(
        """
        INSERT INTO action_runs(
            id, organization_id, conversation_id, turn_id, created_by_user_id,
            workflow_type, status, thread_id, proposal_id
        ) VALUES (?, ?, 'conv-1', ?, ?, ?, ?, ?, ?)
        """,
        (run_id, org_id, run_id, user_id, workflow_type, status, run_id, proposal_id),
    )


def _insert_proposal(
    connection,
    proposal_id,
    org_id,
    run_id,
    order_id,
    user_id,
    action_type="refund",
    status="awaiting_approval",
    current_version_id=None,
):
    connection.execute(
        """
        INSERT INTO action_proposals(
            id, organization_id, run_id, order_id, action_type,
            status, current_version_id, created_by_user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (proposal_id, org_id, run_id, order_id, action_type, status, current_version_id, user_id),
    )


def _insert_version(
    connection,
    version_id,
    org_id,
    proposal_id,
    user_id,
    version_no=1,
    amount_cents=1000,
    currency="CNY",
    reason_code="quality_issue",
    reason_text="",
    parameters_json='{"refund_scope": "partial"}',
):
    connection.execute(
        """
        INSERT INTO action_proposal_versions(
            id, organization_id, proposal_id, version_no, amount_cents,
            currency, reason_code, reason_text, parameters_json, created_by_user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            org_id,
            proposal_id,
            version_no,
            amount_cents,
            currency,
            reason_code,
            reason_text,
            parameters_json,
            user_id,
        ),
    )


def _insert_approval(
    connection,
    approval_id,
    org_id,
    proposal_id,
    requested_version_id,
    user_id,
    status="pending",
):
    connection.execute(
        """
        INSERT INTO approvals(
            id, organization_id, proposal_id, requested_version_id,
            requested_by_user_id, status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (approval_id, org_id, proposal_id, requested_version_id, user_id, status),
    )


def _insert_decision(
    connection,
    decision_id,
    org_id,
    approval_id,
    decided_version_id,
    user_id,
    decision="approved",
):
    connection.execute(
        """
        INSERT INTO approval_decisions(
            id, organization_id, approval_id, decision, decided_version_id,
            decided_by_user_id, comment
        ) VALUES (?, ?, ?, ?, ?, ?, NULL)
        """,
        (decision_id, org_id, approval_id, decision, decided_version_id, user_id),
    )


def _insert_execution(
    connection,
    execution_id,
    org_id,
    proposal_id,
    proposal_version_id,
    action_type="refund",
    idempotency_key=None,
    status="claimed",
    attempt_count=1,
):
    key = idempotency_key or f"key-{execution_id}"
    connection.execute(
        """
        INSERT INTO tool_executions(
            id, organization_id, proposal_id, proposal_version_id, action_type,
            idempotency_key, status, attempt_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (execution_id, org_id, proposal_id, proposal_version_id, action_type, key, status, attempt_count),
    )


def _insert_refund_record(
    connection,
    record_id,
    org_id,
    order_id,
    proposal_id,
    proposal_version_id,
    tool_execution_id,
    amount_cents=1000,
    currency="CNY",
    reason_code="quality_issue",
    status="simulated_succeeded",
):
    connection.execute(
        """
        INSERT INTO refund_records(
            id, organization_id, order_id, proposal_id, proposal_version_id,
            tool_execution_id, amount_cents, currency, reason_code, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            org_id,
            order_id,
            proposal_id,
            proposal_version_id,
            tool_execution_id,
            amount_cents,
            currency,
            reason_code,
            status,
        ),
    )


def _insert_compensation_record(
    connection,
    record_id,
    org_id,
    order_id,
    proposal_id,
    proposal_version_id,
    tool_execution_id,
    amount_cents=1000,
    currency="CNY",
    reason_code="delayed_shipment",
    coupon_valid_days=30,
    status="simulated_succeeded",
):
    connection.execute(
        """
        INSERT INTO compensation_records(
            id, organization_id, order_id, proposal_id, proposal_version_id,
            tool_execution_id, amount_cents, currency, reason_code,
            coupon_valid_days, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            org_id,
            order_id,
            proposal_id,
            proposal_version_id,
            tool_execution_id,
            amount_cents,
            currency,
            reason_code,
            coupon_valid_days,
            status,
        ),
    )


def _insert_audit_log(
    connection,
    log_id,
    org_id,
    run_id,
    actor_type,
    actor_user_id=None,
    proposal_id=None,
):
    connection.execute(
        """
        INSERT INTO audit_logs(
            id, organization_id, run_id, proposal_id, actor_type,
            actor_user_id, event_type, resource_type, resource_id, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, 'run_created', 'action_run', ?, '{}')
        """,
        (log_id, org_id, run_id, proposal_id, actor_type, actor_user_id, run_id),
    )


# —— 约束测试 ——


def test_proposal_version_rejects_cross_tenant_proposal(tmp_path):
    # 保护行为：提案版本必须与提案同属一个企业，跨租户引用在复合外键层被拒绝。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _seed_scope(connection, "org-b", "user-b")
        _insert_run(connection, "run-a", "org-a", "user-a")
        _insert_proposal(connection, "proposal-a", "org-a", "run-a", "ord-org-a", "user-a")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_version(connection, "version-b", "org-b", "proposal-a", "user-b")


def test_approval_rejects_second_decision(tmp_path):
    # 保护行为：同一审批最多只能有一个决定，第二个决定被唯一约束拒绝。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a")
        _insert_proposal(connection, "proposal-a", "org-a", "run-a", "ord-org-a", "user-a")
        _insert_version(connection, "version-a", "org-a", "proposal-a", "user-a")
        _insert_approval(connection, "approval-a", "org-a", "proposal-a", "version-a", "user-a")

        _insert_decision(connection, "decision-1", "org-a", "approval-a", "version-a", "user-a")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_decision(connection, "decision-2", "org-a", "approval-a", "version-a", "user-a")


def test_execution_rejects_duplicate_idempotency_key(tmp_path):
    # 保护行为：同一幂等键只能有一个执行记录，重复幂等键被唯一约束拒绝。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a")
        _insert_proposal(connection, "proposal-a", "org-a", "run-a", "ord-org-a", "user-a")
        _insert_version(connection, "version-a", "org-a", "proposal-a", "user-a")

        _insert_execution(
            connection,
            "execution-1",
            "org-a",
            "proposal-a",
            "version-a",
            idempotency_key="action-execution:v1:shared-key",
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_execution(
                connection,
                "execution-2",
                "org-a",
                "proposal-a",
                "version-a",
                idempotency_key="action-execution:v1:shared-key",
            )


def test_compensation_rejects_duplicate_reason_per_order(tmp_path):
    # 保护行为：同一订单同一补偿原因只能成功一次。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a", workflow_type="compensation")
        _insert_proposal(
            connection,
            "proposal-a",
            "org-a",
            "run-a",
            "ord-org-a",
            "user-a",
            action_type="compensation",
        )
        _insert_version(
            connection,
            "version-1",
            "org-a",
            "proposal-a",
            "user-a",
            version_no=1,
            reason_code="delayed_shipment",
            parameters_json='{"coupon_valid_days": 30}',
        )
        _insert_version(
            connection,
            "version-2",
            "org-a",
            "proposal-a",
            "user-a",
            version_no=2,
            reason_code="delayed_shipment",
            parameters_json='{"coupon_valid_days": 30}',
        )
        _insert_execution(connection, "execution-1", "org-a", "proposal-a", "version-1", action_type="compensation")
        _insert_execution(connection, "execution-2", "org-a", "proposal-a", "version-2", action_type="compensation")

        _insert_compensation_record(
            connection,
            "record-1",
            "org-a",
            "ord-org-a",
            "proposal-a",
            "version-1",
            "execution-1",
            reason_code="delayed_shipment",
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_compensation_record(
                connection,
                "record-2",
                "org-a",
                "ord-org-a",
                "proposal-a",
                "version-2",
                "execution-2",
                reason_code="delayed_shipment",
            )


def test_partial_index_allows_only_one_active_proposal_per_order(tmp_path):
    # 保护行为：同订单同动作类型只能有一个非终态提案；进入终态后允许新建申请。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-1", "org-a", "user-a")
        _insert_proposal(connection, "proposal-1", "org-a", "run-1", "ord-org-a", "user-a")

        _insert_run(connection, "run-2", "org-a", "user-a")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_proposal(connection, "proposal-2", "org-a", "run-2", "ord-org-a", "user-a")

        connection.execute(
            "UPDATE action_proposals SET status = 'rejected' WHERE id = ?",
            ("proposal-1",),
        )
        _insert_run(connection, "run-3", "org-a", "user-a")
        _insert_proposal(connection, "proposal-3", "org-a", "run-3", "ord-org-a", "user-a")


def test_version_rejects_non_positive_amount(tmp_path):
    # 边界情况：提案版本金额必须为正整数，0 或负数被检查约束拒绝。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a")
        _insert_proposal(connection, "proposal-a", "org-a", "run-a", "ord-org-a", "user-a")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_version(connection, "version-a", "org-a", "proposal-a", "user-a", amount_cents=0)


def test_proposal_version_is_immutable(tmp_path):
    # 保护行为：提案版本一旦写入便不可修改或删除，确保审批历史引用的是稳定事实。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a")
        _insert_proposal(connection, "proposal-a", "org-a", "run-a", "ord-org-a", "user-a")
        _insert_version(connection, "version-a", "org-a", "proposal-a", "user-a")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE action_proposal_versions SET amount_cents = 2000 WHERE id = 'version-a'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM action_proposal_versions WHERE id = 'version-a'"
            )


def test_current_version_must_belong_to_same_proposal(tmp_path):
    # 保护行为：提案的 current_version_id 只能指向自身版本，不能串用同企业其他提案版本。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-refund", "org-a", "user-a")
        _insert_proposal(connection, "proposal-refund", "org-a", "run-refund", "ord-org-a", "user-a")
        _insert_version(connection, "version-refund", "org-a", "proposal-refund", "user-a")
        _insert_run(connection, "run-compensation", "org-a", "user-a", workflow_type="compensation")
        _insert_proposal(
            connection,
            "proposal-compensation",
            "org-a",
            "run-compensation",
            "ord-org-a",
            "user-a",
            action_type="compensation",
        )
        _insert_version(connection, "version-compensation", "org-a", "proposal-compensation", "user-a")

        connection.execute(
            "UPDATE action_proposals SET current_version_id = 'version-refund' WHERE id = 'proposal-refund'"
        )
        connection.execute(
            "UPDATE action_runs SET proposal_id = 'proposal-refund' WHERE id = 'run-refund'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE action_proposals SET current_version_id = 'version-compensation' WHERE id = 'proposal-refund'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE action_runs SET proposal_id = 'proposal-compensation' WHERE id = 'run-refund'"
            )


def test_approval_and_execution_reject_other_proposal_version(tmp_path):
    # 保护行为：审批请求和执行记录必须引用所属提案的版本，不能在同租户内拼接授权链。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-refund", "org-a", "user-a")
        _insert_proposal(connection, "proposal-refund", "org-a", "run-refund", "ord-org-a", "user-a")
        _insert_version(connection, "version-refund", "org-a", "proposal-refund", "user-a")
        _insert_run(connection, "run-compensation", "org-a", "user-a", workflow_type="compensation")
        _insert_proposal(
            connection,
            "proposal-compensation",
            "org-a",
            "run-compensation",
            "ord-org-a",
            "user-a",
            action_type="compensation",
        )
        _insert_version(connection, "version-compensation", "org-a", "proposal-compensation", "user-a")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_approval(
                connection,
                "approval-refund",
                "org-a",
                "proposal-refund",
                "version-compensation",
                "user-a",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_execution(
                connection,
                "execution-refund",
                "org-a",
                "proposal-refund",
                "version-compensation",
            )


def test_proposal_action_type_must_match_run(tmp_path):
    # 保护行为：提案动作类型必须与所属 Run 的工作流类型一致，禁止拼接不同业务流程。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a", workflow_type="refund")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_proposal(
                connection,
                "proposal-a",
                "org-a",
                "run-a",
                "ord-org-a",
                "user-a",
                action_type="compensation",
            )


def test_refund_result_must_match_approved_version(tmp_path):
    # 保护行为：退款业务结果的订单、执行记录及金额参数必须与提案版本保持一致。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a")
        _insert_proposal(connection, "proposal-a", "org-a", "run-a", "ord-org-a", "user-a")
        _insert_version(connection, "version-a", "org-a", "proposal-a", "user-a", amount_cents=1000)
        _insert_execution(connection, "execution-a", "org-a", "proposal-a", "version-a")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_refund_record(
                connection,
                "record-a",
                "org-a",
                "ord-org-a",
                "proposal-a",
                "version-a",
                "execution-a",
                amount_cents=2000,
            )


def test_approval_decision_is_immutable(tmp_path):
    # 保护行为：审批决定落库后不可修改或删除，重复请求只能读取原决定。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a")
        _insert_proposal(connection, "proposal-a", "org-a", "run-a", "ord-org-a", "user-a")
        _insert_version(connection, "version-a", "org-a", "proposal-a", "user-a")
        _insert_approval(connection, "approval-a", "org-a", "proposal-a", "version-a", "user-a")
        _insert_decision(connection, "decision-a", "org-a", "approval-a", "version-a", "user-a")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE approval_decisions SET decision = 'rejected' WHERE id = 'decision-a'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM approval_decisions WHERE id = 'decision-a'"
            )


def test_run_rejects_unknown_status(tmp_path):
    # 边界情况：动作 Run 状态必须是枚举内取值，未知状态被检查约束拒绝。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(connection, "run-a", "org-a", "user-a", status="bogus")


def test_run_rejects_unknown_workflow_type(tmp_path):
    # 边界情况：工作流类型必须是退款或补偿，未知类型被检查约束拒绝。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(connection, "run-a", "org-a", "user-a", workflow_type="coupon")


def test_decision_rejects_unknown_value(tmp_path):
    # 边界情况：审批决定必须是批准/修改后批准/拒绝，未知值被检查约束拒绝。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a")
        _insert_proposal(connection, "proposal-a", "org-a", "run-a", "ord-org-a", "user-a")
        _insert_version(connection, "version-a", "org-a", "proposal-a", "user-a")
        _insert_approval(connection, "approval-a", "org-a", "proposal-a", "version-a", "user-a")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_decision(
                connection,
                "decision-1",
                "org-a",
                "approval-a",
                "version-a",
                "user-a",
                decision="auto_approved",
            )


def test_audit_log_rejects_unknown_actor_type(tmp_path):
    # 边界情况：审计主体类型必须是 user 或 system，未知值被检查约束拒绝。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_audit_log(connection, "log-a", "org-a", "run-a", actor_type="robot")


def test_audit_log_requires_user_id_for_user_actor(tmp_path):
    # 边界情况：user 主体必须有 actor_user_id，缺失时被检查约束拒绝。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_audit_log(connection, "log-a", "org-a", "run-a", actor_type="user", actor_user_id=None)


def test_compensation_rejects_non_30_coupon_valid_days(tmp_path):
    # 边界情况：优惠券有效天数本阶段固定为 30，其他值被检查约束拒绝。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a", workflow_type="compensation")
        _insert_proposal(
            connection,
            "proposal-a",
            "org-a",
            "run-a",
            "ord-org-a",
            "user-a",
            action_type="compensation",
        )
        _insert_version(
            connection,
            "version-a",
            "org-a",
            "proposal-a",
            "user-a",
            reason_code="delayed_shipment",
            parameters_json='{"coupon_valid_days": 30}',
        )
        _insert_execution(connection, "execution-a", "org-a", "proposal-a", "version-a", action_type="compensation")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_compensation_record(
                connection,
                "record-a",
                "org-a",
                "ord-org-a",
                "proposal-a",
                "version-a",
                "execution-a",
                coupon_valid_days=10,
            )


def test_refund_record_rejects_unknown_status(tmp_path):
    # 边界情况：退款记录状态本阶段固定为 simulated_succeeded，未知状态被拒绝。
    database_path = _fresh_database(tmp_path)
    with _connect(database_path) as connection:
        _seed_scope(connection, "org-a", "user-a")
        _insert_run(connection, "run-a", "org-a", "user-a")
        _insert_proposal(connection, "proposal-a", "org-a", "run-a", "ord-org-a", "user-a")
        _insert_version(connection, "version-a", "org-a", "proposal-a", "user-a")
        _insert_execution(connection, "execution-a", "org-a", "proposal-a", "version-a")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_refund_record(
                connection,
                "record-a",
                "org-a",
                "ord-org-a",
                "proposal-a",
                "version-a",
                "execution-a",
                status="succeeded",
            )
