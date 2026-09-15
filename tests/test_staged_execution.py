from __future__ import annotations

from pathlib import Path

import pytest

from ms_agent_eval.core.errors import IntegrityError
from ms_agent_eval.core.stages import (
    StageBudget,
    StageError,
    StageExecutorIdentity,
    StageKind,
    StageLedger,
    StageModelIdentity,
    StagePacket,
    StageResult,
    StageState,
)
from ms_agent_eval.core.storage import FilesystemArtifactStore


def _packet(ledger: StageLedger) -> StagePacket:
    return StagePacket.create(
        run_id="run-1",
        experiment_id="baseline",
        experiment_revision_id=f"sha256:{'3' * 64}",
        stage=StageKind.SOLVE,
        attempt=1,
        dependencies=(),
        inputs=(ledger.put_json("case", {"task": "answer"}),),
        instructions=ledger.put_json("instructions", {"task": "solve"}),
        output_schema="solver-response-v1",
        executor=StageExecutorIdentity("remote", "openai_compatible", f"sha256:{'1' * 64}"),
        model=StageModelIdentity("shared-model", f"sha256:{'2' * 64}"),
        budget=StageBudget(1, 1000, 60),
    )


def _result(ledger: StageLedger, packet: StagePacket, worker: str = "worker") -> StageResult:
    return StageResult.create(
        packet=packet,
        status="completed",
        executor_version="1",
        worker_id=worker,
        outputs=(ledger.put_json("result", {"response": "done"}),),
        usage={"model_calls": 1},
        started_at="2026-09-12T00:00:00+00:00",
        completed_at="2026-09-12T00:00:01+00:00",
        observed_model="shared-model",
    )


