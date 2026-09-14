from __future__ import annotations

import json
from copy import deepcopy
from collections import Counter
from pathlib import Path

import pytest
import yaml

from ms_agent_eval.adapters.mlflow import MlflowRecorder
from ms_agent_eval.core.coordinator import StageCoordinator
from ms_agent_eval.core.lifecycle import RunLifecycleLedger, RunLifecycleState
from ms_agent_eval.core.hashing import sha256_file
from ms_agent_eval.core.planning import compile_workspace
from ms_agent_eval.core.providers import ModelCallObserver
from ms_agent_eval.core.recorder import (
    ExternalBinding,
    RecorderCapabilities,
    RecorderService,
    RecordingPlan,
    load_recording_plan_event,
)
from ms_agent_eval.core.recording_projection import (
    RecorderProjector,
    _chart_manifest,
    _dag_svg,
    _token_usage_rows,
)
from ms_agent_eval.core.stages import StageLedger
from ms_agent_eval.core.stages import (
    StageBudget,
    StageExecutorIdentity,
    StageKind,
    StageModelIdentity,
    StagePacket,
    StageResult,
)
from ms_agent_eval.core.storage import FilesystemArtifactStore
from ms_agent_eval.core.workspace import (
    RecorderAuthenticationConfiguration,
    RecorderConfiguration,
    WorkspaceConfiguration,
    ResolvedRoleModel,
    WorkspaceRepository,
)
from ms_agent_eval.programs.dspy.budget import BudgetLedger

from .helpers import (
    FixedObservedLM,
    create_checkout,
    create_snapshot,
    create_workspace,
    judge_response,
    seed_cases_and_judge_validation,
    solver_response,
    workspace_payload,
)


class FakeRecorder:
    def __init__(self) -> None:
        self.experiments = 0
        self.runs = 0
        self.plans: list[RecordingPlan] = []
        self.finalized: list[RunLifecycleState] = []
        self.failures_remaining = 0

    @property
    def capabilities(self) -> RecorderCapabilities:
        return RecorderCapabilities(True, True, True, True, True, False)

    def healthcheck(self, *, write_test: bool = False):  # type: ignore[no-untyped-def]
        return {"reachable": True, "write_test": write_test}

    def ensure_experiment(self, descriptor, binding: ExternalBinding) -> str:  # type: ignore[no-untyped-def]
        del descriptor, binding
        self.experiments += 1
        return "experiment-1"

    def ensure_run(self, descriptor, binding: ExternalBinding) -> str:  # type: ignore[no-untyped-def]
        del descriptor, binding
        self.runs += 1
        return "run-remote-1"

    def sync(self, plan: RecordingPlan, binding: ExternalBinding):  # type: ignore[no-untyped-def]
        del binding
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("temporary recorder outage with TOP_SECRET_TOKEN")
        self.plans.append(plan)
        return {"plan_id": plan.plan_id}

    def finalize(
        self,
        binding: ExternalBinding,
        status: RunLifecycleState,
        end_time_ms: int,
    ):
        del binding, end_time_ms
        self.finalized.append(status)
        return {"status": status.value}


def _run(store: FilesystemArtifactStore, run_id: str = "run-1") -> None:
    ledger = StageLedger(store)
    lock = store.put_manifest("fixtures/experiment-lock", {"kind": "lock"})
    bundle = store.put_manifest("fixtures/experiment-bundle", {"kind": "bundle"})
    definition = store.put_manifest("fixtures/experiment-definition", {"kind": "definition"})
    ledger.create_run(
        run_id,
        {
            "schema_version": 4,
            "run_id": run_id,
            "workspace_id": "synthetic-evaluation",
            "workspace_hash": f"sha256:{'1' * 64}",
            "experiment_id": "baseline",
            "experiment_revision_id": f"sha256:{'2' * 64}",
            "execution_mode": "benchmark",
            "benchmark_valid": True,
            "coverage_status": "complete",
            "judge_repetitions": 1,
            "experiment_lock_artifact": lock.__dict__,
            "experiment_bundle_artifact": bundle.__dict__,
            "experiment_definition_artifact": definition.__dict__,
            "created_at": "2026-09-14T10:00:00+00:00",
        },
    )


def _configuration() -> WorkspaceConfiguration:
    return WorkspaceConfiguration.from_mapping(workspace_payload())


