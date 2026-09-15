from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class BudgetLimits:
    model_calls: int
    configured_cost: float
    # Retained as an observable usage field for historical lock compatibility.
    # It is not an execution ceiling.
    tokens: int | None
    wall_seconds: float
    concurrency: int


@dataclass(frozen=True)
class BudgetSnapshot:
    model_calls: int
    configured_cost: float
    tokens: int
    active_calls: int
    elapsed_seconds: float


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str, snapshot: BudgetSnapshot) -> None:
        super().__init__(f"LLM role budget exceeded: {reason}")
        self.reason = reason
        self.snapshot = snapshot


class BudgetLedger:
    """Thread-safe outer budget ledger independent from DSPy's optimizer settings."""

    def __init__(
        self,
        limits: BudgetLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._clock = clock
        self._started_at = clock()
        self._model_calls = 0
        self._configured_cost = 0.0
        self._tokens = 0
        self._active_calls = 0
        self._lock = threading.Lock()

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def begin_call(self, configured_cost: float) -> None:
        with self._lock:
            snapshot = self._snapshot_unlocked()
            self._raise_if_elapsed(snapshot)
            if snapshot.active_calls >= self.limits.concurrency:
                raise BudgetExceeded("concurrency", snapshot)
            if snapshot.model_calls >= self.limits.model_calls:
                raise BudgetExceeded("model_calls", snapshot)
            if snapshot.configured_cost + configured_cost > self.limits.configured_cost:
                raise BudgetExceeded("configured_cost", snapshot)
            self._model_calls += 1
            self._configured_cost += configured_cost
            self._active_calls += 1

    def finish_call(self, usage: Mapping[str, object] | None = None) -> None:
        with self._lock:
            if self._active_calls < 1:
                raise RuntimeError("optimizer budget call accounting underflow")
            self._active_calls -= 1
            raw_tokens = (usage or {}).get("total_tokens", 0)
            tokens = raw_tokens if isinstance(raw_tokens, int) and raw_tokens >= 0 else 0
            self._tokens += tokens
            snapshot = self._snapshot_unlocked()
            self._raise_if_elapsed(snapshot)

    def check(self) -> None:
        with self._lock:
            self._raise_if_elapsed(self._snapshot_unlocked())

    def _snapshot_unlocked(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            self._model_calls,
            round(self._configured_cost, 10),
            self._tokens,
            self._active_calls,
            max(0.0, self._clock() - self._started_at),
        )

    def _raise_if_elapsed(self, snapshot: BudgetSnapshot) -> None:
        if snapshot.elapsed_seconds >= self.limits.wall_seconds:
            raise BudgetExceeded("wall_seconds", snapshot)
