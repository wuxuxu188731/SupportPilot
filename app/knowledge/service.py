"""Deterministic server-owned budgets for adaptive knowledge search."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.knowledge.base import SearchBudgetExceededError

MAX_PLANNER_CALLS = 1
MAX_ROUNDS = 2
MAX_ROUND_QUERIES = (3, 2)
MAX_ASSESSOR_CALLS = 2
MAX_MODEL_CALLS = 3


@dataclass
class SearchBudget:
    """Tracks hard search limits that model output cannot alter."""

    deadline: float
    clock: Callable[[], float]
    planner_calls: int = 0
    assessor_calls: int = 0
    round_count: int = 0

    @classmethod
    def start(
        cls,
        *,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> "SearchBudget":
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        return cls(deadline=clock() + timeout_seconds, clock=clock)

    @property
    def model_calls(self) -> int:
        return self.planner_calls + self.assessor_calls

    def remaining_seconds(self) -> float:
        return self.deadline - self.clock()

    def ensure_time(self) -> None:
        if self.remaining_seconds() <= 0:
            raise SearchBudgetExceededError()

    def consume_planner_call(self) -> None:
        self.ensure_time()
        if (
            self.planner_calls >= MAX_PLANNER_CALLS
            or self.model_calls >= MAX_MODEL_CALLS
        ):
            raise SearchBudgetExceededError()
        self.planner_calls += 1

    def consume_assessor_call(self) -> None:
        self.ensure_time()
        if (
            self.assessor_calls >= MAX_ASSESSOR_CALLS
            or self.model_calls >= MAX_MODEL_CALLS
        ):
            raise SearchBudgetExceededError()
        self.assessor_calls += 1

    def start_round(self, *, query_count: int) -> None:
        self.ensure_time()
        if self.round_count >= MAX_ROUNDS:
            raise SearchBudgetExceededError()
        if (
            isinstance(query_count, bool)
            or not isinstance(query_count, int)
            or query_count < 1
            or query_count > MAX_ROUND_QUERIES[self.round_count]
        ):
            raise SearchBudgetExceededError()
        self.round_count += 1

    def external_timeout_seconds(self, max_seconds: int = 5) -> int:
        if (
            isinstance(max_seconds, bool)
            or not isinstance(max_seconds, int)
            or max_seconds < 1
        ):
            raise ValueError("max_seconds must be a positive integer")
        timeout = math.floor(min(max_seconds, self.remaining_seconds()))
        if timeout < 1:
            raise SearchBudgetExceededError()
        return timeout