def test_lifecycle_is_append_only_and_reconstructs(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "external")
    _run(store)
    ledger = RunLifecycleLedger(store, timestamp=lambda: "2026-09-14T10:00:01+00:00")

    created = ledger.append("run-1", RunLifecycleState.CREATED, reason="run_created")
    running = ledger.append("run-1", RunLifecycleState.RUNNING, reason="run_started")
    duplicate = ledger.append(
        "run-1",
        RunLifecycleState.RUNNING,
        reason="run_started",
        if_changed=True,
    )

    assert created.sequence == 1
    assert running.sequence == 2
    assert duplicate.event_id == running.event_id
    assert RunLifecycleLedger(store).current("run-1") == running


def test_projection_is_deterministic_and_round_trips(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "external")
    _run(store)
    lifecycle = RunLifecycleLedger(store)
    lifecycle.append("run-1", RunLifecycleState.CREATED, reason="run_created")
    lifecycle.append("run-1", RunLifecycleState.RUNNING, reason="run_started")
    ledger = StageLedger(store)
    packet = StagePacket.create(
        run_id="run-1",
        experiment_id="baseline",
        experiment_revision_id=f"sha256:{'2' * 64}",
        stage=StageKind.SOLVE,
        attempt=1,
        dependencies=(),
        inputs=(),
        instructions=ledger.put_json("instructions", {"policy": "be correct"}),
        output_schema="solver-response-v1",
        executor=StageExecutorIdentity("remote-models", "openai_compatible", f"sha256:{'3' * 64}"),
        model=StageModelIdentity("solver", f"sha256:{'4' * 64}"),
        budget=StageBudget(1, 1000, 60),
    )
    ledger.prepare(packet)
    configuration = _configuration()
    projector = RecorderProjector(
        store,
        recorder=configuration.recorders[configuration.recording.recorder],
        recording=configuration.recording,
    )

    first = projector.build("run-1")
    second = projector.build("run-1")

    assert first.plan_id == second.plan_id
    assert first.as_dict() == second.as_dict()
    # Native MLflow tables remain terminal-only to avoid uploading a new table
    # artifact on every live checkpoint, but the local/browser table view must
    # expose the evidence already available during a running experiment.
    assert first.tables == ()
    assert {item.path for item in first.artifacts} >= {
        "evidence/artifact-index.json",
        "graphs/execution-dag.json",
        "graphs/execution-dag.svg",
        "graphs/index.html",
        "graphs/manifest.json",
        "identity/recorder-manifest.json",
        "identity/run.json",
    }
    table_artifact = next(item for item in first.artifacts if item.path == "tables/index.html")
    with store.get_blob(table_artifact.reference) as handle:
        table_index = handle.read().decode("utf-8")
    assert '"name":"stage-attempts"' in table_index
    assert (store.summary_root / "runs" / "run-1" / "tables" / "index.html").read_text(
        encoding="utf-8"
    ) == table_index


def test_execution_dag_svg_renders_dependencies_and_accessible_description() -> None:
    dag = {
        "overview": {
            "nodes": [
                {
                    "id": "scope:solve.test",
                    "scope": "solve.test",
                    "stage": "solve",
                    "attempts": 102,
                    "states": {"persisted": 102},
                    "executed": 100,
                    "reused": 2,
                },
                {
                    "id": "scope:judge.test",
                    "scope": "judge.test",
                    "stage": "judge",
                    "attempts": 102,
                    "states": {"persisted": 100, "claimed": 2},
                    "executed": 102,
                    "reused": 0,
                },
            ],
            "edges": [
                {
                    "source": "scope:solve.test",
                    "target": "scope:judge.test",
                    "type": "dependency",
                    "count": 102,
                }
            ],
        }
    }

    rendered = _dag_svg(dag).decode("utf-8")

    assert '<path class="dag-edge"' in rendered
    assert 'marker-end="url(#dag-arrow)"' in rendered
    assert "102 dependency edges" in rendered
    assert '<title id="chart-title">Execution dependency overview</title>' in rendered
    assert '<desc id="chart-description">' in rendered
    assert "24516" not in rendered


