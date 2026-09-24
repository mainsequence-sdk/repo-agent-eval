from __future__ import annotations

from ms_agent_eval.programs.dspy.budget import BudgetLedger, BudgetLimits


def test_token_usage_is_observed_not_an_execution_ceiling() -> None:
    """Legacy numeric token fields must never reject an accepted response."""

    ledger = BudgetLedger(
        BudgetLimits(
            model_calls=1,
            configured_cost=float("inf"),
            # A historical lock can still carry this numeric value.
            tokens=1,
            wall_seconds=60,
            concurrency=1,
        )
    )

    ledger.begin_call(0.0)
    ledger.finish_call({"total_tokens": 35_281})

    assert ledger.snapshot().tokens == 35_281
