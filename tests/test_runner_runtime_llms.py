from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Mapping
from io import BytesIO
import json
import os
from pathlib import Path
from threading import Lock
import time

import pytest
import yaml

from ms_agent_eval.core.errors import PreflightError
from ms_agent_eval.core.coordinator import StageCoordinator
from ms_agent_eval.core.evaluation import CaseDefinition, deterministic_group_split
from ms_agent_eval.core.hashing import content_hash, sha256_file
from ms_agent_eval.core.planning import compile_workspace
from ms_agent_eval.core.progress import ProgressEvent
from ms_agent_eval.core.providers import ModelCallObserver
from ms_agent_eval.core.runner import ExperimentRunner
from ms_agent_eval.core.stages import StageKind, StageState
from ms_agent_eval.core.token_preflight import TokenPreflightResult
from ms_agent_eval.core.workspace import ResolvedRoleModel, WorkspaceRepository
from ms_agent_eval.programs.dspy.budget import BudgetLedger

from .helpers import (
    FixedObservedLM,
    create_case,
    create_checkout,
    create_snapshot,
    create_workspace,
    judge_response,
    seed_cases_and_judge_validation,
    solver_response,
)


def _environment() -> Mapping[str, str]:
    return {"TEST_API_KEY": "test-secret"}


def _compiled(tmp_path: Path):  # type: ignore[no-untyped-def]
    repository = create_workspace(tmp_path / "workspace")
    lock, directory = create_snapshot(repository, create_checkout(tmp_path), tmp_path / "external")
    seed_cases_and_judge_validation(
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
    seed_cases_and_judge_validation(
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


def _pooled_compiled(tmp_path: Path):  # type: ignore[no-untyped-def]
    repository = create_workspace(tmp_path / "workspace")
    payload = yaml.safe_load(repository.workspace_file.read_text(encoding="utf-8"))
    payload["executors"]["remote-models"]["endpoints"] = [
        {
            "id": "primary",
            "base_url": "http://localhost:8000/v1",
            "max_concurrent_requests": 2,
            "api_key_env": "TEST_API_KEY",
        },
        {
            "id": "secondary",
            "base_url": "http://localhost:8001/v1",
            "max_concurrent_requests": 1,
            "api_key_env": "TEST_API_KEY",
        },
    ]
    payload["evaluation"]["judge"]["repetitions"] = 3
    payload["stages"]["validate_judge"]["budget"]["model_calls"] = 20
    repository.workspace_file.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    repository = WorkspaceRepository.from_file(repository.workspace_file)
    lock, directory = create_snapshot(
        repository, create_checkout(tmp_path), tmp_path / "external"
    )
    authoring_hash = sha256_file(repository.case_authoring_path)
    seed_cases_and_judge_validation(
        repository,
        authoring_instructions_hash=authoring_hash,
        source_snapshot_hash=lock.content_hash,
    )
    split_file = repository.cases_root / "splits.yaml"
    splits = yaml.safe_load(split_file.read_text(encoding="utf-8"))
    index = 2
    created = 0
    while created < 4:
        group = f"group-pool-{index}"
        index += 1
        if deterministic_group_split(
            group, seed="ms-agent-eval", train=0.7, test=0.2
        ) != "test":
            continue
        created += 1
        create_case(
            repository.cases_root,
            case_id=f"case-test-{created + 1}",
            skill="gamma",
            group=group,
            source_path="skills/gamma/SKILL.md",
            authoring_instructions_hash=authoring_hash,
            source_snapshot_hash=lock.content_hash,
        )
        splits["groups"][group] = "test"
    split_file.write_text(yaml.safe_dump(splits, sort_keys=False), encoding="utf-8")
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
    events: list[ProgressEvent] = []

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        assert api_key == "test-secret"
        assert model.base_url == "http://localhost:8000/v1"
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
    assert run.result["case_count"] == 1
    assert all("passed" not in item for item in run.result["cases"])
    assert run.result["role_usage"]["solver"]["model_calls"] == 1
    assert run.result["role_usage"]["judge"]["model_calls"] == 6
    assert set(run.result["role_usage"]) == {"solver", "judge"}
    endpoint_execution = run.result["endpoint_execution"]
    assert endpoint_execution["configured_capacity"] == 1
    assert endpoint_execution["endpoints"][0]["endpoint_id"] == "primary"
    assert endpoint_execution["endpoints"][0]["model_calls"] == 7
    report_root = runner.coordinator.artifacts.summary_root / "runs" / run.run_id
    assert run.result["report"]["directory"] == report_root.as_posix()
    assert (report_root / "summary.md").is_file()
    assert "## Endpoint execution" in (report_root / "summary.md").read_text(
        encoding="utf-8"
    )
    assert (report_root / "results.json").is_file()
    with (report_root / "results.csv").open(encoding="utf-8", newline="") as handle:
        report_rows = list(csv.DictReader(handle))
    assert len(report_rows) == 1
    assert {row["skill_name"] for row in report_rows} == {"gamma"}
    assert all(json.loads(row["scores_detail"])["criteria"] for row in report_rows)
    assert all(item["state"] == "persisted" for item in run.packets)
    solve_starts = [
        event
        for event in events
        if event.kind == "packet_started" and event.stage == "solve"
    ]
    assert [event.case_id for event in solve_starts] == ["case-test"]
    assert [(event.position, event.total) for event in solve_starts] == [(1, 1)]
    judge_completions = [
        event
        for event in events
        if event.kind == "packet_completed" and event.stage == "judge"
    ]
    assert len(judge_completions) == 1
    assert all(event.details["score"] == 1.0 for event in judge_completions)
    assert any(event.kind == "cleanup_completed" for event in events)
    assert events[-1].kind == "run_status"
    assert events[-1].details["status"] == "completed"
    model_statuses = [
        item
        for item in runner.coordinator.ledger.list(run.run_id)
        if item.packet.stage
        in {StageKind.VALIDATE_JUDGE, StageKind.SOLVE, StageKind.JUDGE}
    ]
    for status in model_statuses:
        assert status.result is not None
        endpoint = status.result.provenance["endpoint_pool"]
        assert endpoint["endpoint_id"] == "primary"
        assert endpoint["max_concurrent_requests"] == 1
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


def test_endpoint_pool_routes_a_real_run_with_bounded_parallelism(tmp_path: Path) -> None:
    compiled = _pooled_compiled(tmp_path)
    responses = {
        "solver": [solver_response() for _ in range(20)],
        "judge": [
            judge_response(1.0),
            judge_response(0.5),
            judge_response(0.0),
            judge_response(0.0),
            judge_response(0.0),
            *[judge_response(1.0) for _ in range(20)],
        ],
    }
    state_lock = Lock()
    active: Counter[str] = Counter()
    maximum: Counter[str] = Counter()
    calls: Counter[str] = Counter()

    class SlowObservedLM(FixedObservedLM):
        def __init__(self, *args, endpoint: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
            super().__init__(*args, **kwargs)
            self.endpoint = endpoint

        def forward(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            rendered = json.dumps(
                kwargs.get("messages") or kwargs.get("prompt") or args,
                ensure_ascii=False,
            )
            validation_scores = {
                "strong candidate": 1.0,
                "partial candidate": 0.5,
                "incorrect candidate": 0.0,
                "contradictory candidate": 0.0,
                "adversarial candidate": 0.0,
            }
            for marker, score in validation_scores.items():
                if marker in rendered:
                    self.responses = [judge_response(score)]
                    break
            is_validation = any(marker in rendered for marker in validation_scores)
            with state_lock:
                active[self.endpoint] += 1
                active["total"] += 1
                if is_validation:
                    active["validation"] += 1
                calls[self.endpoint] += 1
                maximum[self.endpoint] = max(
                    maximum[self.endpoint], active[self.endpoint]
                )
                maximum["total"] = max(maximum["total"], active["total"])
                if is_validation:
                    maximum["validation"] = max(
                        maximum["validation"], active["validation"]
                    )
            try:
                time.sleep(0.03)
                return super().forward(*args, **kwargs)
            finally:
                with state_lock:
                    active[self.endpoint] -= 1
                    active["total"] -= 1
                    if is_validation:
                        active["validation"] -= 1

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> SlowObservedLM:
        assert api_key == "test-secret"
        assert budget is not None
        assert budget.limits.concurrency == 3
        return SlowObservedLM(
            observer,
            responses[model.role],
            model=model.role,
            endpoint=model.base_url,
        )

    events: list[ProgressEvent] = []
    runner = ExperimentRunner(
        compiled,
        lm_factory=factory,
        progress_sink=events.append,
    )
    run = runner.run("baseline", environment=_environment())

    assert run.status == "completed"
    assert maximum["http://localhost:8000/v1"] <= 2
    assert maximum["http://localhost:8001/v1"] <= 1
    assert maximum["total"] == 3
    assert maximum["validation"] == 3
    assert calls["http://localhost:8000/v1"] > 0
    assert calls["http://localhost:8001/v1"] > 0
    validation = next(
        status
        for status in runner.coordinator.ledger.list(run.run_id)
        if status.packet.stage is StageKind.VALIDATE_JUDGE
    )
    assert validation.result is not None
    validation_pool = validation.result.provenance["endpoint_pool"]
    assert validation_pool["total_concurrency"] == 3
    assert validation_pool["requests"] == 15
    assert sum(route["model_calls"] for route in validation_pool["routes"]) == 15
    assert {route["endpoint_id"] for route in validation_pool["routes"]} == {
        "primary",
        "secondary",
    }
    vote_events = [
        event
        for event in events
        if event.kind == "judge_validation_vote_completed"
    ]
    assert len(vote_events) == 15
    assert sorted(event.position for event in vote_events) == list(range(1, 16))
    solve_endpoints = {
        status.result.provenance["endpoint_pool"]["endpoint_id"]
        for status in runner.coordinator.ledger.list(run.run_id)
        if status.packet.stage is StageKind.SOLVE and status.result is not None
    }
    assert solve_endpoints == {"primary", "secondary"}
    assert run.result is not None
    assert run.result["endpoint_execution"]["configured_capacity"] == 3


def test_context_preflight_reroutes_without_calling_a_known_small_endpoint(
    tmp_path: Path,
) -> None:
    compiled = _pooled_compiled(tmp_path)
    preflight_calls: Counter[str] = Counter()
    model_endpoints: Counter[str] = Counter()

    class SelectivePreflight:
        def evaluate(self, endpoint, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            preflight_calls[endpoint.id] += 1
            fits = endpoint.id == "secondary"
            return TokenPreflightResult(
                status="fits" if fits else "exceeds",
                reason="test_exact_tokenization",
                context_window=16 if fits else 8,
                input_tokens=7,
                maximum_output_tokens=2,
                required_tokens=9,
                tokenizer_fingerprint="sha256:test-tokenizer",
                chat_template_fingerprint="sha256:test-template",
                capability_source="props",
                capability_checked_at="2026-09-13T00:00:00+00:00",
                rendered_message_hash="sha256:test-request",
            )

    class DynamicObservedLM(FixedObservedLM):
        def forward(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            rendered = json.dumps(
                kwargs.get("messages") or kwargs.get("prompt") or args,
                ensure_ascii=False,
            )
            if self.model == "solver":
                response = solver_response()
            else:
                score = 1.0
                for marker, value in {
                    "strong candidate": 1.0,
                    "partial candidate": 0.5,
                    "incorrect candidate": 0.0,
                    "contradictory candidate": 0.0,
                    "adversarial candidate": 0.0,
                }.items():
                    if marker in rendered:
                        score = value
                        break
                response = judge_response(score)
            self.responses = [response]
            return super().forward(*args, **kwargs)

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> DynamicObservedLM:
        del budget
        assert api_key == "test-secret"
        assert model.base_url == "http://localhost:8001/v1"
        model_endpoints[model.base_url] += 1
        return DynamicObservedLM(observer, [], model=model.role)

    run = ExperimentRunner(
        compiled,
        lm_factory=factory,
        token_preflight=SelectivePreflight(),  # type: ignore[arg-type]
    ).run("baseline", environment=_environment())

    assert run.status == "completed"
    assert preflight_calls["primary"] > 0
    assert preflight_calls["secondary"] > 0
    assert model_endpoints == Counter({"http://localhost:8001/v1": sum(model_endpoints.values())})
    assert run.result is not None
    endpoints = {
        item["endpoint_id"]: item
        for item in run.result["endpoint_execution"]["endpoints"]
    }
    assert endpoints["primary"]["model_calls"] == 0
    assert endpoints["primary"]["preflight_rejections"] > 0
    assert endpoints["secondary"]["model_calls"] > 0


def test_context_preflight_failure_makes_no_model_request(tmp_path: Path) -> None:
    compiled = _compiled(tmp_path)
    model_calls = 0

    class RejectingPreflight:
        def evaluate(self, endpoint, **kwargs):  # type: ignore[no-untyped-def]
            del endpoint, kwargs
            return TokenPreflightResult(
                status="exceeds",
                reason="test_exact_tokenization",
                context_window=8,
                input_tokens=7,
                maximum_output_tokens=2,
                required_tokens=9,
                tokenizer_fingerprint="sha256:test-tokenizer",
                chat_template_fingerprint="sha256:test-template",
                capability_source="props",
                capability_checked_at="2026-09-13T00:00:00+00:00",
                rendered_message_hash="sha256:test-request",
            )

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        nonlocal model_calls
        del model, observer, budget, api_key
        model_calls += 1
        raise AssertionError("known-oversized requests must not reach the model")

    run = ExperimentRunner(
        compiled,
        lm_factory=factory,
        token_preflight=RejectingPreflight(),  # type: ignore[arg-type]
    ).run("baseline", environment=_environment())

    assert run.status == "execution_failed"
    assert run.result is not None
    assert run.result["error_kind"] == "context_window_exceeded"
    assert model_calls == 0
    validation = next(
        item
        for item in run.packets
        if item["stage"] == "validate_judge"
    )
    assert validation["state"] == "failed"


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
        "validate_judge": 1,
        "solve": 1,
        "judge": 1,
        "aggregate": 1,
    }
    model_results = [
        item.result
        for item in second_runner.coordinator.ledger.list(second.run_id)
        if item.packet.stage
        in {StageKind.VALIDATE_JUDGE, StageKind.SOLVE, StageKind.JUDGE}
    ]
    assert all(result is not None and result.provenance["cache_hit"] for result in model_results)
    assert all(result is not None and result.usage["model_calls"] == 0 for result in model_results)


def test_failed_judge_validation_is_not_reused(tmp_path: Path) -> None:
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

    assert first.status == second.status == "judge_validation_failed"
    assert calls["judge"] == 2
    assert not any(
        event.kind == "packet_reused" and event.stage == "validate_judge"
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
    assert reused["solve"] == 1
    assert reused["judge"] == 0


def test_resume_rehydrates_the_run_revision_after_workspace_changes(
    tmp_path: Path,
) -> None:
    repository = create_workspace(tmp_path / "workspace")
    lock, directory = create_snapshot(
        repository,
        create_checkout(tmp_path),
        repository.data_root,
    )
    seed_cases_and_judge_validation(
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


def test_pilot_runs_only_train_without_benchmark_metrics(
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
    assert run.result["evaluated_splits"] == ["train"]
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
    assert solve_splits == {"train"}
    stored = runner.coordinator.ledger.load_run(run.run_id)
    assert stored["execution_mode"] == "pilot"
    assert stored["benchmark_valid"] is False
    assert stored["selected_splits"] == ["train"]
    assert stored["judge_repetitions"] == 1


def test_pilot_rejects_optimization_experiment(tmp_path: Path) -> None:
    compiled = _pilot_compiled(tmp_path)
    runner = ExperimentRunner(compiled)
    with pytest.raises(PreflightError, match="evaluate experiments only"):
        runner.run("optimize", environment=_environment(), pilot=True)


def test_cross_validation_selects_then_refits_before_held_out_test(tmp_path: Path) -> None:
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
    selection = next(
        item
        for item in runner.coordinator.ledger.list(run.run_id)
        if item.packet.stage is StageKind.SELECT and item.state is StageState.PERSISTED
    )
    assert selection.result is not None
    selected = runner.coordinator.ledger.read_output(selection.result)
    assert selected["metric"] == "mean_score"
    assert selected["selected_candidate_id"] == "baseline"
    assert "threshold" not in selected
    final_fit = next(
        item
        for item in runner.coordinator.ledger.list(run.run_id)
        if item.packet.stage is StageKind.FIT
        and item.state is StageState.PERSISTED
        and item.result is not None
        and runner.coordinator.ledger.read_output(item.result)["phase"] == "final_fit"
    )
    assert final_fit.result is not None
    fitted = runner.coordinator.ledger.read_output(final_fit.result)
    assert fitted["state_format"] == "json"
    test_solve = next(
        item.packet
        for item in runner.coordinator.ledger.list(run.run_id)
        if item.packet.stage is StageKind.SOLVE
        and runner.coordinator._input(item.packet, "case")["evaluation_phase"] == "test"
    )
    assert final_fit.packet.packet_id in test_solve.dependencies
    cv_cases = {
        runner.coordinator._input(item.packet, "case")["case_id"]
        for item in runner.coordinator.ledger.list(run.run_id)
        if item.packet.stage is StageKind.SOLVE
        and runner.coordinator._input(item.packet, "case")["evaluation_phase"]
        == "cross_validation"
    }
    assert cv_cases == {"case-train", "case-train-two"}
    report_root = runner.coordinator.artifacts.summary_root / "runs" / run.run_id
    report = json.loads((report_root / "results.json").read_text(encoding="utf-8"))
    assert report["cross_validation"]["selected_candidate_id"] == "baseline"
    assert report["cross_validation"]["metric"] == "mean_score"
    assert "pass_rate" not in report.get("benchmark_metrics", {})


def test_optimization_rejects_judge_validation_on_held_out_case_before_lm_creation(
    tmp_path: Path,
) -> None:
    compiled = _compiled(tmp_path)
    manifest = compiled.repository.judge_validation_root / "manifest.yaml"
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
    assert run.status == "execution_failed"
    assert run.result is not None
    assert run.result["error_kind"] == "runtimeerror"
    assert run.result["error"] == "endpoint rejected credential [REDACTED]"
    judge_validation = next(
        item
        for item in runner.coordinator.ledger.list(run.run_id)
        if item.packet.stage is StageKind.VALIDATE_JUDGE
    )
    assert judge_validation.state is StageState.FAILED
    assert judge_validation.result is not None
    assert judge_validation.result.error is not None
    assert judge_validation.result.error.message == "endpoint rejected credential [REDACTED]"
    evidence = "".join(
        path.read_text(encoding="utf-8")
        for path in compiled.repository.data_root.rglob("*.json")
    )
    assert "test-secret" not in evidence


def test_dspy_connection_failure_is_actionable(tmp_path: Path) -> None:
    compiled = _compiled(tmp_path)

    class ConnectionFailureLM(FixedObservedLM):
        def forward(self, prompt=None, messages=None, **kwargs):  # type: ignore[no-untyped-def]
            del prompt, messages, kwargs
            raise RuntimeError("OpenAIException - Connection error")

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        del budget, api_key
        return ConnectionFailureLM(observer, [], model=model.role)

    runner = ExperimentRunner(compiled, lm_factory=factory)
    run = runner.run("baseline", environment=_environment())

    assert run.status == "execution_failed"
    assert run.result is not None
    assert run.result["error_kind"] == "connection_error"
    assert run.result["error"] == (
        "judge DSPy call failed (connection_error): OpenAIException - Connection error"
    )


def test_remote_solver_can_pause_before_a_local_harness_judge(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    repository = create_workspace(workspace_root)
    payload = yaml.safe_load(repository.workspace_file.read_text(encoding="utf-8"))
    payload["stages"]["validate_judge"]["executor"] = "authoring-harness"
    payload["stages"]["judge"]["executor"] = "authoring-harness"
    repository.workspace_file.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    repository = WorkspaceRepository.from_file(repository.workspace_file)
    lock, directory = create_snapshot(
        repository, create_checkout(tmp_path), tmp_path / "external"
    )
    seed_cases_and_judge_validation(
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
    judge_validation = next(
        item.packet
        for item in runner.coordinator.ledger.list(waiting.run_id)
        if item.packet.stage is StageKind.VALIDATE_JUDGE
    )
    validation_input = runner.coordinator._input(judge_validation, "judge_validation")
    assert all(
        "score_range" not in fixture and "label" not in fixture
        for fixture in validation_input["fixtures"]
    )
    scores = {
        fixture.id: {
            "strong": 1.0,
            "partial": 0.5,
            "incorrect": 0.0,
            "contradictory": 0.0,
            "adversarial": 0.0,
        }[fixture.label]
        for fixture in compiled.judge_validation.fixtures
    }
    identity = {
        "ordering_consistent": True,
        "fixture_scores": scores,
        "corpus_hash": validation_input["corpus_hash"],
    }
    result_file = tmp_path / "judge-validation-result.json"
    result_file.write_text(
        json.dumps({**identity, "content_hash": content_hash(identity)}), encoding="utf-8"
    )
    claim = runner.coordinator.ledger.claim(
        judge_validation.packet_id,
        executor_id="authoring-harness",
        worker_id="local-codex",
    )
    runner.coordinator.submit_file(
        judge_validation.packet_id,
        result_file,
        executor_id="authoring-harness",
        worker_id="local-codex",
        lease_id=claim.lease_id,
    )

    solved = runner.resume(waiting.run_id, environment=_environment(), through="solve")
    assert solved.status == "paused"
    assert solver_calls == 1
    judging = runner.resume(waiting.run_id, environment=_environment())
    assert judging.status == "awaiting_execution"
    assert judging.current_stage == "judge"
    assert solver_calls == 1