def test_token_chart_rows_use_model_call_tokens_instead_of_stage_latency() -> None:
    rows = _token_usage_rows(
        (
            {
                "stage": "solve",
                "status": "completed",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 25,
                    "total_tokens": 125,
                },
            },
            {
                "stage": "solve",
                "status": "completed",
                "usage": {"total_tokens": 40},
            },
            {
                "stage": "judge",
                "status": "failed",
                "usage": {"total_tokens": 999},
            },
        )
    )

    assert rows == (
        {
            "stage": "solve",
            "input_tokens": 100,
            "output_tokens": 25,
            "unclassified_tokens": 40,
            "calls": 2,
        },
    )


def test_chart_manifest_explains_generated_and_omitted_graphs() -> None:
    manifest = _chart_manifest(
        {"graphs/endpoint-workload.svg": (b"svg", "image/svg+xml")},
        ({"artifact": "endpoint-saturation.svg", "reason": "no_observed_concurrency"},),
    )
    charts = {item["artifact"]: item for item in manifest["charts"]}

    workload = charts["graphs/endpoint-workload.svg"]
    assert workload["status"] == "generated"
    assert "not time-based endpoint utilization" in workload["description"]
    saturation = charts["graphs/endpoint-saturation.svg"]
    assert saturation["status"] == "omitted"
    assert saturation["omission_reason"] == "no_observed_concurrency"


def test_comparison_projection_groups_only_compatible_model_arms(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "external")
    group_id = f"sha256:{'a' * 64}"
    benchmark_id = f"sha256:{'b' * 64}"

    def report(
        run_id: str,
        *,
        arm: str,
        experiment: str,
        optimizer: bool,
        score: float,
        created_at: str,
    ) -> dict[str, object]:
        models: dict[str, object] = {
            "solver": {
                "profile_id": "qwen-solver",
                "deployment_id": "qwen-local",
                "configured_name": "qwen",
                "deployment_revision": "qwen-v1",
            },
            "judge": {
                "profile_id": "fixed-judge",
                "deployment_id": "qwen-local",
                "configured_name": "qwen",
                "deployment_revision": "qwen-v1",
            },
        }
        comparison = None
        optimization = None
        if optimizer:
            models["optimizer"] = {
                "profile_id": "glm-optimizer",
                "deployment_id": "glm-local",
                "configured_name": "glm",
                "deployment_revision": "glm-v1",
            }
            comparison = {
                "baseline_mean_score": 0.5,
                "optimized_mean_score": score,
                "micro_case_delta": score - 0.5,
                "macro_skill_delta": score - 0.5,
                "skills": [
                    {
                        "skill_name": "alpha",
                        "baseline_mean_score": 0.5,
                        "optimized_mean_score": score,
                        "delta": score - 0.5,
                        "case_count": 2,
                    }
                ],
                "length_bands": [
                    {
                        "expected_response_length_band": "129-256",
                        "baseline_mean_score": 0.5,
                        "optimized_mean_score": score,
                        "delta": score - 0.5,
                        "case_count": 2,
                    }
                ],
            }
            optimization = {"strategy": "copro"}
        return {
            "schema_version": 1,
            "run_id": run_id,
            "experiment_id": experiment,
            "workspace_id": "synthetic-evaluation",
            "created_at": created_at,
            "status": "completed",
            "benchmark_valid": True,
            "identity": {
                "experiment_lock_hash": f"sha256:{run_id[-1] * 64}",
                "benchmark_revision_id": benchmark_id,
                "comparison_group_id": group_id,
                "comparison_id": "model-arms",
                "arm_id": arm,
                "run_graph_id": f"sha256:{arm[-1] * 64}",
            },
            "models": models,
            "benchmark_metrics": {"mean_score": score},
            "test_comparison": comparison,
            "optimization": optimization,
            "case_count": 2,
            "skills": [{"skill_name": "alpha", "mean_score": score, "case_count": 2}],
            "cases": [
                {
                    "case_id": "case-1",
                    "score": score,
                    "expected_response_length_band": "129-256",
                }
            ],
            "usage_by_role": {
                "solver": {"tokens": 100, "latency_seconds": 2.0},
                "judge": {"tokens": 50, "latency_seconds": 1.0},
            },
            "endpoint_execution": {"endpoints": [{"requests": 2}]},
            "stages": [
                {
                    "stage": "aggregate",
                    "result_id": f"sha256:{experiment[-1] * 64}",
                }
            ],
            "content_hash": f"sha256:{run_id[-1] * 64}",
        }

    for payload in (
        report(
            "run-1",
            arm=f"sha256:{'1' * 64}",
            experiment="qwen-baseline",
            optimizer=False,
            score=0.5,
            created_at="2026-09-14T10:00:00+00:00",
        ),
        report(
            "run-2",
            arm=f"sha256:{'2' * 64}",
            experiment="qwen-copro-glm",
            optimizer=True,
            score=0.75,
            created_at="2026-09-14T11:00:00+00:00",
        ),
    ):
        root = store.summary_root / "runs" / str(payload["run_id"])
        root.mkdir(parents=True)
        (root / "results.json").write_text(json.dumps(payload), encoding="utf-8")

    configuration = _configuration()
    plan = RecorderProjector(
        store,
        recorder=configuration.recorders[configuration.recording.recorder],
        recording=configuration.recording,
    ).build_comparison("run-2")

    assert plan is not None
    assert plan.run.tags["agent_eval.run_kind"] == "model_comparison"
    assert plan.run.tags["agent_eval.comparison_group_id"] == group_id
    assert {table.name for table in plan.tables} == {
        "model-arm-comparison",
        "model-skill-comparison",
        "model-token-band-comparison",
    }
    artifact_paths = {artifact.path for artifact in plan.artifacts}
    assert {
        "index.html",
        "tables/index.html",
        "comparison/comparison-manifest.json",
        "comparison/graphs/model-baseline-score.html",
        "comparison/graphs/model-optimized-score.html",
        "comparison/graphs/optimizer-lift.html",
        "comparison/graphs/model-skill-heatmap.html",
        "comparison/graphs/model-token-band-score.html",
        "comparison/graphs/model-quality-vs-latency.html",
        "comparison/graphs/model-quality-vs-tokens.html",
    } <= artifact_paths
    manifest_descriptor = next(
        item for item in plan.artifacts if item.path == "comparison/comparison-manifest.json"
    )
    with store.get_blob(manifest_descriptor.reference) as handle:
        manifest = json.load(handle)
    assert all(item["aggregate_result_id"] for item in manifest["input_runs"])
    assert all(item["run_graph_id"] for item in manifest["input_runs"])
    graph_manifest_descriptor = next(
        item for item in plan.artifacts if item.path == "comparison/graphs/manifest.json"
    )
    with store.get_blob(graph_manifest_descriptor.reference) as handle:
        graph_manifest = json.load(handle)
    assert all(item["artifact"].endswith(".html") for item in graph_manifest["charts"])


