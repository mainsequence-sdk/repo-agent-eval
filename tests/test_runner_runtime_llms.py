from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Mapping
from io import BytesIO
import json
import os
from pathlib import Path

import pytest
import yaml

from ms_agent_eval.core.errors import PreflightError
from ms_agent_eval.core.coordinator import StageCoordinator
from ms_agent_eval.core.evaluation import CaseDefinition
from ms_agent_eval.core.hashing import content_hash, sha256_file
from ms_agent_eval.core.planning import compile_workspace
from ms_agent_eval.core.progress import ProgressEvent
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


def _pilot_compiled(tmp_path: Path):  # type: ignore[no-untyped-def]
    repository = create_workspace(tmp_path / "workspace")
    catalog_file = repository.capability_catalog_path
    catalog = yaml.safe_load(catalog_file.read_text(encoding="utf-8"))
    catalog["default_minimum_primary_cases"] = 2
    catalog_file.write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")
    lock, directory = create_snapshot(
        repository, create_checkout(tmp_path), tmp_path / "external"
    )
    seed_cases_and_calibration(
        repository,
        authoring_instructions_hash=sha256_file(repository.case_authoring_path),
        source_snapshot_hash=lock.content_hash,
    )
    with pytest.raises(PreflightError, match="case bank does not satisfy"):
        compile_workspace(repository, snapshot=lock, snapshot_directory=directory)
    return compile_workspace(
        repository,
        snapshot=lock,
        snapshot_directory=directory,
        allow_incomplete_coverage=True,
    )


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
    events: list[ProgressEvent] = []

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

    runner = ExperimentRunner(
        compiled,
        lm_factory=factory,
        progress_sink=events.append,
    )
    garbage = runner.coordinator.artifacts.put_blob(
        BytesIO(b"abandoned before the run"),
        "application/octet-stream",
    )
    garbage_path = (
        runner.coordinator.artifacts.blob_root
        / garbage.content_id.removeprefix("sha256:")
    )
    os.utime(garbage_path, (0, 0))
    run = runner.run("baseline", environment=_environment())
    assert run.status == "completed"
    assert not garbage_path.exists()
    assert run.result is not None
    assert run.result["case_count"] == 3
    assert all(item["passed"] for item in run.result["cases"])
    assert run.result["role_usage"]["solver"]["model_calls"] == 3
    assert run.result["role_usage"]["judge"]["model_calls"] == 8
    assert set(run.result["role_usage"]) == {"solver", "judge"}
    report_root = runner.coordinator.artifacts.summary_root / "runs" / run.run_id
    assert run.result["report"]["directory"] == report_root.as_posix()
    assert (report_root / "summary.md").is_file()
    assert (report_root / "results.json").is_file()
    with (report_root / "results.csv").open(encoding="utf-8", newline="") as handle:
        report_rows = list(csv.DictReader(handle))
    assert len(report_rows) == 3
    assert {row["skill_name"] for row in report_rows} == {"alpha", "beta", "gamma"}
    assert all(json.loads(row["scores_detail"])["criteria"] for row in report_rows)
    assert all(item["state"] == "persisted" for item in run.packets)
    solve_starts = [
        event
        for event in events
        if event.kind == "packet_started" and event.stage == "solve"
    ]
    assert [event.case_id for event in solve_starts] == [
        case.id for case in compiled.case_bank.cases
    ]
    assert [(event.position, event.total) for event in solve_starts] == [
        (1, 3),
        (2, 3),
        (3, 3),
    ]
    judge_completions = [
        event
        for event in events
        if event.kind == "packet_completed" and event.stage == "judge"
    ]
    assert len(judge_completions) == 3
    assert all(event.details["score"] == 1.0 for event in judge_completions)
    assert any(event.kind == "cleanup_completed" for event in events)
    assert events[-1].kind == "run_status"
    assert events[-1].details["status"] == "completed"
    model_statuses = [
        item
        for item in runner.coordinator.ledger.list(run.run_id)
        if item.packet.stage
        in {StageKind.CALIBRATE_JUDGE, StageKind.SOLVE, StageKind.JUDGE}
    ]
    for status in model_statuses:
        assert status.result is not None
        for output in status.result.outputs:
            runner.coordinator.artifacts.reference(output.content_id, output.media_type)
        call_ids = status.result.provenance["model_call_ids"]
        traces = status.result.provenance["trace_artifacts"]
        assert call_ids
        assert traces
        for call_id in call_ids:
            assert (
                runner.coordinator.artifacts.manifest_root
                / "runs"
                / run.run_id
                / "model-calls"
                / f"{call_id}.json"
            ).is_file()
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


