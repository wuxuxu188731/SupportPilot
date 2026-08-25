"""Allow pre-planning failures to record the unplanned strategy."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0010_retrieval_event_unplanned"
down_revision: str | None = "0009_knowledge_base"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STRATEGIES = "'baseline', 'single', 'multi', 'none'"
NEW_STRATEGIES = OLD_STRATEGIES + ", 'unplanned'"


def _replace_strategy_check(allowed: str) -> None:
    with op.batch_alter_table(
        "retrieval_events", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_retrieval_events_strategy", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_retrieval_events_strategy",
            f"strategy IN ({allowed})",
        )


def upgrade() -> None:
    _replace_strategy_check(NEW_STRATEGIES)


def downgrade() -> None:
    bind = op.get_bind()
    unplanned_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM retrieval_events "
            "WHERE strategy = 'unplanned'"
        )
    ).scalar_one()
    if unplanned_count:
        raise RuntimeError(
            "cannot downgrade retrieval_events while unplanned rows exist"
        )
    _replace_strategy_check(OLD_STRATEGIES)