def test_recorder_delivery_is_retryable_idempotent_and_redacts_errors(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "external")
    _run(store)
    lifecycle = RunLifecycleLedger(store)
    lifecycle.append("run-1", RunLifecycleState.CREATED, reason="run_created")
    lifecycle.append("run-1", RunLifecycleState.COMPLETED, reason="run_completed")
    configuration = _configuration()
    fake = FakeRecorder()
    fake.failures_remaining = 1
    service = RecorderService(
        store,
        configuration.recording,
        configuration.recorders[configuration.recording.recorder],
        {"RECORDER_TOKEN": "TOP_SECRET_TOKEN"},
        adapter_factory=lambda _configuration, _environment: fake,
    )

    failed = service.sync_run("run-1")
    assert failed["status"] == "failed"
    assert "TOP_SECRET_TOKEN" not in str(failed["last_error"])

    synced = service.sync_run("run-1", raise_on_error=True)
    repeated = service.sync_run("run-1", raise_on_error=True)

    assert synced["status"] == "synced"
    assert repeated["status"] == "synced"
    assert len(fake.plans) == 1
    assert fake.finalized == [
        RunLifecycleState.COMPLETED,
        RunLifecycleState.COMPLETED,
    ]
    assert synced["external_run_id"] == "run-remote-1"
    event = next((service._run_root("run-1") / "events").glob("*.json"))
    assert load_recording_plan_event(event).plan_id == fake.plans[0].plan_id