def test_second_run_reuses_validated_stage_graph_without_model_calls(
    tmp_path: Path,
) -> None:
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
    calls: Counter[str] = Counter()

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        assert api_key == "test-secret"
        del budget
        calls[model.role] += 1
        return FixedObservedLM(observer, responses[model.role], model=model.role)

    first_runner = ExperimentRunner(compiled, lm_factory=factory)
    first = first_runner.run("baseline", environment=_environment())
    assert first.status == "completed"
    calls_after_first = calls.copy()

    events: list[ProgressEvent] = []
    second_runner = ExperimentRunner(
        compiled,
        lm_factory=factory,
        progress_sink=events.append,
    )
    second = second_runner.run("baseline", environment=_environment())

    assert second.status == "completed"
    assert calls == calls_after_first
    assert second.run_id != first.run_id
    reused = [event for event in events if event.kind == "packet_reused"]
    assert Counter(event.stage for event in reused) == {
        "calibrate_judge": 1,
        "solve": 3,
        "judge": 3,
        "aggregate": 1,
    }
    model_results = [
        item.result
        for item in second_runner.coordinator.ledger.list(second.run_id)
        if item.packet.stage
        in {StageKind.CALIBRATE_JUDGE, StageKind.SOLVE, StageKind.JUDGE}
    ]
    assert all(result is not None and result.provenance["cache_hit"] for result in model_results)
    assert all(result is not None and result.usage["model_calls"] == 0 for result in model_results)


def test_failed_judge_calibration_is_not_reused(tmp_path: Path) -> None:
    compiled = _compiled(tmp_path)
    calls: Counter[str] = Counter()

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        assert api_key == "test-secret"
        del budget
        calls[model.role] += 1
        return FixedObservedLM(
            observer,
            [judge_response(1.0) for _ in range(5)],
            model=model.role,
        )

    first = ExperimentRunner(compiled, lm_factory=factory).run(
        "baseline", environment=_environment()
    )
    events: list[ProgressEvent] = []
    second = ExperimentRunner(
        compiled,
        lm_factory=factory,
        progress_sink=events.append,
    ).run("baseline", environment=_environment())

    assert first.status == second.status == "calibration_failed"
    assert calls["judge"] == 2
    assert not any(
        event.kind == "packet_reused" and event.stage == "calibrate_judge"
        for event in events
    )


def test_rubric_change_reuses_solver_work_and_invalidates_judge_view(
    tmp_path: Path,
) -> None:
    compiled = _compiled(tmp_path)
    responses = {
        "solver": [solver_response()],
        "judge": [
            judge_response(1.0),
            judge_response(0.5),
            judge_response(0.0),
            judge_response(0.0),
            judge_response(0.0),
            judge_response(1.0),
        ],
    }
    calls: Counter[str] = Counter()

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        assert api_key == "test-secret"
        del budget
        calls[model.role] += 1
        return FixedObservedLM(observer, responses[model.role], model=model.role)

    first = ExperimentRunner(compiled, lm_factory=factory).run(
        "baseline", environment=_environment()
    )
    assert first.status == "completed"
    calls_after_first = calls.copy()

    case_directory = compiled.repository.cases_root / "alpha" / "case-train"
    rubric_file = case_directory / "judge" / "rubric.yaml"
    rubric = yaml.safe_load(rubric_file.read_text(encoding="utf-8"))
    rubric["criteria"][0]["description"] = "The response is precisely correct."
    rubric_file.write_text(
        yaml.safe_dump(rubric, sort_keys=False),
        encoding="utf-8",
    )
    case_file = case_directory / "case.yaml"
    case_payload = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    case_payload["provenance"]["case_content_hash"] = "pending"
    case_file.write_text(
        yaml.safe_dump(case_payload, sort_keys=False),
        encoding="utf-8",
    )
    case_payload["provenance"]["case_content_hash"] = CaseDefinition.load(
        case_directory,
        verify_provenance=False,
    ).content_hash
    case_file.write_text(
        yaml.safe_dump(case_payload, sort_keys=False),
        encoding="utf-8",
    )
    changed = compile_workspace(
        compiled.repository,
        snapshot=compiled.snapshot,
        snapshot_directory=compiled.snapshot_directory,
    )
    events: list[ProgressEvent] = []
    second = ExperimentRunner(
        changed,
        lm_factory=factory,
        progress_sink=events.append,
    ).run("baseline", environment=_environment())

    assert second.status == "completed"
    assert calls["solver"] == calls_after_first["solver"]
    assert calls["judge"] == calls_after_first["judge"] + 2
    reused = Counter(
        event.stage for event in events if event.kind == "packet_reused"
    )
    assert reused["solve"] == 3
    assert reused["judge"] == 2