def test_stage_state_reconstructs_after_restart_and_duplicate_submit_is_idempotent(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "external"
    ledger = StageLedger(FilesystemArtifactStore(data_root))
    packet = ledger.prepare(_packet(ledger))
    claim = ledger.claim(packet.packet_id, executor_id="remote", worker_id="worker")
    result = _result(ledger, packet)
    assert ledger.submit(result, lease_id=claim.lease_id).result_id == result.result_id
    assert ledger.submit(result, lease_id=claim.lease_id).result_id == result.result_id
    ledger.validate(packet.packet_id, lambda _packet, _result: None)
    ledger.persist(packet.packet_id)

    restarted = StageLedger(FilesystemArtifactStore(data_root))
    status = restarted.status(packet.packet_id)
    assert status.state is StageState.PERSISTED
    assert status.result is not None
    assert status.result.result_id == result.result_id


def test_conflicting_submission_is_rejected(tmp_path: Path) -> None:
    ledger = StageLedger(FilesystemArtifactStore(tmp_path / "external"))
    packet = ledger.prepare(_packet(ledger))
    claim = ledger.claim(packet.packet_id, executor_id="remote", worker_id="worker")
    ledger.submit(_result(ledger, packet), lease_id=claim.lease_id)

    with pytest.raises(IntegrityError, match="conflicting result"):
        ledger.submit(_result(ledger, packet, worker="other-worker"), lease_id=claim.lease_id)


def test_expired_claim_can_be_reclaimed(tmp_path: Path) -> None:
    now = [100.0]
    ledger = StageLedger(FilesystemArtifactStore(tmp_path / "external"), clock=lambda: now[0])
    packet = ledger.prepare(_packet(ledger))
    first = ledger.claim(packet.packet_id, executor_id="remote", worker_id="first", lease_seconds=5)
    now[0] = 106.0
    second = ledger.claim(
        packet.packet_id, executor_id="remote", worker_id="second", lease_seconds=5
    )
    assert first.lease_id != second.lease_id
    assert ledger.status(packet.packet_id).claim == second


def test_malformed_result_is_rejected_and_retry_is_append_only(tmp_path: Path) -> None:
    ledger = StageLedger(FilesystemArtifactStore(tmp_path / "external"))
    packet = ledger.prepare(_packet(ledger))
    claim = ledger.claim(packet.packet_id, executor_id="remote", worker_id="worker")
    ledger.submit(_result(ledger, packet), lease_id=claim.lease_id)

    with pytest.raises(IntegrityError, match="malformed"):
        ledger.validate(
            packet.packet_id,
            lambda _packet, _result: (_ for _ in ()).throw(IntegrityError("malformed")),
        )
    assert ledger.status(packet.packet_id).state is StageState.REJECTED
    retry = ledger.retry(packet.packet_id)
    assert retry.attempt == 2
    assert retry.packet_id != packet.packet_id
    assert retry.attempt_id != packet.attempt_id
    assert retry.work_id == packet.work_id
    assert ledger.status(packet.packet_id).state is StageState.REJECTED
    assert ledger.status(retry.packet_id).state is StageState.PREPARED


def test_work_identity_is_reusable_across_runs_but_attempt_identity_is_not(
    tmp_path: Path,
) -> None:
    ledger = StageLedger(FilesystemArtifactStore(tmp_path / "external"))
    first = ledger.prepare(_packet(ledger))
    claim = ledger.claim(first.packet_id, executor_id="remote", worker_id="worker")
    result = _result(ledger, first)
    ledger.submit(result, lease_id=claim.lease_id)
    ledger.validate(first.packet_id, lambda _packet, _result: None)
    ledger.persist(first.packet_id)

    second = StagePacket.create(
        run_id="run-2",
        experiment_id=first.experiment_id,
        experiment_revision_id=first.experiment_revision_id,
        stage=first.stage,
        attempt=1,
        dependencies=(),
        inputs=first.inputs,
        instructions=first.instructions,
        output_schema=first.output_schema,
        executor=first.executor,
        model=first.model,
        budget=first.budget,
    )
    ledger.prepare(second)

    assert second.work_id == first.work_id
    assert second.attempt_id != first.attempt_id
    assert ledger.reusable_result(second) == result

    refreshed = StagePacket.create(
        run_id="run-3",
        experiment_id=first.experiment_id,
        experiment_revision_id=first.experiment_revision_id,
        stage=first.stage,
        attempt=1,
        reuse_policy="refresh",
        dependencies=(),
        inputs=first.inputs,
        instructions=first.instructions,
        output_schema=first.output_schema,
        executor=first.executor,
        model=first.model,
        budget=first.budget,
    )
    assert refreshed.work_id == first.work_id
    assert ledger.reusable_result(refreshed) is None


def test_orchestration_metadata_does_not_change_semantic_work_identity(
    tmp_path: Path,
) -> None:
    ledger = StageLedger(FilesystemArtifactStore(tmp_path / "external"))
    instructions = ledger.put_json("instructions", {"task": "solve"})
    executor = StageExecutorIdentity("remote", "openai_compatible", f"sha256:{'1' * 64}")
    model = StageModelIdentity("shared-model", f"sha256:{'2' * 64}")
    budget = StageBudget(1, 1000, 60)
    semantic_request = {
        "task": "answer",
        "global_context": "global",
        "skill_context": "skill",
        "program_hash": f"sha256:{'4' * 64}",
    }
    first = StagePacket.create(
        run_id="run-1",
        experiment_id="optimize",
        experiment_revision_id=f"sha256:{'3' * 64}",
        stage=StageKind.SOLVE,
        attempt=1,
        dependencies=(f"sha256:{'5' * 64}",),
        inputs=(
            ledger.put_json(
                "case",
                {**semantic_request, "phase": "validation"},
            ),
        ),
        work_spec=semantic_request,
        instructions=instructions,
        output_schema="solver-response-v1",
        executor=executor,
        model=model,
        budget=budget,
    )
    second = StagePacket.create(
        run_id="run-2",
        experiment_id="optimize",
        experiment_revision_id=f"sha256:{'6' * 64}",
        stage=StageKind.SOLVE,
        attempt=1,
        dependencies=(f"sha256:{'7' * 64}",),
        inputs=(
            ledger.put_json(
                "case",
                {**semantic_request, "phase": "test"},
            ),
        ),
        work_spec=semantic_request,
        instructions=instructions,
        output_schema="solver-response-v1",
        executor=executor,
        model=model,
        budget=budget,
    )

    assert first.work_id == second.work_id
    assert first.attempt_id != second.attempt_id


def test_failed_result_records_normalized_error(tmp_path: Path) -> None:
    ledger = StageLedger(FilesystemArtifactStore(tmp_path / "external"))
    packet = ledger.prepare(_packet(ledger))
    claim = ledger.claim(packet.packet_id, executor_id="remote", worker_id="worker")
    failed = StageResult.create(
        packet=packet,
        status="failed",
        executor_version="1",
        worker_id="worker",
        started_at="2026-09-12T00:00:00+00:00",
        completed_at="2026-09-12T00:00:01+00:00",
        error=StageError("endpoint_error", "request failed"),
    )
    ledger.submit(failed, lease_id=claim.lease_id)
    assert ledger.status(packet.packet_id).state is StageState.FAILED
