from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import json
from pathlib import Path

import pytest
import yaml

from ms_agent_eval.core.errors import PreflightError
from ms_agent_eval.core.hashing import content_hash, sha256_file
from ms_agent_eval.core.planning import compile_workspace
from ms_agent_eval.core.providers import ModelCallObserver
from ms_agent_eval.core.runner import ExperimentRunner
from ms_agent_eval.core.stages import StageKind, StageState
from ms_agent_eval.core.workspace import ResolvedRoleModel, WorkspaceRepository
from ms_agent_eval.programs.dspy.budget import BudgetLedger

from .helpers import (
    FixedObservedLM,
    create_checkout,
    create_snapshot,
    create_workspace,
    judge_response,
    seed_cases_and_calibration,
    solver_response,
)


def _environment() -> Mapping[str, str]:
    return {"TEST_API_KEY": "test-secret"}


def _compiled(tmp_path: Path):  # type: ignore[no-untyped-def]
    repository = create_workspace(tmp_path / "workspace")
    lock, directory = create_snapshot(repository, create_checkout(tmp_path), tmp_path / "external")
    seed_cases_and_calibration(
        repository,
        authoring_instructions_hash=sha256_file(repository.case_authoring_path),
        source_snapshot_hash=lock.content_hash,
    )
    return compile_workspace(repository, snapshot=lock, snapshot_directory=directory)


def test_fixed_solver_and_judge_run_through_observed_dspy_contract(tmp_path: Path) -> None:
    compiled = _compiled(tmp_path)
    responses = {
        "solver": [solver_response() for _ in range(3)],
        "judge": [
            judge_response(1.0),
            judge_response(0.5),
            judge_response(0.0),
            judge_response(0.0),
            judge_response(0.0),
            *[judge_response(1.0) for _ in range(3)],
        ],
    }
    factory_calls: Counter[str] = Counter()

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        assert api_key == "test-secret"
        factory_calls[model.role] += 1
        del budget
        return FixedObservedLM(observer, responses[model.role], model=model.role)

    runner = ExperimentRunner(compiled, lm_factory=factory)
    run = runner.run("baseline", environment=_environment())
    assert run.status == "completed"
    assert run.result is not None
    assert run.result["case_count"] == 3
    assert all(item["passed"] for item in run.result["cases"])
    assert run.result["role_usage"]["solver"]["model_calls"] == 3
    assert run.result["role_usage"]["judge"]["model_calls"] == 8
    assert set(run.result["role_usage"]) == {"solver", "judge"}
    assert all(item["state"] == "persisted" for item in run.packets)
    solve_packets = [
        item.packet
        for item in runner.coordinator.ledger.list(run.run_id)
        if item.packet.stage is StageKind.SOLVE
    ]
    for packet in solve_packets:
        case_input = runner.coordinator._input(packet, "case")
        assert not {
            "rubric",
            "expected_response",
            "expected_artifacts",
            "judge_feedback",
        } & set(case_input)
    before_resume = factory_calls.copy()
    resumed = runner.resume(run.run_id, environment=_environment())
    assert resumed.status == "completed"
    assert factory_calls == before_resume


def test_optimization_publishes_json_state_before_held_out_test(tmp_path: Path) -> None:
    compiled = _compiled(tmp_path)
    responses = {
        "solver": [solver_response(), solver_response()],
        "judge": [
            judge_response(1.0),
            judge_response(0.5),
            judge_response(0.0),
            judge_response(0.0),
            judge_response(0.0),
            judge_response(1.0),
            judge_response(1.0),
        ],
    }

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        assert api_key == "test-secret"
        del budget
        return FixedObservedLM(observer, responses[model.role], model=model.role)

    runner = ExperimentRunner(compiled, lm_factory=factory)
    run = runner.run("optimize", environment=_environment())
    assert run.status == "completed"
    assert run.result is not None
    assert [item["split"] for item in run.result["cases"]] == ["test"]
    optimize = next(
        item
        for item in runner.coordinator.ledger.list(run.run_id)
        if item.packet.stage is StageKind.OPTIMIZE and item.state is StageState.PERSISTED
    )
    assert optimize.result is not None
    manifest = runner.coordinator.ledger.read_output(optimize.result)
    assert manifest["state_format"] == "json"
    assert manifest["published"] is True