def test_resume_rehydrates_the_run_revision_after_workspace_changes(
    tmp_path: Path,
) -> None:
    repository = create_workspace(tmp_path / "workspace")
    lock, directory = create_snapshot(
        repository,
        create_checkout(tmp_path),
        repository.data_root,
    )
    seed_cases_and_calibration(
        repository,
        authoring_instructions_hash=sha256_file(repository.case_authoring_path),
        source_snapshot_hash=lock.content_hash,
    )
    compiled = compile_workspace(
        repository,
        snapshot=lock,
        snapshot_directory=directory,
    )
    responses = {
        "solver": [solver_response()],
        "judge": [
            judge_response(1.0),
            judge_response(0.5),
            judge_response(0.0),
            judge_response(0.0),
            judge_response(0.0),
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

    original = ExperimentRunner(compiled, lm_factory=factory)
    paused = original.run(
        "baseline",
        environment=_environment(),
        through="solve",
    )
    assert paused.status == "paused"
    run_manifest = original.coordinator.ledger.load_run(paused.run_id)
    assert run_manifest["schema_version"] == 4
    assert "experiment_definition_artifact" in run_manifest
    original_revision = run_manifest["experiment_revision_id"]

    workspace_payload = yaml.safe_load(
        repository.workspace_file.read_text(encoding="utf-8")
    )
    workspace_payload["models"]["judge"]["deployment_revision"] = "judge-test-v2"
    repository.workspace_file.write_text(
        yaml.safe_dump(workspace_payload, sort_keys=False),
        encoding="utf-8",
    )
    changed_repository = WorkspaceRepository.from_file(repository.workspace_file)
    changed = compile_workspace(
        changed_repository,
        snapshot=lock,
        snapshot_directory=directory,
    )
    assert changed.experiment_lock("baseline").content_hash != original_revision

    resumed = ExperimentRunner(changed, lm_factory=factory).resume(
        paused.run_id,
        environment=_environment(),
    )
    assert resumed.status == "completed"
    assert resumed.result is not None
    assert all(
        status.packet.experiment_revision_id == original_revision
        for status in original.coordinator.ledger.list(paused.run_id)
    )
    assert (
        original.coordinator.ledger.load_run(paused.run_id)["experiment_revision_id"]
        == original_revision
    )
    rebuilt = StageCoordinator(changed_repository).build_report(
        paused.run_id,
        replace=True,
    )
    rebuilt_payload = json.loads(rebuilt.json.read_text(encoding="utf-8"))
    assert rebuilt_payload["identity"]["experiment_lock_hash"] == original_revision


def test_pilot_runs_only_train_and_validation_without_benchmark_metrics(
    tmp_path: Path,
) -> None:
    compiled = _pilot_compiled(tmp_path)
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
    run = runner.run("baseline", environment=_environment(), pilot=True)
    assert run.status == "pilot_completed"
    assert run.execution_mode == "pilot"
    assert run.benchmark_valid is False
    assert run.coverage_status == "incomplete"
    assert run.result is not None
    assert run.result["diagnostic_only"] is True
    assert run.result["benchmark_metrics"] is None
    assert "mean_score" not in run.result
    assert "pass_rate" not in run.result
    assert run.result["case_count"] == 2
    assert run.result["evaluated_splits"] == ["train", "validation"]
    report_root = runner.coordinator.artifacts.summary_root / "runs" / run.run_id
    report_payload = json.loads((report_root / "results.json").read_text(encoding="utf-8"))
    assert report_payload["benchmark_valid"] is False
    assert report_payload["benchmark_metrics"] is None
    summary = (report_root / "summary.md").read_text(encoding="utf-8")
    assert "not a valid benchmark" in summary
    assert "## Benchmark metrics" not in summary

    solve_splits = {
        runner.coordinator._input(item.packet, "case")["split"]
        for item in runner.coordinator.ledger.list(run.run_id)
        if item.packet.stage is StageKind.SOLVE
    }
    assert solve_splits == {"train", "validation"}
    stored = runner.coordinator.ledger.load_run(run.run_id)
    assert stored["execution_mode"] == "pilot"
    assert stored["benchmark_valid"] is False
    assert stored["selected_splits"] == ["train", "validation"]
    assert stored["judge_repetitions"] == 1


def test_pilot_rejects_optimization_experiment(tmp_path: Path) -> None:
    compiled = _pilot_compiled(tmp_path)
    runner = ExperimentRunner(compiled)
    with pytest.raises(PreflightError, match="evaluate experiments only"):
        runner.run("optimize", environment=_environment(), pilot=True)


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
