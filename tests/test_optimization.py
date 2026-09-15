from __future__ import annotations

from types import SimpleNamespace

import pytest

from ms_agent_eval.core.coordinator import StageCoordinator
from ms_agent_eval.core.errors import IntegrityError
from ms_agent_eval.core.optimization import (
    normalize_copro_proposals,
    validate_copro_proposal_accounting,
)
from ms_agent_eval.core.stages import StageError, StageState


def test_duplicate_copro_proposals_create_an_audited_shortfall() -> None:
    candidates, accounting = normalize_copro_proposals(
        [
            {"instruction": "A useful rewrite", "output_prefix": "ignored"},
            {"instruction": " A useful rewrite ", "output_prefix": "also ignored"},
        ],
        requested_count=2,
        target_kind="skill",
        current_text="Current skill",
        current_output_prefix="",
    )

    assert candidates == [{"instruction": "A useful rewrite", "output_prefix": ""}]
    assert accounting == {
        "requested_count": 2,
        "received_count": 2,
        "accepted_count": 1,
        "malformed_dropped_count": 0,
        "unchanged_dropped_count": 0,
        "duplicate_dropped_count": 1,
        "excess_dropped_count": 0,
        "shortfall_count": 1,
        "degraded": True,
    }
    assert (
        validate_copro_proposal_accounting(
            accounting,
            expected_count=2,
            accepted_count=1,
        )
        == accounting
    )


def test_copro_proposal_normalization_drops_unchanged_and_excess_candidates() -> None:
    candidates, accounting = normalize_copro_proposals(
        [
            {"instruction": " Current global instructions ", "output_prefix": "ignored"},
            {"instruction": "Rewrite A", "output_prefix": "Response A:"},
            {"instruction": "Rewrite B", "output_prefix": "Response B:"},
            {"instruction": "Rewrite C", "output_prefix": "Response C:"},
        ],
        requested_count=2,
        target_kind="global_instructions",
        current_text="Current global instructions",
        current_output_prefix="",
    )

    assert candidates == [
        {"instruction": "Rewrite A", "output_prefix": ""},
        {"instruction": "Rewrite B", "output_prefix": ""},
    ]
    assert accounting["unchanged_dropped_count"] == 1
    assert accounting["excess_dropped_count"] == 1
    assert accounting["shortfall_count"] == 0
    assert accounting["degraded"] is False


def test_copro_proposal_normalization_discards_malformed_siblings() -> None:
    candidates, accounting = normalize_copro_proposals(
        [
            {"instruction": "A complete rewrite", "output_prefix": ""},
            {
                "determine": "A leaked markdown fragment",
                "instruction": "An incomplete rewrite",
                "output_prefix": "",
            },
            {"instruction": " ", "output_prefix": "Answer:"},
        ],
        requested_count=2,
        target_kind="global_instructions",
        current_text="Current global instructions",
        current_output_prefix="",
    )

    assert candidates == [{"instruction": "A complete rewrite", "output_prefix": ""}]
    assert accounting["received_count"] == 3
    assert accounting["accepted_count"] == 1
    assert accounting["malformed_dropped_count"] == 2
    assert accounting["shortfall_count"] == 1
    assert accounting["degraded"] is True


def test_copro_proposal_normalization_allows_an_empty_degraded_proposal() -> None:
    candidates, accounting = normalize_copro_proposals(
        [{"determine": "A leaked markdown fragment"}],
        requested_count=1,
        target_kind="skill",
        current_text="Current skill",
        current_output_prefix="",
    )

    assert candidates == []
    assert accounting["malformed_dropped_count"] == 1
    assert accounting["shortfall_count"] == 1
    assert accounting["degraded"] is True


def test_copro_proposal_normalization_accepts_text_as_provider_alias() -> None:
    candidates, accounting = normalize_copro_proposals(
        [{"text": "A complete rewritten skill"}],
        requested_count=1,
        target_kind="skill",
        current_text="Current skill",
        current_output_prefix="",
    )

    assert candidates == [
        {"instruction": "A complete rewritten skill", "output_prefix": ""}
    ]
    assert accounting["shortfall_count"] == 0


def test_copro_proposal_normalization_discards_ambiguous_text_alias() -> None:
    candidates, accounting = normalize_copro_proposals(
        [{"instruction": "Rewrite A", "text": "Rewrite B"}],
        requested_count=1,
        target_kind="skill",
        current_text="Current skill",
        current_output_prefix="",
    )

    assert candidates == []
    assert accounting["malformed_dropped_count"] == 1
    assert accounting["degraded"] is True


def test_copro_proposal_accounting_rejects_inconsistent_counts() -> None:
    with pytest.raises(IntegrityError, match="received_count is inconsistent"):
        validate_copro_proposal_accounting(
            {
                "requested_count": 2,
                "received_count": 2,
                "accepted_count": 1,
                "malformed_dropped_count": 0,
                "unchanged_dropped_count": 0,
                "duplicate_dropped_count": 0,
                "excess_dropped_count": 0,
                "shortfall_count": 1,
                "degraded": True,
            },
            expected_count=2,
            accepted_count=1,
        )


def test_copro_proposal_accounting_accepts_immutable_v1_results() -> None:
    legacy_accounting = {
        "requested_count": 2,
        "received_count": 2,
        "accepted_count": 1,
        "unchanged_dropped_count": 0,
        "duplicate_dropped_count": 1,
        "excess_dropped_count": 0,
        "shortfall_count": 1,
        "degraded": True,
    }

    assert (
        validate_copro_proposal_accounting(
            legacy_accounting,
            expected_count=2,
            accepted_count=1,
        )
        == legacy_accounting
    )


def test_historical_duplicate_failure_gets_an_append_only_retry() -> None:
    proposal = SimpleNamespace(packet_id="sha256:" + "a" * 64)
    retry = object()

    class FailedResult:
        error = StageError("integrityerror", "optimizer candidates must be distinct")

    class FailedStatus:
        state = StageState.FAILED
        result = FailedResult()

    class FakeLedger:
        retried = False

        def status(self, packet_id):  # type: ignore[no-untyped-def]
            assert packet_id == proposal.packet_id
            return FailedStatus()

        def retry(self, packet_id):  # type: ignore[no-untyped-def]
            assert packet_id == proposal.packet_id
            self.retried = True
            return retry

    coordinator = object.__new__(StageCoordinator)
    ledger = FakeLedger()
    coordinator.ledger = ledger

    assert coordinator._retry_legacy_copro_exhaustion(proposal) is retry  # type: ignore[arg-type]
    assert ledger.retried is True