def test_optimization_rejects_calibration_on_held_out_case_before_lm_creation(
    tmp_path: Path,
) -> None:
    compiled = _compiled(tmp_path)
    manifest = compiled.repository.calibration_root / "manifest.yaml"
    payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    for fixture in payload["fixtures"]:
        fixture["case"] = "case-test"
    manifest.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    recompiled = compile_workspace(
        compiled.repository,
        snapshot=compiled.snapshot,
        snapshot_directory=compiled.snapshot_directory,
    )
    calls = 0

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        nonlocal calls
        assert api_key == "test-secret"
        calls += 1
        return FixedObservedLM(observer, [], model=model.role)

    with pytest.raises(PreflightError, match="held-out"):
        ExperimentRunner(recompiled, lm_factory=factory).run("optimize", environment=_environment())
    assert calls == 0


def test_endpoint_failure_is_durable_and_redacts_credentials(tmp_path: Path) -> None:
    compiled = _compiled(tmp_path)

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        del model, observer, budget
        raise RuntimeError(f"endpoint rejected credential {api_key}")

    runner = ExperimentRunner(compiled, lm_factory=factory)
    run = runner.run("baseline", environment=_environment())
    assert run.status == "awaiting_execution"
    calibration = next(
        item
        for item in runner.coordinator.ledger.list(run.run_id)
        if item.packet.stage is StageKind.CALIBRATE_JUDGE
    )
    assert calibration.state is StageState.FAILED
    assert calibration.result is not None
    assert calibration.result.error is not None
    assert calibration.result.error.message == "endpoint rejected credential [REDACTED]"
    evidence = "".join(
        path.read_text(encoding="utf-8")
        for path in compiled.repository.data_root.rglob("*.json")
    )
    assert "test-secret" not in evidence


def test_remote_solver_can_pause_before_a_local_harness_judge(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    repository = create_workspace(workspace_root)
    payload = yaml.safe_load(repository.workspace_file.read_text(encoding="utf-8"))
    payload["stages"]["calibrate_judge"]["executor"] = "authoring-harness"
    payload["stages"]["judge"]["executor"] = "authoring-harness"
    repository.workspace_file.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    repository = WorkspaceRepository.from_file(repository.workspace_file)
    lock, directory = create_snapshot(
        repository, create_checkout(tmp_path), tmp_path / "external"
    )
    seed_cases_and_calibration(
        repository,
        authoring_instructions_hash=sha256_file(repository.case_authoring_path),
        source_snapshot_hash=lock.content_hash,
    )
    compiled = compile_workspace(repository, snapshot=lock, snapshot_directory=directory)
    solver_calls = 0

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        nonlocal solver_calls
        assert model.role == "solver"
        assert api_key == "test-secret"
        del budget
        solver_calls += 1
        return FixedObservedLM(observer, [solver_response()], model=model.role)

    runner = ExperimentRunner(compiled, lm_factory=factory)
    waiting = runner.run("baseline", environment=_environment())
    assert waiting.status == "awaiting_execution"
    calibration = next(
        item.packet
        for item in runner.coordinator.ledger.list(waiting.run_id)
        if item.packet.stage is StageKind.CALIBRATE_JUDGE
    )
    calibration_input = runner.coordinator._input(calibration, "calibration")
    assert all(
        "score_range" not in fixture and "label" not in fixture
        for fixture in calibration_input["fixtures"]
    )
    scores = {
        fixture.id: (fixture.minimum_score + fixture.maximum_score) / 2
        for fixture in compiled.calibration.fixtures
    }
    passes = {
        fixture.id: fixture.label == "strong"
        for fixture in compiled.calibration.fixtures
    }
    identity = {
        "passed": True,
        "fixture_scores": scores,
        "fixture_passes": passes,
        "corpus_hash": calibration_input["corpus_hash"],
    }
    result_file = tmp_path / "calibration-result.json"
    result_file.write_text(
        json.dumps({**identity, "content_hash": content_hash(identity)}), encoding="utf-8"
    )
    claim = runner.coordinator.ledger.claim(
        calibration.packet_id,
        executor_id="authoring-harness",
        worker_id="local-codex",
    )
    runner.coordinator.submit_file(
        calibration.packet_id,
        result_file,
        executor_id="authoring-harness",
        worker_id="local-codex",
        lease_id=claim.lease_id,
    )

    solved = runner.resume(waiting.run_id, environment=_environment(), through="solve")
    assert solved.status == "paused"
    assert solver_calls == 3
    judging = runner.resume(waiting.run_id, environment=_environment())
    assert judging.status == "awaiting_execution"
    assert judging.current_stage == "judge"
    assert solver_calls == 3