def test_completed_run_automatically_flushes_the_recorder(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    payload = yaml.safe_load(repository.workspace_file.read_text(encoding="utf-8"))
    payload["recording"]["enabled"] = True
    repository.workspace_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    repository = WorkspaceRepository.from_file(repository.workspace_file)
    snapshot, directory = create_snapshot(
        repository,
        create_checkout(tmp_path),
        tmp_path / "external",
    )
    seed_cases_and_judge_validation(
        repository,
        authoring_instructions_hash=sha256_file(repository.case_authoring_path),
        source_snapshot_hash=snapshot.content_hash,
    )
    compiled = compile_workspace(
        repository,
        snapshot=snapshot,
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
    factory_calls: Counter[str] = Counter()

    def lm_factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        del budget
        assert api_key == "test-secret"
        factory_calls[model.role] += 1
        return FixedObservedLM(observer, responses[model.role], model=model.role)

    fake = FakeRecorder()
    # The first background projection fails, but recorder availability must
    # not affect model work or the canonical run outcome. A later checkpoint
    # retries the same durable projection.
    fake.failures_remaining = 1
    from ms_agent_eval.core.runner import ExperimentRunner

    runner = ExperimentRunner(
        compiled,
        lm_factory=lm_factory,
        recorder_factory=lambda _configuration, _environment: fake,
    )

    run = runner.run("baseline", environment={"TEST_API_KEY": "test-secret"})

    assert run.status == "completed"
    assert fake.plans
    assert fake.plans[-1].run.status == RunLifecycleState.COMPLETED.value
    assert fake.finalized[-1] is RunLifecycleState.COMPLETED
    assert runner.coordinator.recorder_service.status(run.run_id)["status"] == "synced"
    states = [event.state for event in runner.coordinator.lifecycle.list(run.run_id)]
    assert RunLifecycleState.REPORT_PUBLISHED in states
    assert states[-1] is RunLifecycleState.COMPLETED
    assert factory_calls["solver"] == 1


def test_recorder_destination_does_not_change_experiment_identity() -> None:
    first_payload = workspace_payload()
    second_payload = deepcopy(first_payload)
    second_payload["recorders"]["local-mlflow"]["tracking_uri"] = "http://127.0.0.1:6000"

    first = WorkspaceConfiguration.from_mapping(first_payload)
    second = WorkspaceConfiguration.from_mapping(second_payload)

    assert first.identity_hash == second.identity_hash
    assert (
        first.recorders["local-mlflow"].configuration_hash
        != second.recorders["local-mlflow"].configuration_hash
    )


def test_recorder_destination_change_reuses_the_same_revision_bundle(
    tmp_path: Path,
) -> None:
    repository = create_workspace(tmp_path / "workspace")
    snapshot, directory = create_snapshot(
        repository,
        create_checkout(tmp_path),
        tmp_path / "external",
    )
    seed_cases_and_judge_validation(
        repository,
        authoring_instructions_hash=sha256_file(repository.case_authoring_path),
        source_snapshot_hash=snapshot.content_hash,
    )
    first = compile_workspace(
        repository,
        snapshot=snapshot,
        snapshot_directory=directory,
    )
    first_run = StageCoordinator(first).start("baseline", through="author")

    payload = yaml.safe_load(repository.workspace_file.read_text(encoding="utf-8"))
    payload["recorders"]["local-mlflow"]["tracking_uri"] = "http://127.0.0.1:6000"
    repository.workspace_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    updated_repository = WorkspaceRepository.from_file(repository.workspace_file)
    second = compile_workspace(
        updated_repository,
        snapshot=snapshot,
        snapshot_directory=directory,
    )
    second_run = StageCoordinator(second).start("baseline", through="author")

    assert first_run.status == "awaiting_execution"
    assert second_run.status == "awaiting_execution"
    first_manifest = StageLedger(FilesystemArtifactStore(repository.data_root)).load_run(
        first_run.run_id
    )
    second_manifest = StageLedger(FilesystemArtifactStore(repository.data_root)).load_run(
        second_run.run_id
    )
    assert first_manifest["experiment_revision_id"] == second_manifest["experiment_revision_id"]
    assert (
        first_manifest["experiment_definition_artifact"]
        == second_manifest["experiment_definition_artifact"]
    )


@pytest.mark.mlflow
def test_mlflow_adapter_write_probe_uses_the_pinned_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mlflow")
    from mlflow import MlflowClient

    monkeypatch.chdir(tmp_path)
    database = tmp_path / "mlflow.db"
    configuration = RecorderConfiguration(
        "test-mlflow",
        "mlflow",
        f"sqlite:///{database.as_posix()}",
        "ms-agent-eval/{workspace_id}/{experiment_id}",
        RecorderAuthenticationConfiguration("none", None, None, None),
    )

    result = MlflowRecorder(configuration, {}).healthcheck(write_test=True)

    assert result["reachable"] is True
    assert result["write_test"] == "passed"
    assert result["artifact_upload"] is True
    assert result["artifact_download"] is True
    assert result["table"] is True
    assert result["trace"] is True
    client = MlflowClient(tracking_uri=configuration.tracking_uri)
    assert client.get_experiment_by_name("ms-agent-eval/doctor/doctor") is None


@pytest.mark.mlflow
def test_mlflow_adapter_projects_a_stage_with_artifacts_and_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mlflow")
    monkeypatch.chdir(tmp_path)
    from mlflow import MlflowClient

    store = FilesystemArtifactStore(tmp_path / "external")
    _run(store)
    ledger = StageLedger(store)
    packet = StagePacket.create(
        run_id="run-1",
        experiment_id="baseline",
        experiment_revision_id=f"sha256:{'2' * 64}",
        stage=StageKind.SOLVE,
        attempt=1,
        dependencies=(),
        inputs=(ledger.put_json("case", {"task": "x" * 20_000}),),
        instructions=ledger.put_json("instructions", {"policy": "be correct"}),
        output_schema="solver-response-v1",
        executor=StageExecutorIdentity("remote-models", "openai_compatible", f"sha256:{'3' * 64}"),
        model=StageModelIdentity("solver", f"sha256:{'4' * 64}"),
        budget=StageBudget(1, 1000, 60),
    )
    ledger.prepare(packet)
    claim = ledger.claim(
        packet.packet_id,
        executor_id="remote-models",
        worker_id="worker",
    )
    result = StageResult.create(
        packet=packet,
        status="completed",
        executor_version="1",
        worker_id="worker",
        outputs=(ledger.put_json("solver_response", {"response": "done"}),),
        usage={"model_calls": 1, "tokens": 2},
        started_at="2026-09-14T10:00:01+00:00",
        completed_at="2026-09-14T10:00:02+00:00",
        observed_model="solver",
    )
    ledger.submit(result, lease_id=claim.lease_id)
    ledger.validate(packet.packet_id, lambda _packet, _result: None)
    ledger.persist(packet.packet_id)
    lifecycle = RunLifecycleLedger(store)
    lifecycle.append("run-1", RunLifecycleState.CREATED, reason="run_created")
    lifecycle.append("run-1", RunLifecycleState.COMPLETED, reason="run_completed")

    database = tmp_path / "projected-mlflow.db"
    recorder = RecorderConfiguration(
        "test-mlflow",
        "mlflow",
        f"sqlite:///{database.as_posix()}",
        "ms-agent-eval/{workspace_id}/{experiment_id}",
        RecorderAuthenticationConfiguration("none", None, None, None),
    )
    configuration = _configuration()
    plan = RecorderProjector(
        store,
        recorder=recorder,
        recording=configuration.recording,
    ).build("run-1")
    artifacts = {item.path: item for item in plan.artifacts}
    assert "graphs/stage-latency-summary.svg" in artifacts
    assert "graphs/stage-latency-summary.html" in artifacts
    assert "graphs/cache-reuse-by-stage.svg" in artifacts
    assert "graphs/cache-reuse-by-stage.html" in artifacts
    assert "index.html" in artifacts
    assert "tables/index.html" in artifacts
    assert "graphs/latency-and-tokens.svg" not in artifacts
    assert "graphs/endpoint-utilization.svg" not in artifacts
    local_report = store.summary_root / "runs" / "run-1"
    assert (local_report / "index.html").is_file()
    assert (local_report / "graphs" / "index.html").is_file()
    assert (local_report / "graphs" / "stage-latency-summary.html").is_file()
    assert (local_report / "graphs" / "stage-latency-summary.svg").is_file()
    assert (local_report / "tables" / "index.html").is_file()
    assert (local_report / "graphs" / "manifest.json").is_file()
    assert not any(path.startswith("reports/graphs/") for path in artifacts)
    assert "reports/index.html" not in artifacts
    assert "reports/tables/index.html" not in artifacts
    with store.get_blob(artifacts["graphs/stage-latency-summary.html"].reference) as handle:
        interactive_latency = handle.read().decode("utf-8")
    assert "cdn.plot.ly/plotly-" in interactive_latency
    assert "Plotly.newPlot" in interactive_latency
    assert "scrollZoom" in interactive_latency
    with store.get_blob(artifacts["graphs/manifest.json"].reference) as handle:
        chart_manifest = json.load(handle)
    latency_definition = next(
        item
        for item in chart_manifest["charts"]
        if item["static_artifact"] == "graphs/stage-latency-summary.svg"
    )
    assert chart_manifest["schema_version"] == 2
    assert latency_definition["artifact"] == "graphs/stage-latency-summary.html"
    assert latency_definition["interactive_artifact"] == ("graphs/stage-latency-summary.html")
    with store.get_blob(artifacts["graphs/index.html"].reference) as handle:
        chart_index = handle.read().decode("utf-8")
    assert "Plotly.react('scatter'" in chart_index
    assert "stage-latency-summary.html" in chart_index
    with store.get_blob(artifacts["tables/index.html"].reference) as handle:
        table_index = handle.read().decode("utf-8")
    assert "Experiment tables" in table_index
    assert "stage-attempts" in table_index
    assert "Filter any visible column" in table_index
    with store.get_blob(artifacts["index.html"].reference) as handle:
        recording_index = handle.read().decode("utf-8")
    assert "Open interactive charts" in recording_index
    assert "Open sortable tables" in recording_index
    with store.get_blob(artifacts["graphs/execution-dag.json"].reference) as handle:
        dag = json.load(handle)
    assert {item["type"] for item in dag["nodes"]} >= {"work", "attempt", "result"}
    assert any(item["type"] == "produces" for item in dag["edges"])
    metric_names = {item.key for item in plan.metrics}
    assert "latency.stage.solve.p50_seconds" in metric_names
    assert "latency.stage.solve.p95_seconds" in metric_names
    assert "cache.stage.solve.reuse_ratio" in metric_names
    assert len(json.dumps(plan.traces[0].inputs)) < 8_000
    assert "x" * 100 not in json.dumps(plan.traces[0].inputs)
    assert packet.inputs[0].content_id in json.dumps(plan.traces[0].inputs)
    stale_chart = local_report / "graphs" / "removed-chart.html"
    stale_chart.write_text("obsolete", encoding="utf-8")
    repeated_plan = RecorderProjector(
        store,
        recorder=recorder,
        recording=configuration.recording,
    ).build("run-1")
    assert repeated_plan.plan_id == plan.plan_id
    assert not stale_chart.exists()
    service = RecorderService(
        store,
        configuration.recording,
        recorder,
        {},
    )

    status = service.sync_run("run-1", raise_on_error=True)

    assert status["status"] == "synced"
    client = MlflowClient(tracking_uri=recorder.tracking_uri)
    root = client.get_run(str(status["external_run_id"]))
    assert root.data.tags["agent_eval.local_run_id"] == "run-1"
    assert root.data.tags["mlflow.runName"].endswith("· run run-1")
    assert root.info.status == "FINISHED"
    assert client.list_artifacts(root.info.run_id, "graphs")
    stage_tables = client.list_artifacts(root.info.run_id, "tables/stage-attempts")
    assert len(stage_tables) == 1
    downloaded = client.download_artifacts(
        root.info.run_id,
        stage_tables[0].path,
        (tmp_path / "downloaded").as_posix(),
    )
    table_payload = json.loads(Path(downloaded).read_text(encoding="utf-8"))
    assert set(table_payload) >= {"columns", "data"}
    assert "rows" not in table_payload
    traces = client.search_traces(
        experiment_ids=[str(status["external_experiment_id"])],
        include_spans=True,
        flush=True,
    )
    assert len(traces) == 1
    assert traces[0].info.tags["agent_eval.local_run_id"] == "run-1"

    lifecycle.append("run-1", RunLifecycleState.RUNNING, reason="run_resumed")
    resumed = service.sync_run("run-1", raise_on_error=True)

    assert resumed["external_run_id"] == status["external_run_id"]
    resumed_root = client.get_run(str(resumed["external_run_id"]))
    assert resumed_root.info.status == "RUNNING"
    scoped_runs = client.search_runs(
        [str(resumed["external_experiment_id"])],
        filter_string="tags.`agent_eval.local_run_id` = 'run-1'",
        max_results=1000,
    )
    assert all(item.info.status == "RUNNING" for item in scoped_runs)
