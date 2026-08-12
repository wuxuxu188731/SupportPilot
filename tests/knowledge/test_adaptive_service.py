import pytest

from app.knowledge.base import SearchBudgetExceededError
from app.knowledge.service import SearchBudget


def test_budget_rejects_third_round_and_fourth_model_call():
    budget = SearchBudget.start(timeout_seconds=15, clock=lambda: 0.0)
    budget.consume_planner_call()
    budget.start_round(query_count=3)
    budget.consume_assessor_call()
    budget.start_round(query_count=2)
    budget.consume_assessor_call()

    with pytest.raises(SearchBudgetExceededError):
        budget.start_round(query_count=1)
    with pytest.raises(SearchBudgetExceededError):
        budget.consume_assessor_call()


def test_budget_rejects_query_caps_and_elapsed_deadline():
    budget = SearchBudget.start(timeout_seconds=15, clock=lambda: 0.0)
    with pytest.raises(SearchBudgetExceededError):
        budget.start_round(query_count=4)

    now = iter([0.0, 16.0])
    expired = SearchBudget.start(timeout_seconds=15, clock=lambda: next(now))
    with pytest.raises(SearchBudgetExceededError):
        expired.ensure_time()


def test_external_timeout_floors_remaining_time_and_never_returns_zero():
    now = iter([0.0, 11.2])
    budget = SearchBudget.start(timeout_seconds=15, clock=lambda: next(now))
    assert budget.external_timeout_seconds() == 3

    nearly_expired = iter([0.0, 14.2])
    budget = SearchBudget.start(
        timeout_seconds=15, clock=lambda: next(nearly_expired)
    )
    with pytest.raises(SearchBudgetExceededError):
        budget.external_timeout_seconds()
