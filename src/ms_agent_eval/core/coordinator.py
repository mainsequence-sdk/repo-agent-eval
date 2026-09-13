from __future__ import annotations

import json
import random
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from threading import Lock
from types import MappingProxyType
from uuid import uuid4

import dspy

from ms_agent_eval.programs.dspy.budget import BudgetLedger, BudgetLimits
from ms_agent_eval.programs.dspy.engine import (
    DspyExecutionContract,
    DspyExecutor,
    create_author_program,
    create_judge_program,
    create_solver_program,
    load_state_json,
    program_hash,
)
from ms_agent_eval.programs.dspy.observed_lm import RoutedDspyLM
from ms_agent_eval.providers.openai_compatible import create_observed_lm

from .errors import (
    AgentEvalError,
    ConfigurationError,
    IntegrityError,
    PreflightError,
    ResolutionError,
)
from .authoring import (
    InstructionImportIdentity,
    InstructionUnitIdentity,
    SnapshotContext,
    SnapshotSourceIdentity,
)
from .evaluation import (
    CapabilityCatalog,
    CaseBank,
    CaseDefinition,
    JudgeValidationCorpus,
    JudgeValidationFixture,
    JudgeVote,
    LlmJudge,
    SplitManifest,
    aggregate_judge_votes,
    deterministic_group_k_fold,
    judge_validation_result_from_scores,
    validate_judge,
)
from .endpoint_pool import EndpointLease, EndpointPool, endpoint_provenance_routes
from .garbage_collection import collect_artifact_garbage
from .hashing import canonical_json_bytes, content_hash, json_value
from .models import ArtifactReference, ProgramResult, SnapshotLock
from .planning import CompiledWorkspace, ExperimentLock, lock_as_dict
from .progress import ProgressEvent, ProgressSink
from .providers import ModelCallObserver
from .reporting import PublishedReport, build_and_publish_run_report
from .stage_executors import (
    AutomaticExecutionFailed,
    ExecutorOutput,
    HarnessExecutionPending,
)
from .stages import (
    StageArtifact,
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
from .storage import FilesystemArtifactStore
from .snapshots import ExternalSnapshotStore
from .token_preflight import (
    ContextWindowExceededError,
    EndpointTokenPreflight,
    TokenPreflightResult,
)
from .workspace import (
    ExecutorConfiguration,
    ExecutorType,
    ExperimentMode,
    ResolvedRoleModel,
    RoleBudget,
    WorkspaceRepository,
    load_yaml,
    resolve_stage_model,
)


LmFactory = Callable[
    [ResolvedRoleModel, ModelCallObserver, BudgetLedger | None, str | None], dspy.BaseLM
]


def default_lm_factory(
    model: ResolvedRoleModel,
    observer: ModelCallObserver,
    budget: BudgetLedger | None,
    api_key: str | None,
) -> dspy.BaseLM:
    return create_observed_lm(model, observer, budget=budget, api_key=api_key)


@dataclass(frozen=True)
class RunProgress:
    schema_version: int
    run_id: str
    experiment_id: str
    execution_mode: str
    benchmark_valid: bool
    coverage_status: str
    status: str
    current_stage: str | None
    packets: tuple[Mapping[str, object], ...]
    result: Mapping[str, object] | None


@dataclass(frozen=True)
class _JudgeValidationVoteExecution:
    position: int
    fixture_id: str
    repetition: int
    vote: JudgeVote | None
    endpoint_id: str | None
    usage: Mapping[str, object]
    duration_seconds: float
    error: Exception | None


class _ExecutionSession:
    def __init__(
        self,
        coordinator: StageCoordinator,
        lock: ExperimentLock,
        environment: Mapping[str, str],
    ) -> None:
        self.coordinator = coordinator
        self.lock = lock
        self.environment = environment
        self.observers: dict[str, list[ModelCallObserver]] = {}
        self.budgets: dict[str, BudgetLedger] = {}
        self.delegates: dict[tuple[int, str], dspy.BaseLM] = {}
        self._lock = Lock()

    def lm(
        self,
        role: str,
        packet: StagePacket,
    ) -> tuple[dspy.BaseLM, ModelCallObserver]:
        model = self.lock.models.get(role) or resolve_stage_model(
            self.coordinator.workspace.repository.workspace,
            packet.stage.value,
            role=role,
        )
        if model is None:
            raise ConfigurationError(f"stage {packet.stage.value!r} has no model")
        executor = self.coordinator.workspace.repository.workspace.executors[model.executor_id]
        observer = ModelCallObserver(
            self.coordinator.artifacts,
            role=role,
            run_id=packet.run_id,
            packet_id=packet.packet_id,
            work_id=packet.work_id,
            attempt_id=packet.attempt_id,
            experiment_revision_id=packet.experiment_revision_id,
            stage=packet.stage.value,
        )
        with self._lock:
            self.observers.setdefault(packet.packet_id, []).append(observer)
            budget = self.budgets.get(packet.packet_id)
            if budget is None:
                budget = self.coordinator._budget_ledger(
                    packet.stage,
                    concurrency=(
                        executor.total_concurrency
                        if executor.type is ExecutorType.ENDPOINT
                        else 1
                    ),
                )
                self.budgets[packet.packet_id] = budget
        if executor.type is ExecutorType.ENDPOINT:
            lm = RoutedDspyLM(
                model=f"openai/{model.model}",
                route_factory=lambda messages, parameters: self._route_lm(
                    model,
                    executor,
                    observer,
                    budget,
                    messages,
                    parameters,
                ),
            )
        else:
            lm = self.coordinator.lm_factory(model, observer, budget, None)
        return lm, observer

    @contextmanager
    def _route_lm(
        self,
        model: ResolvedRoleModel,
        executor: ExecutorConfiguration,
        observer: ModelCallObserver,
        budget: BudgetLedger,
        messages: Sequence[Mapping[str, object]],
        call_parameters: Mapping[str, object],
    ):  # type: ignore[no-untyped-def]
        try:
            pool = self.coordinator.endpoint_pools[executor.id]
        except KeyError as error:
            raise IntegrityError("endpoint executor pool is unavailable") from error
        configured_ids = {endpoint.id for endpoint in executor.endpoints}
        excluded: set[str] = set()
        maximum_output_tokens = call_parameters.get("max_tokens")
        if maximum_output_tokens is None:
            maximum_output_tokens = call_parameters.get("max_completion_tokens")
        if maximum_output_tokens is None:
            maximum_output_tokens = model.parameters.get("max_tokens")
        if maximum_output_tokens is None:
            maximum_output_tokens = model.parameters.get("max_completion_tokens")
        if not isinstance(maximum_output_tokens, int) or isinstance(
            maximum_output_tokens, bool
        ):
            maximum_output_tokens = None

        while True:
            rejected: ContextWindowExceededError | None = None
            with pool.lease(excluded_endpoint_ids=frozenset(excluded)) as lease:
                endpoint = lease.endpoint
                api_key = endpoint.api_key(self.environment)
                preflight = self.coordinator.token_preflight.evaluate(
                    endpoint,
                    model=model.model,
                    deployment_revision=model.deployment_revision,
                    messages=messages,
                    maximum_output_tokens=maximum_output_tokens,
                    reasoning_effort=model.reasoning_effort,
                    api_key=api_key,
                )
                if not preflight.permits_dispatch:
                    observer.record_endpoint_route(
                        self._endpoint_route(
                            lease,
                            model,
                            preflight,
                            generation_attempted=False,
                            request_status="not_dispatched",
                            usage=_usage(()),
                        )
                    )
                    rejected = ContextWindowExceededError(endpoint.id, preflight)
                else:
                    started = time.monotonic()
                    record_start = len(observer.records)
                    request_status = "completed"
                    try:
                        delegate_key = (id(observer), endpoint.id)
                        with self._lock:
                            delegate = self.delegates.get(delegate_key)
                            if delegate is None:
                                delegate = self.coordinator.lm_factory(
                                    model.bind_endpoint(endpoint),
                                    observer,
                                    budget,
                                    api_key,
                                )
                                self.delegates[delegate_key] = delegate
                        yield delegate
                    except BaseException:
                        request_status = "failed"
                        raise
                    finally:
                        records = tuple(observer.records[record_start:])
                        if records and records[-1].status == "failed":
                            request_status = "failed"
                        observer.record_endpoint_route(
                            self._endpoint_route(
                                lease,
                                model,
                                preflight,
                                generation_attempted=True,
                                request_status=request_status,
                                usage=_usage(records),
                                duration_seconds=time.monotonic() - started,
                            )
                        )
                    return
            assert rejected is not None
            excluded.add(rejected.endpoint_id)
            if configured_ids <= excluded:
                raise rejected

    @staticmethod
    def _endpoint_route(
        lease: EndpointLease,
        model: ResolvedRoleModel,
        preflight: TokenPreflightResult,
        *,
        generation_attempted: bool,
        request_status: str,
        usage: Mapping[str, object],
        duration_seconds: float = 0.0,
    ) -> Mapping[str, object]:
        route = dict(lease.provenance()["endpoint_pool"])
        route.update(
            {
                "preflight": preflight.as_dict(),
                "model": model.model,
                "deployment_revision": model.deployment_revision,
                "generation_attempted": generation_attempted,
                "request_status": request_status,
                "request_duration_seconds": duration_seconds,
                **dict(usage),
            }
        )
        return route

    def evidence(self, packet_id: str) -> Mapping[str, object]:
        with self._lock:
            observers = tuple(self.observers.get(packet_id, ()))
        records = [record for observer in observers for record in observer.records]
        traces = [artifact for observer in observers for artifact in observer.trace_artifacts]
        if not records and not traces:
            return {}
        return {
            "model_call_ids": [record.call_id for record in records],
            "trace_artifacts": [json_value(artifact) for artifact in traces],
        }

    def usage(self, packet_id: str) -> Mapping[str, object]:
        with self._lock:
            observers = tuple(self.observers.get(packet_id, ()))
        return _usage(
            [
                record
                for observer in observers
                for record in observer.records
            ]
        )

    def endpoint_provenance(self, packet_id: str) -> Mapping[str, object]:
        with self._lock:
            observers = tuple(self.observers.get(packet_id, ()))
        observed = [
            dict(route)
            for observer in observers
            for route in observer.endpoint_routes
        ]
        if not observed:
            return {}
        grouped: dict[str, dict[str, object]] = {}
        for route in observed:
            endpoint_id = route.get("endpoint_id")
            if not isinstance(endpoint_id, str):
                continue
            aggregate = grouped.setdefault(
                endpoint_id,
                {
                    "pool_hash": route.get("pool_hash"),
                    "endpoint_id": endpoint_id,
                    "endpoint_configuration_hash": route.get(
                        "endpoint_configuration_hash"
                    ),
                    "model": route.get("model"),
                    "deployment_revision": route.get("deployment_revision"),
                    "max_concurrent_requests": route.get(
                        "max_concurrent_requests"
                    ),
                    "requests": 0,
                    "completed_requests": 0,
                    "failed_requests": 0,
                    "preflight_checks": 0,
                    "preflight_rejections": 0,
                    "preflight_skips": 0,
                    "model_calls": 0,
                    "completed_calls": 0,
                    "failed_calls": 0,
                    "tokens": 0,
                    "configured_cost": 0.0,
                    "latency_seconds": 0.0,
                    "queue_wait_seconds": 0.0,
                    "peak_observed_endpoint_concurrency": 0,
                    "peak_observed_pool_concurrency": 0,
                },
            )
            preflight = route.get("preflight")
            if isinstance(preflight, Mapping):
                status = preflight.get("status")
                aggregate["preflight_checks"] = int(aggregate["preflight_checks"]) + 1
                if status == "exceeds":
                    aggregate["preflight_rejections"] = int(
                        aggregate["preflight_rejections"]
                    ) + 1
                elif status == "skipped":
                    aggregate["preflight_skips"] = int(
                        aggregate["preflight_skips"]
                    ) + 1
                aggregate["context_window"] = preflight.get("context_window")
                aggregate["last_preflight"] = dict(preflight)
            if route.get("generation_attempted") is True:
                aggregate["requests"] = int(aggregate["requests"]) + 1
                outcome = (
                    "completed_requests"
                    if route.get("request_status") == "completed"
                    else "failed_requests"
                )
                aggregate[outcome] = int(aggregate[outcome]) + 1
            for key in ("model_calls", "completed_calls", "failed_calls", "tokens"):
                value = route.get(key, 0)
                if isinstance(value, int) and not isinstance(value, bool):
                    aggregate[key] = int(aggregate[key]) + value
            for key in ("configured_cost", "latency_seconds", "queue_wait_seconds"):
                value = route.get(key, 0.0)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    aggregate[key] = float(aggregate[key]) + float(value)
            aggregate["peak_observed_endpoint_concurrency"] = max(
                int(aggregate["peak_observed_endpoint_concurrency"]),
                int(route.get("endpoint_active_at_start", 0)),
            )
            aggregate["peak_observed_pool_concurrency"] = max(
                int(aggregate["peak_observed_pool_concurrency"]),
                int(route.get("pool_active_at_start", 0)),
            )

        routes = [grouped[key] for key in sorted(grouped)]
        role = observers[0].role
        resolved_model = self.lock.models.get(role) or resolve_stage_model(
            self.coordinator.workspace.repository.workspace,
            self.coordinator.ledger.load_packet(packet_id).stage.value,
            role=role,
        )
        if resolved_model is None:
            raise IntegrityError("endpoint provenance has no resolved model")
        executor = self.coordinator.workspace.repository.workspace.executors[
            resolved_model.executor_id
        ]
        pool_value: dict[str, object] = {
            "pool_hash": executor.pool_hash,
            "total_concurrency": executor.total_concurrency,
            "requests": sum(int(route["requests"]) for route in routes),
            "completed_requests": sum(
                int(route["completed_requests"]) for route in routes
            ),
            "failed_requests": sum(int(route["failed_requests"]) for route in routes),
            "preflight_checks": sum(int(route["preflight_checks"]) for route in routes),
            "preflight_rejections": sum(
                int(route["preflight_rejections"]) for route in routes
            ),
            "preflight_skips": sum(int(route["preflight_skips"]) for route in routes),
            "routes": routes,
        }
        if len(routes) == 1:
            singular = dict(routes[0])
            singular["total_concurrency"] = executor.total_concurrency
            return {"endpoint_pool": singular}
        return {"endpoint_pool": pool_value}


class StageCoordinator:
    """Advance experiments exclusively through the portable stage contract."""

    def __init__(
        self,
        workspace: CompiledWorkspace | WorkspaceRepository,
        *,
        lm_factory: LmFactory = default_lm_factory,
        progress_sink: ProgressSink | None = None,
        token_preflight: EndpointTokenPreflight | None = None,
    ) -> None:
        self.workspace = workspace
        repository = (
            workspace
            if isinstance(workspace, WorkspaceRepository)
            else workspace.repository
        )
        self.artifacts = FilesystemArtifactStore(
            repository.data_root,
            workspace_root=repository.root,
        )
        self.ledger = StageLedger(self.artifacts)
        self.dspy = DspyExecutor(self.artifacts)
        self.lm_factory = lm_factory
        self.token_preflight = token_preflight or EndpointTokenPreflight()
        self.progress_sink = progress_sink
        self._progress_lock = Lock()
        executor_lock_root = self.artifacts.operations_root / "locks" / "executors"
        self.endpoint_pools = {
            executor.id: EndpointPool(executor, executor_lock_root)
            for executor in repository.workspace.executors.values()
            if executor.type is ExecutorType.ENDPOINT
        }

    def build_report(self, run_id: str, *, replace: bool = False) -> PublishedReport:
        """Build a deterministic derived report without executing a model."""

        run = self.ledger.load_run(run_id)
        repository = (
            self.workspace
            if isinstance(self.workspace, WorkspaceRepository)
            else self.workspace.repository
        )
        environment = repository.environment()
        if isinstance(self.workspace, WorkspaceRepository):
            with self._frozen_coordinator(run, environment) as coordinator:
                return coordinator.build_report(run_id, replace=replace)
        if not self._is_active_revision(run, environment):
            with self._frozen_coordinator(run, environment) as coordinator:
                return coordinator.build_report(run_id, replace=replace)
        return build_and_publish_run_report(
            self.workspace,
            self.ledger,
            run_id,
            replace=replace,
        )

    def start(
        self,
        experiment_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        through: str | None = None,
        pilot: bool = False,
    ) -> RunProgress:
        resolved_environment = (
            environment if environment is not None else self.workspace.repository.environment()
        )
        lock = self.workspace.experiment_lock(experiment_id, environment=resolved_environment)
        if pilot and lock.configuration.mode is not ExperimentMode.EVALUATE:
            raise PreflightError("pilot execution supports evaluate experiments only")
        self._validate_judge_validation_split(lock, pilot=pilot)
        run_id = str(uuid4())
        execution_mode = "pilot" if pilot else "benchmark"
        revision_digest = lock.content_hash.removeprefix("sha256:")
        definition_artifact = self.ledger.put_json(
            "experiment_definition", self._experiment_definition(lock)
        )
        lock_reference = self.artifacts.put_manifest(
            f"experiments/{experiment_id}/{revision_digest}/experiment.lock",
            lock_as_dict(lock),
        )
        bundle_reference = self.artifacts.put_manifest(
            f"experiments/{experiment_id}/{revision_digest}/bundle",
            {
                "schema_version": 2,
                "experiment_family": experiment_id,
                "experiment_revision_id": lock.content_hash,
                "lock": json_value(lock_reference),
                "definition": json_value(definition_artifact),
                "source_snapshot_hash": lock.source_snapshot_hash,
                "case_bank_hash": lock.case_bank_hash,
                "split_hash": lock.split_hash,
                "judge_validation_hash": lock.judge_validation_hash,
                "case_ids": [item.id for item in lock.cases],
            },
        )
        self.ledger.create_run(
            run_id,
            {
                "schema_version": 4,
                "run_id": run_id,
                "experiment_id": experiment_id,
                "execution_mode": execution_mode,
                "benchmark_valid": not pilot,
                "coverage_status": self.workspace.validation["status"],
                "selected_splits": (
                    ["train"] if pilot else ["test"]
                ),
                "judge_repetitions": (
                    1
                    if pilot
                    else self.workspace.repository.workspace.evaluation.judge.repetitions
                ),
                "workspace_id": lock.workspace_id,
                "workspace_hash": lock.workspace_hash,
                "experiment_revision_id": lock.content_hash,
                "experiment_lock_hash": lock.content_hash,
                "experiment_lock_artifact": json_value(lock_reference),
                "experiment_bundle_artifact": json_value(bundle_reference),
                "experiment_definition_artifact": json_value(definition_artifact),
                "created_at": _timestamp(),
            },
        )
        self.artifacts.put_manifest(
            f"experiments/{experiment_id}/{revision_digest}/runs/{run_id}",
            {
                "schema_version": 1,
                "run_id": run_id,
                "experiment_family": experiment_id,
                "experiment_revision_id": lock.content_hash,
            },
        )
        self._emit(
            "run_started",
            run_id,
            experiment_id,
            details={
                "execution_mode": execution_mode,
                "case_count": self._run_case_count(
                    lock.configuration.mode,
                    pilot=pilot,
                ),
                "data_root": self.artifacts.data_root.as_posix(),
                "experiment_revision_id": lock.content_hash,
            },
        )
        return self._advance(
            run_id,
            lock,
            environment=resolved_environment,
            through=through,
            pilot=pilot,
        )

    def _experiment_definition(self, lock: ExperimentLock) -> Mapping[str, object]:
        """Freeze all static, stage-visible inputs needed by this revision."""

        file_inventory = self._workspace_file_inventory()
        return {
            "schema_version": 3,
            "experiment_family": lock.configuration.id,
            "experiment_revision_id": lock.content_hash,
            "lock": lock_as_dict(lock),
            "workspace_configuration_json": self._selected_workspace_configuration(lock),
            "workspace_files": file_inventory,
            "target_snapshot_hash": self.workspace.snapshot.content_hash,
            "validation": json_value(self.workspace.validation),
            "context": {
                "snapshot_hash": self.workspace.context.snapshot_hash,
                "resolved_commit": self.workspace.context.resolved_commit,
                "global": self.workspace.context.global_context,
                "skills": dict(self.workspace.context.skill_contexts),
                "sources": json_value(self.workspace.context.sources),
                "instruction_imports": json_value(
                    self.workspace.context.instruction_imports
                ),
                "instruction_units": json_value(self.workspace.context.instruction_units),
                "source_paths": list(self.workspace.context.source_paths),
                "workspace_source_paths": list(
                    self.workspace.context.workspace_source_paths
                ),
            },
            "authoring": {
                "instructions": self.workspace.repository.case_authoring_instructions(),
                "capability_catalog": json_value(self.workspace.capability_catalog),
            },
            "cases": [
                {
                    "id": case.id,
                    "title": case.title,
                    "skill": case.skill,
                    "group": case.group,
                    "split": self.workspace.splits.split_for(case),
                    "prompt": case.prompt,
                    "solver_attachments": dict(case.solver_attachments()),
                    "expected_response": case.expected_response,
                    "expected_artifacts": dict(case.expected_artifacts()),
                    "rubric": json.loads(case.rubric.prompt_text()),
                    "source_paths": list(case.source_paths),
                    "solver_view_hash": case.solver_view_hash,
                    "judge_view_hash": case.judge_view_hash,
                    "metadata_hash": case.metadata_hash,
                    "case_hash": case.content_hash,
                }
                for case in self.workspace.case_bank.cases
            ],
            "judge_validation": {
                "content_hash": self.workspace.judge_validation.content_hash,
                "fixtures": [
                    {
                        "id": fixture.id,
                        "case_id": fixture.case_id,
                        "candidate_response": fixture.candidate_path.read_text(
                            encoding="utf-8"
                        ),
                        "label": fixture.label,
                    }
                    for fixture in self.workspace.judge_validation.fixtures
                ],
            },
        }

    def _selected_workspace_configuration(
        self, lock: ExperimentLock
    ) -> str:
        """Project the workspace to only the selected experiment family."""

        payload = dict(load_yaml(self.workspace.repository.workspace_file))
        experiments = _mapping(payload.get("experiments"), "workspace experiments")
        selected_ids = [lock.configuration.id]
        if lock.configuration.based_on is not None:
            selected_ids.append(lock.configuration.based_on)
        payload["experiments"] = {
            experiment_id: experiments[experiment_id] for experiment_id in selected_ids
        }
        stages = _mapping(payload.get("stages"), "workspace stages")
        executor_ids = {
            _string(
                _mapping(stage, f"workspace stage {stage_id}").get("executor"),
                f"workspace stage {stage_id} executor",
            )
            for stage_id, stage in stages.items()
        }
        model_ids: set[str] = set()
        for stage_id, stage in stages.items():
            raw_model = _mapping(stage, f"workspace stage {stage_id}").get("model")
            if raw_model is not None:
                model_ids.add(
                    _string(raw_model, f"workspace stage {stage_id} model")
                )
        executors = _mapping(payload.get("executors"), "workspace executors")
        models = _mapping(payload.get("models"), "workspace models")
        payload["executors"] = {
            executor_id: executors[executor_id]
            for executor_id in sorted(executor_ids)
        }
        payload["models"] = {
            model_id: models[model_id] for model_id in sorted(model_ids)
        }
        workspace = dict(_mapping(payload.get("workspace"), "workspace configuration"))
        workspace["data_root"] = self.artifacts.data_root.as_posix()
        payload["workspace"] = workspace
        return canonical_json_bytes(payload).decode("utf-8")

    def _workspace_file_inventory(self) -> list[Mapping[str, object]]:
        """Store every local file needed to reconstruct a selected revision."""

        repository = self.workspace.repository
        candidates = {
            repository.case_authoring_path,
            repository.capability_catalog_path,
            repository.split_file,
            repository.judge_validation_root / "manifest.yaml",
            *(fixture.candidate_path for fixture in self.workspace.judge_validation.fixtures),
            *(
                repository.root / relative
                for relative in self.workspace.context.workspace_source_paths
            ),
        }
        for case in self.workspace.case_bank.cases:
            candidates.update(
                {
                    case.path / "case.yaml",
                    case.solver_prompt_path,
                    case.judge_expected_path,
                    case.judge_rubric_path,
                }
            )
            for root in (
                case.solver_attachments_directory,
                case.expected_artifacts_directory,
            ):
                if root is not None:
                    candidates.update(
                        path
                        for path in root.rglob("*")
                        if path.is_file() and not path.is_symlink()
                    )
        inventory: list[Mapping[str, object]] = []
        workspace_relative = repository.workspace_file.relative_to(
            repository.root
        ).as_posix()
        for path in sorted(candidates):
            resolved = path.resolve()
            if path.is_symlink() or repository.root not in resolved.parents:
                raise IntegrityError(f"revision input file is unsafe: {path}")
            relative = resolved.relative_to(repository.root).as_posix()
            if relative == workspace_relative:
                raise ConfigurationError(
                    "the workspace manifest cannot also be an evaluation input file"
                )
            with resolved.open("rb") as handle:
                reference = self.artifacts.put_blob(handle, "application/octet-stream")
            inventory.append(
                {
                    "path": relative,
                    "artifact": json_value(reference),
                }
            )
        return inventory

    @contextmanager
    def _frozen_coordinator(
        self,
        run: Mapping[str, object],
        environment: Mapping[str, str],
    ):  # type: ignore[no-untyped-def]
        """Rehydrate the immutable experiment definition referenced by a run."""

        run_id = _string(run.get("run_id"), "run id")
        family = _string(run.get("experiment_id"), "run experiment_id")
        revision = _string(
            run.get("experiment_revision_id"), "run experiment_revision_id"
        )
        definition_reference = self._artifact_reference(
            run.get("experiment_definition_artifact"),
            "experiment definition artifact",
        )
        with self.artifacts.get_blob(definition_reference) as handle:
            try:
                loaded = json.load(handle)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise IntegrityError(
                    f"experiment definition is invalid for run {run_id}"
                ) from error
        definition = _mapping(loaded, "experiment definition")
        if definition.get("schema_version") != 3:
            raise IntegrityError("experiment definition schema_version must be 3")
        if (
            definition.get("experiment_family") != family
            or definition.get("experiment_revision_id") != revision
        ):
            raise IntegrityError("experiment definition identity differs from its run")

        with tempfile.TemporaryDirectory(
            prefix=f"frozen-{revision.removeprefix('sha256:')[:12]}-",
            dir=self.artifacts.temporary_root,
        ) as temporary:
            root = Path(temporary)
            workspace_json = _string(
                definition.get("workspace_configuration_json"),
                "frozen workspace configuration JSON",
            )
            (root / "workspace.yaml").write_text(
                workspace_json,
                encoding="utf-8",
            )
            materialized_paths: set[str] = set()
            for item in _list(definition.get("workspace_files"), "workspace files"):
                entry = _mapping(item, "workspace files[]")
                relative = self._safe_bundle_path(entry.get("path"))
                if relative in materialized_paths:
                    raise IntegrityError(f"duplicate frozen workspace file: {relative}")
                materialized_paths.add(relative)
                reference = self._artifact_reference(
                    entry.get("artifact"), f"workspace file {relative} artifact"
                )
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with self.artifacts.get_blob(reference) as source:
                    destination.write_bytes(source.read())

            repository = WorkspaceRepository.from_file(root / "workspace.yaml")
            snapshot_store = ExternalSnapshotStore(repository.data_root)
            snapshot = self._snapshot_from_hash(
                snapshot_store,
                _string(definition.get("target_snapshot_hash"), "target snapshot hash"),
            )
            context = self._frozen_context(
                _mapping(definition.get("context"), "frozen context"),
                snapshot_store,
                workspace_root=root,
            )
            case_bank = CaseBank.discover(repository.cases_root)
            capability_catalog = CapabilityCatalog.load(
                repository.capability_catalog_path
            )
            splits = SplitManifest.load(repository.split_file, case_bank)
            judge_validation = JudgeValidationCorpus.load(repository.judge_validation_root, case_bank)
            validation = _mapping(
                definition.get("validation"), "frozen validation"
            )
            frozen = CompiledWorkspace(
                repository,
                snapshot,
                snapshot_store.directory(snapshot),
                context,
                capability_catalog,
                case_bank,
                splits,
                judge_validation,
                MappingProxyType(validation),
            )
            frozen_lock = frozen.experiment_lock(family, environment=environment)
            if frozen_lock.content_hash != revision:
                raise IntegrityError(
                    "stored experiment definition does not reproduce its revision"
                )
            yield StageCoordinator(
                frozen,
                lm_factory=self.lm_factory,
                progress_sink=self.progress_sink,
            )

    def _artifact_reference(self, value: object, field: str) -> ArtifactReference:
        payload = _mapping(value, field)
        content_id = _string(payload.get("content_id"), f"{field} content_id")
        media_type = _string(payload.get("media_type"), f"{field} media_type")
        resolved = self.artifacts.reference(content_id, media_type)
        has_storage_metadata = {
            "size_bytes",
            "relative_path",
        }.intersection(payload)
        if has_storage_metadata and has_storage_metadata != {
            "size_bytes",
            "relative_path",
        }:
            raise IntegrityError(f"{field} has incomplete storage metadata")
        if has_storage_metadata and (
            payload["size_bytes"] != resolved.size_bytes
            or payload["relative_path"] != resolved.relative_path
        ):
            raise IntegrityError(f"{field} metadata does not match content storage")
        return resolved

    @staticmethod
    def _safe_bundle_path(value: object) -> str:
        path = PurePosixPath(_string(value, "workspace file path"))
        if path.is_absolute() or ".." in path.parts or path.as_posix() in {"", "."}:
            raise IntegrityError("workspace file path must be safe and relative")
        return path.as_posix()

    @staticmethod
    def _snapshot_from_hash(
        store: ExternalSnapshotStore, snapshot_hash: str
    ) -> SnapshotLock:
        digest = snapshot_hash.removeprefix("sha256:")
        if (
            not snapshot_hash.startswith("sha256:")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise IntegrityError("snapshot hash must be a sha256 content id")
        lock_file = store.snapshot_root / digest / "snapshot.lock.json"
        if not lock_file.is_file() or lock_file.is_symlink():
            raise IntegrityError(f"immutable snapshot is unavailable: {snapshot_hash}")
        try:
            loaded = json.loads(lock_file.read_text(encoding="utf-8"))
            lock = SnapshotLock.from_mapping(_mapping(loaded, "snapshot lock"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ConfigurationError) as error:
            raise IntegrityError(f"immutable snapshot is invalid: {snapshot_hash}") from error
        if lock.content_hash != snapshot_hash:
            raise IntegrityError("snapshot directory and lock identities differ")
        store.verify(lock)
        return lock

    def _frozen_context(
        self,
        payload: Mapping[str, object],
        store: ExternalSnapshotStore,
        *,
        workspace_root: Path,
    ) -> SnapshotContext:
        raw_sources = _list(payload.get("sources"), "frozen context sources")
        sources: list[SnapshotSourceIdentity] = []
        source_files: dict[str, Path] = {}
        for raw_source in raw_sources:
            item = _mapping(raw_source, "frozen context sources[]")
            include = tuple(
                _string(pattern, "frozen source include[]")
                for pattern in _list(item.get("include"), "frozen source include")
            )
            source = SnapshotSourceIdentity(
                id=_string(item.get("id"), "frozen source id"),
                role=_string(item.get("role"), "frozen source role"),
                repository_url=_string(
                    item.get("repository_url"), "frozen source repository_url"
                ),
                requested_ref=_string(
                    item.get("requested_ref"), "frozen source requested_ref"
                ),
                resolved_commit=_string(
                    item.get("resolved_commit"), "frozen source resolved_commit"
                ),
                snapshot_hash=_string(
                    item.get("snapshot_hash"), "frozen source snapshot_hash"
                ),
                include=include,
            )
            source_lock = self._snapshot_from_hash(store, source.snapshot_hash)
            if (
                source_lock.repository_url != source.repository_url
                or source_lock.resolved_commit != source.resolved_commit
                or source_lock.requested_ref.value != source.requested_ref
            ):
                raise IntegrityError("frozen source identity differs from its snapshot")
            for locked_file in source_lock.files:
                if source.id != "target" and not any(
                    fnmatchcase(locked_file.source_path, pattern) for pattern in include
                ):
                    continue
                qualified = (
                    locked_file.source_path
                    if source.id == "target"
                    else f"references/{source.id}/{locked_file.source_path}"
                )
                if qualified in source_files:
                    raise IntegrityError(f"frozen source path collision: {qualified}")
                source_files[qualified] = (
                    store.directory(source_lock) / locked_file.snapshot_path
                )
            sources.append(source)
        workspace_source_paths = tuple(
            sorted(
                self._safe_bundle_path(item)
                for item in _list(
                    payload.get("workspace_source_paths"),
                    "frozen context workspace_source_paths",
                )
            )
        )
        for relative in workspace_source_paths:
            if relative in source_files:
                raise IntegrityError(f"frozen workspace source path collision: {relative}")
            candidate = workspace_root / relative
            if candidate.is_symlink() or not candidate.is_file():
                raise IntegrityError(f"frozen workspace source is missing or unsafe: {relative}")
            source_files[relative] = candidate
        declared_paths = tuple(
            sorted(
                _string(item, "frozen context source_paths[]")
                for item in _list(
                    payload.get("source_paths"), "frozen context source_paths"
                )
            )
        )
        if declared_paths != tuple(sorted(source_files)):
            raise IntegrityError("frozen context source inventory is incomplete")
        skills = _mapping(payload.get("skills"), "frozen context skills")
        instruction_units = tuple(
            InstructionUnitIdentity(
                id=_string(item.get("id"), "frozen instruction unit id"),
                source_id=_string(
                    item.get("source_id"), "frozen instruction unit source_id"
                ),
                source_type=_string(
                    item.get("source_type"), "frozen instruction unit source_type"
                ),
                source_path=self._safe_bundle_path(item.get("source_path")),
                content_hash=_string(
                    item.get("content_hash"), "frozen instruction unit content_hash"
                ),
            )
            for item in (
                _mapping(raw, "frozen instruction units[]")
                for raw in _list(
                    payload.get("instruction_units"), "frozen instruction units"
                )
            )
        )
        if {item.id for item in instruction_units} != set(skills):
            raise IntegrityError("frozen instruction units do not match frozen skill contexts")
        instruction_imports: list[InstructionImportIdentity] = []
        for raw_import in _list(
            payload.get("instruction_imports"), "frozen instruction imports"
        ):
            item = _mapping(raw_import, "frozen instruction imports[]")
            imported_units = tuple(
                InstructionUnitIdentity(
                    id=_string(unit.get("id"), "frozen imported unit id"),
                    source_id=_string(
                        unit.get("source_id"), "frozen imported unit source_id"
                    ),
                    source_type=_string(
                        unit.get("source_type"), "frozen imported unit source_type"
                    ),
                    source_path=self._safe_bundle_path(unit.get("source_path")),
                    content_hash=_string(
                        unit.get("content_hash"), "frozen imported unit content_hash"
                    ),
                )
                for unit in (
                    _mapping(raw_unit, "frozen imported units[]")
                    for raw_unit in _list(item.get("units"), "frozen imported units")
                )
            )
            instruction_imports.append(
                InstructionImportIdentity(
                    id=_string(item.get("id"), "frozen instruction import id"),
                    source=_string(item.get("source"), "frozen instruction import source"),
                    note=_string(item.get("note"), "frozen instruction import note"),
                    manifest_path=self._safe_bundle_path(item.get("manifest_path")),
                    manifest_hash=_string(
                        item.get("manifest_hash"), "frozen instruction import manifest_hash"
                    ),
                    source_metadata=MappingProxyType(
                        _mapping(
                            item.get("source_metadata"),
                            "frozen instruction import source_metadata",
                        )
                    ),
                    units=imported_units,
                )
            )
        return SnapshotContext(
            snapshot_hash=_string(
                payload.get("snapshot_hash"), "frozen context snapshot_hash"
            ),
            resolved_commit=_string(
                payload.get("resolved_commit"), "frozen context resolved_commit"
            ),
            sources=tuple(sources),
            instruction_imports=tuple(instruction_imports),
            instruction_units=instruction_units,
            global_context=_text(payload.get("global"), "frozen global context"),
            skill_contexts=MappingProxyType(
                {
                    skill_id: _text(value, f"frozen skill {skill_id}")
                    for skill_id, value in skills.items()
                }
            ),
            source_paths=declared_paths,
            source_files=MappingProxyType(source_files),
            workspace_source_paths=workspace_source_paths,
        )

    def resume(
        self,
        run_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        through: str | None = None,
    ) -> RunProgress:
        run = self.ledger.load_run(run_id)
        experiment_id = _string(run.get("experiment_id"), "run experiment_id")
        pilot = _execution_mode(run) == "pilot"
        resolved_environment = (
            environment if environment is not None else self.workspace.repository.environment()
        )
        if not self._is_active_revision(run, resolved_environment):
            with self._frozen_coordinator(run, resolved_environment) as coordinator:
                return coordinator.resume(
                    run_id,
                    environment=resolved_environment,
                    through=through,
                )
        lock = self.workspace.experiment_lock(experiment_id, environment=resolved_environment)
        self._validate_judge_validation_split(lock, pilot=pilot)
        self._emit(
            "run_resumed",
            run_id,
            experiment_id,
            details={
                "execution_mode": _execution_mode(run),
                "case_count": self._run_case_count(
                    lock.configuration.mode,
                    pilot=pilot,
                ),
                "data_root": self.artifacts.data_root.as_posix(),
                "experiment_revision_id": lock.content_hash,
            },
        )
        return self._advance(
            run_id,
            lock,
            environment=resolved_environment,
            through=through,
            pilot=pilot,
        )

    def prepare_stage(
        self,
        run_id: str,
        stage: StageKind,
        *,
        campaign: str | None = None,
    ) -> tuple[StagePacket, ...]:
        run = self.ledger.load_run(run_id)
        experiment_id = _string(run.get("experiment_id"), "run experiment_id")
        pilot = _execution_mode(run) == "pilot"
        environment = self.workspace.repository.environment()
        if not self._is_active_revision(run, environment):
            with self._frozen_coordinator(run, environment) as coordinator:
                return coordinator.prepare_stage(run_id, stage, campaign=campaign)
        lock = self.workspace.experiment_lock(experiment_id, environment=environment)
        if stage is StageKind.AUTHOR:
            return (self._author_packet(run_id, lock, campaign=campaign),)
        if stage is StageKind.VALIDATE_JUDGE:
            return (self._judge_validation_packet(run_id, lock),)
        judge_validation = self._persisted_packet(run_id, StageKind.VALIDATE_JUDGE)
        if judge_validation is None or not self._judge_validation_succeeded(judge_validation):
            raise PreflightError("judge validation must succeed before this stage is prepared")
        if stage is StageKind.FIT:
            if pilot:
                raise PreflightError("pilot runs cannot prepare fitting")
            if lock.configuration.mode is not ExperimentMode.OPTIMIZE:
                raise PreflightError("fit is valid only for optimization experiments")
            selection = self._persisted_packet(run_id, StageKind.SELECT)
            if selection is not None:
                return (
                    self._final_fit_packet(run_id, lock, judge_validation, selection),
                )
            return self._cross_validation_fit_packets(run_id, lock, judge_validation)
        if stage is StageKind.SOLVE:
            if lock.configuration.mode is ExperimentMode.OPTIMIZE:
                fits = self._persisted_fit_packets(run_id, phase="final_fit")
                if fits:
                    return self._solve_packets(
                        run_id,
                        lock,
                        judge_validation,
                        selected_split="test",
                        program_dependency=fits[0],
                        evaluation_phase="test",
                    )
                fits = self._persisted_fit_packets(run_id, phase="cross_validation")
                if not fits:
                    raise PreflightError("cross-validation fits must persist before solve")
                return self._cross_validation_solve_packets(
                    run_id, lock, judge_validation, fits
                )
            return self._solve_packets(
                run_id,
                lock,
                judge_validation,
                selected_split="train" if pilot else "test",
                evaluation_phase="pilot" if pilot else "test",
            )
        if stage is StageKind.JUDGE:
            solves = tuple(
                item.packet
                for item in self.ledger.list(run_id)
                if item.packet.stage is StageKind.SOLVE and item.state is StageState.PERSISTED
            )
            if not solves:
                raise PreflightError("at least one solve result must persist before judge")
            return self._judge_packets(run_id, lock, judge_validation, solves)
        if stage is StageKind.SELECT:
            if lock.configuration.mode is not ExperimentMode.OPTIMIZE:
                raise PreflightError("select is valid only for optimization experiments")
            judges = self._persisted_judges(run_id, phase="cross_validation")
            if not judges:
                raise PreflightError("cross-validation judgments must persist before selection")
            return (self._selection_packet(run_id, lock, judge_validation, judges),)
        if stage is StageKind.AGGREGATE:
            judges = self._persisted_judges(run_id)
            if not judges:
                raise PreflightError("judgments must persist before aggregate")
            return (self._aggregate_packet(run_id, lock, judge_validation, judges),)
        raise ResolutionError(f"unsupported stage: {stage.value}")

    def execute_packet(
        self,
        packet_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        executor_id: str | None = None,
    ) -> StageResult | None:
        packet = self.ledger.load_packet(packet_id)
        run = self.ledger.load_run(packet.run_id)
        resolved_environment = (
            environment if environment is not None else self.workspace.repository.environment()
        )
        if not self._is_active_revision(run, resolved_environment):
            with self._frozen_coordinator(run, resolved_environment) as coordinator:
                return coordinator.execute_packet(
                    packet_id,
                    environment=resolved_environment,
                    executor_id=executor_id,
                )
        lock = self.workspace.experiment_lock(
            _string(run.get("experiment_id"), "run experiment_id"),
            environment=resolved_environment,
        )
        session = _ExecutionSession(
            self,
            lock,
            resolved_environment,
        )
        result = self._run_packet(packet, lock, session, executor_id=executor_id)
        if self._packet_ends_run(packet, result):
            self._finalize_run(packet.run_id, packet=packet)
        return result

    def submit_file(
        self,
        packet_id: str,
        result_file: Path,
        *,
        executor_id: str,
        worker_id: str,
        lease_id: str,
    ) -> StageResult:
        packet = self.ledger.load_packet(packet_id)
        run = self.ledger.load_run(packet.run_id)
        environment = self.workspace.repository.environment()
        if not self._is_active_revision(run, environment):
            with self._frozen_coordinator(run, environment) as coordinator:
                return coordinator.submit_file(
                    packet_id,
                    result_file,
                    executor_id=executor_id,
                    worker_id=worker_id,
                    lease_id=lease_id,
                )
        if not result_file.is_file() or result_file.is_symlink():
            raise ResolutionError(f"stage result file does not exist: {result_file}")
        payload = json.loads(result_file.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ConfigurationError("stage result file must contain one JSON object")
        if executor_id != packet.executor.id:
            raise IntegrityError("submission executor does not match packet executor")
        output = self.ledger.put_json("result", payload)
        now = _timestamp()
        result = StageResult.create(
            packet=packet,
            status="completed",
            executor_version=None,
            worker_id=worker_id,
            outputs=(output,),
            usage={},
            provenance={
                "cache_eligible": not (
                    packet.stage is StageKind.VALIDATE_JUDGE
                    and payload.get("ordering_consistent") is not True
                )
            },
            started_at=now,
            completed_at=now,
            observed_model=packet.model.id,
        )
        self.ledger.submit(result, lease_id=lease_id)
        self.ledger.validate(packet.packet_id, self._validate_result)
        persisted = self.ledger.persist(packet.packet_id)
        self._emit_packet(
            "packet_completed",
            packet,
            details=self._output_event_details(packet.stage, payload),
        )
        if self._packet_ends_run(packet, persisted):
            self._finalize_run(packet.run_id, packet=packet)
        return persisted

    def _is_active_revision(
        self,
        run: Mapping[str, object],
        environment: Mapping[str, str],
    ) -> bool:
        family = _string(run.get("experiment_id"), "run experiment_id")
        try:
            lock = self.workspace.experiment_lock(family, environment=environment)
        except ResolutionError:
            return False
        return lock.content_hash == run.get("experiment_revision_id")

    def _advance(
        self,
        run_id: str,
        lock: ExperimentLock,
        *,
        environment: Mapping[str, str],
        through: str | None,
        pilot: bool,
    ) -> RunProgress:
        if through is not None and through not in {item.value for item in StageKind}:
            raise ConfigurationError(f"unknown --through stage: {through}")
        session = _ExecutionSession(self, lock, environment)
        if through == StageKind.AUTHOR.value:
            author = self._author_packet(run_id, lock, campaign=None)
            if not self._ensure_executed(author, lock, session):
                return self._blocked_progress(run_id, lock.configuration.id, author)
            return self._progress(run_id, lock.configuration.id, "paused", author.stage)
        judge_validation = self._judge_validation_packet(run_id, lock)
        if not self._ensure_executed(judge_validation, lock, session):
            return self._blocked_progress(run_id, lock.configuration.id, judge_validation)
        if not self._judge_validation_succeeded(judge_validation):
            return self._terminal_progress(
                run_id,
                lock.configuration.id,
                "judge_validation_failed",
                judge_validation,
            )
        if through == StageKind.VALIDATE_JUDGE.value:
            return self._progress(
                run_id, lock.configuration.id, "paused", judge_validation.stage
            )

        if lock.configuration.mode is ExperimentMode.OPTIMIZE:
            if pilot:
                raise IntegrityError("pilot execution cannot enter optimization")
            cv_fits = self._cross_validation_fit_packets(run_id, lock, judge_validation)
            pending = self._execute_all(cv_fits, lock, session)
            if pending is not None:
                return self._blocked_progress(run_id, lock.configuration.id, pending)
            if through == StageKind.FIT.value:
                return self._progress(run_id, lock.configuration.id, "paused", StageKind.FIT)
            cv_solves = self._cross_validation_solve_packets(
                run_id, lock, judge_validation, cv_fits
            )
            pending = self._execute_all(cv_solves, lock, session)
            if pending is not None:
                return self._blocked_progress(run_id, lock.configuration.id, pending)
            if through == StageKind.SOLVE.value:
                return self._progress(run_id, lock.configuration.id, "paused", StageKind.SOLVE)
            cv_judges = self._judge_packets(run_id, lock, judge_validation, cv_solves)
            pending = self._execute_all(cv_judges, lock, session)
            if pending is not None:
                return self._blocked_progress(run_id, lock.configuration.id, pending)
            if through == StageKind.JUDGE.value:
                return self._progress(run_id, lock.configuration.id, "paused", StageKind.JUDGE)
            selection = self._selection_packet(
                run_id, lock, judge_validation, cv_judges
            )
            if not self._ensure_executed(selection, lock, session):
                return self._blocked_progress(run_id, lock.configuration.id, selection)
            if through == StageKind.SELECT.value:
                return self._progress(run_id, lock.configuration.id, "paused", selection.stage)
            final_fit = self._final_fit_packet(
                run_id, lock, judge_validation, selection
            )
            if not self._ensure_executed(final_fit, lock, session):
                return self._blocked_progress(run_id, lock.configuration.id, final_fit)
            solves = self._solve_packets(
                run_id,
                lock,
                judge_validation,
                selected_split="test",
                program_dependency=final_fit,
                evaluation_phase="test",
            )
        else:
            solves = self._solve_packets(
                run_id,
                lock,
                judge_validation,
                selected_split="train" if pilot else "test",
                evaluation_phase="pilot" if pilot else "test",
            )

        pending = self._execute_all(solves, lock, session)
        if pending is not None:
            return self._blocked_progress(run_id, lock.configuration.id, pending)
        if through == StageKind.SOLVE.value:
            return self._progress(run_id, lock.configuration.id, "paused", StageKind.SOLVE)
        judges = self._judge_packets(run_id, lock, judge_validation, solves)
        pending = self._execute_all(judges, lock, session)
        if pending is not None:
            return self._blocked_progress(run_id, lock.configuration.id, pending)
        if through == StageKind.JUDGE.value:
            return self._progress(run_id, lock.configuration.id, "paused", StageKind.JUDGE)
        all_judges = self._persisted_judges(run_id)
        aggregate = self._aggregate_packet(run_id, lock, judge_validation, all_judges)
        if not self._ensure_executed(aggregate, lock, session):
            return self._blocked_progress(run_id, lock.configuration.id, aggregate)
        if through == StageKind.AGGREGATE.value:
            return self._progress(run_id, lock.configuration.id, "paused", aggregate.stage)
        aggregate_status = self.ledger.status(aggregate.packet_id)
        assert aggregate_status.result is not None
        result = self.ledger.read_output(aggregate_status.result)
        assert isinstance(result, Mapping)
        return self._terminal_progress(
            run_id,
            lock.configuration.id,
            "pilot_completed" if pilot else "completed",
            aggregate,
            result=result,
        )

    def _execute_all(
        self,
        packets: Sequence[StagePacket],
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> StagePacket | None:
        if not packets:
            return None
        configurations = [self._executor(packet) for packet in packets]
        first = configurations[0]
        if (
            first.type is not ExecutorType.ENDPOINT
            or any(configuration.id != first.id for configuration in configurations)
            or first.total_concurrency <= 1
        ):
            for position, packet in enumerate(packets, start=1):
                if not self._ensure_executed(
                    packet,
                    lock,
                    session,
                    position=position,
                    total=len(packets),
                ):
                    return packet
            return None

        total = len(packets)
        maximum_workers = min(total, first.total_concurrency)

        def execute(position: int, packet: StagePacket) -> bool:
            worker_session = _ExecutionSession(self, lock, session.environment)
            return self._ensure_executed(
                packet,
                lock,
                worker_session,
                position=position,
                total=total,
            )

        failures: list[tuple[int, StagePacket]] = []
        errors: list[tuple[int, Exception]] = []
        next_index = 0
        futures: dict[Future[bool], tuple[int, StagePacket]] = {}
        stopped = False
        with ThreadPoolExecutor(
            max_workers=maximum_workers,
            thread_name_prefix=f"ms-agent-eval-{first.id}",
        ) as pool:
            while next_index < maximum_workers:
                position = next_index + 1
                packet = packets[next_index]
                futures[pool.submit(execute, position, packet)] = (position, packet)
                next_index += 1
            while futures:
                completed, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in completed:
                    position, packet = futures.pop(future)
                    try:
                        succeeded = future.result()
                    except Exception as error:
                        errors.append((position, error))
                        stopped = True
                    else:
                        if not succeeded:
                            failures.append((position, packet))
                            stopped = True
                while not stopped and next_index < total and len(futures) < maximum_workers:
                    position = next_index + 1
                    packet = packets[next_index]
                    futures[pool.submit(execute, position, packet)] = (position, packet)
                    next_index += 1
        if errors:
            raise min(errors, key=lambda item: item[0])[1]
        if failures:
            return min(failures, key=lambda item: item[0])[1]
        return None

    def _ensure_executed(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
        *,
        position: int | None = None,
        total: int | None = None,
    ) -> bool:
        status = self.ledger.status(packet.packet_id)
        if status.state is StageState.PERSISTED:
            return True
        if status.state in {StageState.FAILED, StageState.REJECTED}:
            error = status.result.error.message if status.result and status.result.error else None
            self._emit_packet(
                "packet_failed",
                packet,
                position=position,
                total=total,
                details={"error": error or status.state.value},
            )
            return False
        with self.ledger.work_lock(packet.work_id):
            cached = self.ledger.reusable_result(packet)
            if cached is not None:
                self._materialize_reused_result(
                    packet,
                    cached,
                    position=position,
                    total=total,
                )
                return True
            return (
                self._run_packet(
                    packet,
                    lock,
                    session,
                    position=position,
                    total=total,
                )
                is not None
            )

    def _materialize_reused_result(
        self,
        packet: StagePacket,
        source: StageResult,
        *,
        position: int | None,
        total: int | None,
    ) -> StageResult:
        """Attach one verified shared result to this run without a model call."""

        source_packet = self.ledger.load_packet(source.packet_id)
        source_output = _mapping(
            self.ledger.read_output(source), "reusable stage output"
        )
        rebound = self._rebind_reused_output(packet, source_output)
        claim = self.ledger.claim(
            packet.packet_id,
            executor_id=packet.executor.id,
            worker_id=f"cache-{uuid4()}",
            lease_seconds=packet.budget.wall_seconds,
        )
        now = _timestamp()
        materialized = StageResult.create(
            packet=packet,
            status="completed",
            executor_version=(
                str(source.executor["version"])
                if source.executor.get("version") is not None
                else None
            ),
            worker_id=claim.worker_id,
            outputs=(self.ledger.put_json("result", rebound),),
            usage={"model_calls": 0, "tokens": 0},
            provenance={
                "cache_hit": True,
                "reuse_policy": packet.reuse_policy,
                "source_work_id": source.work_id,
                "source_result_id": source.result_id,
                "source_attempt_id": source.attempt_id,
                "source_run_id": source_packet.run_id,
                "source_executor": dict(source.executor),
                "source_model": dict(source.model),
                "source_usage": dict(source.usage),
                "artifacts_verified": True,
            },
            started_at=now,
            completed_at=now,
            observed_model=(
                str(source.model["observed_id"])
                if source.model.get("observed_id") is not None
                else packet.model.id
            ),
        )
        self.ledger.submit(materialized, lease_id=claim.lease_id)
        self.ledger.validate(packet.packet_id, self._validate_result)
        persisted = self.ledger.persist(packet.packet_id)
        self._emit_packet(
            "packet_reused",
            packet,
            position=position,
            total=total,
            details={
                "source_result_id": source.result_id,
                "source_run_id": source_packet.run_id,
                **self._output_event_details(packet.stage, rebound),
            },
        )
        return persisted

    def _rebind_reused_output(
        self,
        packet: StagePacket,
        source: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Replace run/report metadata while preserving the cached semantic output."""

        value = dict(source)
        if packet.stage is StageKind.SOLVE:
            current = _mapping(self._input(packet, "case"), "solve input")
            for field in (
                "case_id",
                "split",
                "repetition",
                "program_hash",
                "evaluation_phase",
                "fold",
                "candidate_id",
                "candidate_k",
            ):
                value[field] = current[field]
        elif packet.stage is StageKind.JUDGE:
            current = _mapping(self._input(packet, "evaluation"), "judge input")
            value["case_id"] = current["case_id"]
            value["split"] = current["split"]
            value["repetition"] = current["repetition"]
            value["evaluation_phase"] = current["evaluation_phase"]
            value["fold"] = current["fold"]
            value["candidate_id"] = current["candidate_id"]
            value["candidate_k"] = current["candidate_k"]
        elif packet.stage is StageKind.VALIDATE_JUDGE:
            scores = _mapping(value.get("fixture_scores"), "fixture_scores")
            rebound = judge_validation_result_from_scores(
                self.workspace.judge_validation,
                {key: float(score) for key, score in scores.items()},
            )
            value = dict(json_value(rebound))
        elif packet.stage is StageKind.AGGREGATE:
            value = dict(self._execute_local(packet, self._lock_for_packet(packet)))
            execution = dict(_mapping(value.get("execution"), "aggregate execution"))
            execution["executed_this_run"] = max(
                0, int(execution.get("executed_this_run", 1)) - 1
            )
            execution["reused_this_run"] = int(
                execution.get("reused_this_run", 0)
            ) + 1
            stages = {
                key: dict(_mapping(item, f"aggregate execution stage {key}"))
                for key, item in _mapping(
                    execution.get("stages", {}), "aggregate execution stages"
                ).items()
            }
            stages["aggregate"] = {
                "executed_this_run": 0,
                "reused_this_run": 1,
            }
            execution["stages"] = stages
            value["execution"] = execution
        return value

    def _lock_for_packet(self, packet: StagePacket) -> ExperimentLock:
        lock = self.workspace.experiment_lock(packet.experiment_id)
        if lock.content_hash != packet.experiment_revision_id:
            raise IntegrityError("packet experiment revision is no longer active")
        return lock

    def _run_packet(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
        *,
        executor_id: str | None = None,
        position: int | None = None,
        total: int | None = None,
    ) -> StageResult | None:
        configuration = self._executor(packet)
        selected = executor_id or configuration.id
        if selected != configuration.id:
            raise IntegrityError("executor override does not match immutable packet identity")
        if configuration.adapter == "artifact_exchange":
            self._emit_packet(
                "packet_waiting",
                packet,
                position=position,
                total=total,
            )
            return None
        started_clock = time.monotonic()
        started = _timestamp()
        endpoint_provenance: Mapping[str, object] = {}
        claim = None
        try:
            endpoint_details: dict[str, object] = {}
            if configuration.type is ExecutorType.ENDPOINT:
                endpoint_details = {"pool_capacity": configuration.total_concurrency}
                if packet.stage is StageKind.VALIDATE_JUDGE:
                    endpoint_details["request_count"] = (
                        len(self.workspace.judge_validation.fixtures)
                        * self._judge_repetitions(packet.run_id)
                    )
            self._emit_packet(
                "packet_started",
                packet,
                position=position,
                total=total,
                details=endpoint_details,
            )
            claim = self.ledger.claim(
                packet.packet_id,
                executor_id=configuration.id,
                worker_id=f"coordinator-{uuid4()}",
                lease_seconds=packet.budget.wall_seconds,
            )
            started = _timestamp()
            output = self._automatic_output(packet, lock, session, configuration)
            assert claim is not None
            endpoint_provenance = session.endpoint_provenance(packet.packet_id)
            artifact = self.ledger.put_json("result", output.value)
            provenance = {
                **dict(output.provenance),
                **dict(session.evidence(packet.packet_id)),
                **dict(endpoint_provenance),
            }
            if (
                packet.stage is StageKind.VALIDATE_JUDGE
                and output.value.get("ordering_consistent") is not True
            ):
                provenance["cache_eligible"] = False
            result = StageResult.create(
                packet=packet,
                status="completed",
                executor_version=output.version or configuration.version,
                worker_id=claim.worker_id,
                outputs=(artifact,),
                usage=output.usage,
                provenance=provenance,
                started_at=started,
                completed_at=_timestamp(),
                observed_model=output.observed_model,
            )
        except HarnessExecutionPending:
            self._emit_packet(
                "packet_waiting",
                packet,
                position=position,
                total=total,
                duration_seconds=time.monotonic() - started_clock,
            )
            return None
        except AutomaticExecutionFailed as error:
            if claim is None:
                raise
            endpoint_provenance = session.endpoint_provenance(packet.packet_id)
            failed = StageResult.create(
                packet=packet,
                status="failed",
                executor_version=error.version or configuration.version,
                worker_id=claim.worker_id,
                usage=error.usage,
                provenance={
                    **dict(error.provenance),
                    **dict(session.evidence(packet.packet_id)),
                    **dict(endpoint_provenance),
                },
                started_at=started,
                completed_at=_timestamp(),
                observed_model=error.observed_model or packet.model.id,
                error=StageError(
                    error.kind,
                    self._redacted_error(error, session.environment),
                ),
            )
            self.ledger.submit(failed, lease_id=claim.lease_id)
            self._emit_packet(
                "packet_failed",
                packet,
                position=position,
                total=total,
                duration_seconds=time.monotonic() - started_clock,
                details={
                    "error": failed.error.message if failed.error else error.kind,
                    "error_kind": failed.error.kind if failed.error else error.kind,
                    **self._endpoint_event_details(
                        endpoint_provenance,
                        configuration,
                    ),
                },
            )
            return None
        except Exception as error:
            if claim is None:
                raise
            endpoint_provenance = session.endpoint_provenance(packet.packet_id)
            failed = StageResult.create(
                packet=packet,
                status="failed",
                executor_version=configuration.version,
                worker_id=claim.worker_id,
                usage=session.usage(packet.packet_id),
                provenance={
                    **dict(session.evidence(packet.packet_id)),
                    **dict(endpoint_provenance),
                },
                started_at=started,
                completed_at=_timestamp(),
                observed_model=packet.model.id,
                error=StageError(
                    (
                        "context_window_exceeded"
                        if isinstance(error, ContextWindowExceededError)
                        else type(error).__name__.lower()
                    ),
                    self._redacted_error(error, session.environment),
                ),
            )
            self.ledger.submit(failed, lease_id=claim.lease_id)
            self._emit_packet(
                "packet_failed",
                packet,
                position=position,
                total=total,
                duration_seconds=time.monotonic() - started_clock,
                details={
                    "error": failed.error.message if failed.error else str(error),
                    "error_kind": (
                        failed.error.kind
                        if failed.error is not None
                        else type(error).__name__.lower()
                    ),
                    **self._endpoint_event_details(
                        endpoint_provenance,
                        configuration,
                    ),
                },
            )
            return None
        self.ledger.submit(result, lease_id=claim.lease_id)
        try:
            self.ledger.validate(packet.packet_id, self._validate_result)
        except Exception as error:
            self._emit_packet(
                "packet_failed",
                packet,
                position=position,
                total=total,
                duration_seconds=time.monotonic() - started_clock,
                details={"error": str(error)},
            )
            return None
        persisted = self.ledger.persist(packet.packet_id)
        completion_endpoint_details = self._endpoint_event_details(
            endpoint_provenance,
            configuration,
        )
        self._emit_packet(
            "packet_completed",
            packet,
            position=position,
            total=total,
            duration_seconds=time.monotonic() - started_clock,
            details={
                **self._output_event_details(packet.stage, output.value),
                **completion_endpoint_details,
            },
        )
        return persisted

    def _packet_ends_run(
        self,
        packet: StagePacket,
        result: StageResult | None,
    ) -> bool:
        status = self.ledger.status(packet.packet_id)
        if result is None or status.state is not StageState.PERSISTED:
            return False
        if packet.stage is StageKind.AGGREGATE:
            return True
        if packet.stage is StageKind.VALIDATE_JUDGE:
            output = _mapping(self.ledger.read_output(result), "judge validation output")
            return output.get("ordering_consistent") is False
        return False

    def _finalize_run(
        self,
        run_id: str,
        *,
        packet: StagePacket | None = None,
    ) -> PublishedReport:
        report = collect_artifact_garbage(
            self.artifacts,
            delete=True,
        )
        blobs = _mapping(report.get("blobs"), "garbage collection blobs")
        temporary = _mapping(report.get("temporary"), "garbage collection temporary")
        cleanup_details = {
            "deleted_blobs": blobs.get("deleted_count", 0),
            "deleted_blob_bytes": blobs.get("deleted_bytes", 0),
            "deleted_temporary": temporary.get("deleted_count", 0),
            "deleted_temporary_bytes": temporary.get("deleted_bytes", 0),
        }
        if packet is not None:
            self._emit_packet("cleanup_completed", packet, details=cleanup_details)
        published = self.build_report(run_id, replace=True)
        run = self.ledger.load_run(run_id)
        self._emit(
            "report_completed",
            run_id,
            _string(run.get("experiment_id"), "run experiment_id"),
            stage=None if packet is None else packet.stage.value,
            packet_id=None if packet is None else packet.packet_id,
            details=dict(published.as_dict()),
        )
        return published

    def _blocked_progress(
        self,
        run_id: str,
        experiment_id: str,
        packet: StagePacket,
    ) -> RunProgress:
        packet_status = self.ledger.status(packet.packet_id)
        if packet_status.state in {StageState.FAILED, StageState.REJECTED}:
            error = packet_status.result.error if packet_status.result is not None else None
            return self._progress(
                run_id,
                experiment_id,
                "execution_failed",
                packet.stage,
                result={
                    "packet_id": packet.packet_id,
                    "error_kind": error.kind if error is not None else packet_status.state.value,
                    "error": error.message if error is not None else packet_status.state.value,
                },
            )
        return self._progress(run_id, experiment_id, "awaiting_execution", packet.stage)

    def _terminal_progress(
        self,
        run_id: str,
        experiment_id: str,
        status: str,
        packet: StagePacket,
        *,
        result: Mapping[str, object] | None = None,
    ) -> RunProgress:
        try:
            published = self._finalize_run(run_id, packet=packet)
        except AgentEvalError as error:
            return self._progress(
                run_id,
                experiment_id,
                "report_failed",
                packet.stage,
                result={
                    **dict(result or {}),
                    "report_error": str(error),
                },
            )
        return self._progress(
            run_id,
            experiment_id,
            status,
            packet.stage,
            result={
                **dict(result or {}),
                "report": dict(published.as_dict()),
            },
        )

    def _emit_packet(
        self,
        kind: str,
        packet: StagePacket,
        *,
        position: int | None = None,
        total: int | None = None,
        duration_seconds: float | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        context = self._packet_event_context(packet)
        context_details = context.pop("details", {})
        assert isinstance(context_details, Mapping)
        self._emit(
            kind,
            packet.run_id,
            packet.experiment_id,
            stage=packet.stage.value,
            packet_id=packet.packet_id,
            position=position,
            total=total,
            model=packet.model.id,
            executor=packet.executor.id,
            duration_seconds=duration_seconds,
            details={**context_details, **dict(details or {})},
            **context,
        )

    def _emit(
        self,
        kind: str,
        run_id: str,
        experiment_id: str,
        *,
        stage: str | None = None,
        packet_id: str | None = None,
        work_id: str | None = None,
        attempt_id: str | None = None,
        experiment_revision_id: str | None = None,
        case_id: str | None = None,
        case_title: str | None = None,
        skill: str | None = None,
        split: str | None = None,
        repetition: int | None = None,
        position: int | None = None,
        total: int | None = None,
        model: str | None = None,
        executor: str | None = None,
        duration_seconds: float | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if self.progress_sink is None:
            return
        with self._progress_lock:
            self.progress_sink(
                ProgressEvent(
                    kind=kind,
                    run_id=run_id,
                    experiment_id=experiment_id,
                    stage=stage,
                    packet_id=packet_id,
                    work_id=work_id,
                    attempt_id=attempt_id,
                    experiment_revision_id=experiment_revision_id,
                    case_id=case_id,
                    case_title=case_title,
                    skill=skill,
                    split=split,
                    repetition=repetition,
                    position=position,
                    total=total,
                    model=model,
                    executor=executor,
                    duration_seconds=duration_seconds,
                    details=dict(details or {}),
                )
            )

    def _packet_event_context(self, packet: StagePacket) -> dict[str, object]:
        input_id = {
            StageKind.SOLVE: "case",
            StageKind.JUDGE: "evaluation",
        }.get(packet.stage)
        if input_id is not None:
            value = _mapping(self._input(packet, input_id), f"{packet.stage.value} input")
            case_id = _string(value.get("case_id"), f"{packet.stage.value} case_id")
            case = next(
                (item for item in self.workspace.case_bank.cases if item.id == case_id),
                None,
            )
            return {
                "case_id": case_id,
                "case_title": None if case is None else case.title,
                "skill": value.get("skill") if isinstance(value.get("skill"), str) else (
                    None if case is None else case.skill
                ),
                "split": value.get("split") if isinstance(value.get("split"), str) else None,
                "repetition": (
                    value.get("repetition")
                    if isinstance(value.get("repetition"), int)
                    and not isinstance(value.get("repetition"), bool)
                    else None
                ),
            }
        if packet.stage is StageKind.VALIDATE_JUDGE:
            value = _mapping(self._input(packet, "judge_validation"), "judge_validation input")
            fixtures = value.get("fixtures")
            return {
                "details": {
                    "fixture_count": len(fixtures) if isinstance(fixtures, list) else 0
                }
            }
        if packet.stage is StageKind.FIT:
            value = _mapping(self._input(packet, "training_set"), "fit input")
            cases = value.get("cases")
            return {
                "details": {"training_case_count": len(cases) if isinstance(cases, list) else 0}
            }
        return {}

    def _run_case_count(self, mode: ExperimentMode, *, pilot: bool) -> int:
        del mode
        selected_splits = {"train"} if pilot else {"test"}
        return sum(
            self.workspace.splits.split_for(case) in selected_splits
            for case in self.workspace.case_bank.cases
        )

    @staticmethod
    def _output_event_details(
        stage: StageKind,
        output: Mapping[str, object],
    ) -> Mapping[str, object]:
        if stage is StageKind.AUTHOR:
            packages = output.get("candidate_packages")
            if isinstance(packages, Sequence) and not isinstance(packages, (str, bytes)):
                case_ids = [
                    item.get("id")
                    for item in packages
                    if isinstance(item, Mapping) and isinstance(item.get("id"), str)
                ]
                return {"produced_count": len(packages), "case_ids": case_ids}
        if stage is StageKind.VALIDATE_JUDGE:
            return {"ordering_consistent": output.get("ordering_consistent") is True}
        if stage is StageKind.JUDGE:
            return {"score": output.get("score")}
        if stage is StageKind.AGGREGATE:
            return {
                key: output[key]
                for key in ("case_count", "mean_score", "diagnostic_only")
                if key in output
            }
        if stage is StageKind.FIT:
            return {
                "candidate_id": output.get("candidate_id"),
                "phase": output.get("phase"),
                "fold": output.get("fold"),
            }
        if stage is StageKind.SELECT:
            return {
                "selected_candidate_id": output.get("selected_candidate_id"),
                "selected_mean_score": output.get("selected_mean_score"),
            }
        return {}

    @staticmethod
    def _endpoint_event_details(
        provenance: Mapping[str, object],
        configuration: ExecutorConfiguration,
    ) -> Mapping[str, object]:
        if configuration.type is not ExecutorType.ENDPOINT:
            return {}
        raw_pool = provenance.get("endpoint_pool")
        if not isinstance(raw_pool, Mapping):
            return {"pool_capacity": configuration.total_concurrency}
        endpoint_id = raw_pool.get("endpoint_id")
        if isinstance(endpoint_id, str):
            preflight = raw_pool.get("last_preflight")
            return {
                "endpoint_id": endpoint_id,
                "pool_capacity": configuration.total_concurrency,
                "preflight_status": (
                    preflight.get("status")
                    if isinstance(preflight, Mapping)
                    else None
                ),
                **(
                    {
                        key: preflight.get(key)
                        for key in (
                            "reason",
                            "context_window",
                            "input_tokens",
                            "maximum_output_tokens",
                            "required_tokens",
                        )
                    }
                    if isinstance(preflight, Mapping)
                    else {}
                ),
            }
        routes = raw_pool.get("routes")
        endpoint_ids = (
            [
                route["endpoint_id"]
                for route in routes
                if isinstance(route, Mapping)
                and isinstance(route.get("endpoint_id"), str)
            ]
            if isinstance(routes, Sequence)
            and not isinstance(routes, (str, bytes, bytearray))
            else []
        )
        return {
            "pool_capacity": configuration.total_concurrency,
            "endpoint_ids": endpoint_ids,
            "preflight_rejections": raw_pool.get("preflight_rejections", 0),
            "preflight_skips": raw_pool.get("preflight_skips", 0),
        }

    def _automatic_output(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
        executor: ExecutorConfiguration,
    ) -> ExecutorOutput:
        if executor.adapter == "codex_cli":
            from ms_agent_eval.adapters.codex_cli import CodexCliExecutor

            return CodexCliExecutor(self.workspace, lock, executor).execute(
                packet, self.ledger
            )
        if executor.adapter == "local":
            return ExecutorOutput(
                self._execute_local(packet, lock), {}, executor.version, None
            )
        if executor.adapter != "openai_compatible":
            raise ConfigurationError(f"unsupported executor adapter: {executor.adapter}")
        if packet.stage is StageKind.AUTHOR:
            return self._execute_author(packet, session)
        if packet.stage is StageKind.VALIDATE_JUDGE:
            return self._execute_judge_validation(packet, lock, session)
        if packet.stage is StageKind.SOLVE:
            return self._execute_solve(packet, lock, session)
        if packet.stage is StageKind.JUDGE:
            return self._execute_judge(packet, lock, session)
        raise ConfigurationError(
            f"openai_compatible cannot automatically execute {packet.stage.value}"
        )

    def _execute_author(
        self, packet: StagePacket, session: _ExecutionSession
    ) -> ExecutorOutput:
        authoring_context = _mapping(self._input(packet, "authoring_context"), "author input")
        program = create_author_program()
        lm, observer = session.lm("author", packet)
        result = self.dspy.execute(
            contract=DspyExecutionContract(
                role="author",
                program_hash=program_hash(program),
                inputs={
                    "authoring_context": json.dumps(
                        authoring_context, ensure_ascii=False, sort_keys=True
                    )
                },
                required_outputs=("candidate_packages",),
                primary_output=None,
            ),
            program=program,
            lm=lm,
            observer=observer,
        )
        if result.status != "completed":
            raise _dspy_execution_failure("author", result)
        packages = _list(result.outputs.get("candidate_packages"), "candidate_packages")
        return ExecutorOutput(
            {"candidate_packages": list(packages)},
            _usage(observer.records),
            None,
            packet.model.id,
        )

    def _execute_judge_validation(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> ExecutorOutput:
        configuration = self._executor(packet)
        if (
            configuration.type is ExecutorType.ENDPOINT
            and configuration.total_concurrency > 1
        ):
            return self._execute_parallel_judge_validation(
                packet,
                lock,
                session,
                configuration,
            )
        judge, observer = self._judge(
            lock,
            session,
            packet,
            repetitions=self._judge_repetitions(packet.run_id),
        )
        start = len(observer.records)
        result = validate_judge(
            judge,
            self.workspace.judge_validation,
            self.workspace.case_bank,
            self.workspace.context.skill,
        )
        value = json_value(result)
        assert isinstance(value, Mapping)
        return ExecutorOutput(
            value,
            _usage(observer.records[start:]),
            None,
            lock.models["judge"].model,
        )

    def _execute_parallel_judge_validation(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
        configuration: ExecutorConfiguration,
    ) -> ExecutorOutput:
        repetitions = self._judge_repetitions(packet.run_id)
        cases = {case.id: case for case in self.workspace.case_bank.cases}
        jobs: list[
            tuple[int, JudgeValidationFixture, CaseDefinition, str, str, int]
        ] = []
        position = 0
        for fixture in self.workspace.judge_validation.fixtures:
            case = cases[fixture.case_id]
            candidate_response = fixture.candidate_path.read_text(encoding="utf-8")
            skill_context = self.workspace.context.skill(case.skill)
            for repetition in range(1, repetitions + 1):
                position += 1
                jobs.append(
                    (
                        position,
                        fixture,
                        case,
                        candidate_response,
                        skill_context,
                        repetition,
                    )
                )

        progress_lock = Lock()
        completed = 0

        def execute_vote(
            job: tuple[int, JudgeValidationFixture, CaseDefinition, str, str, int],
        ) -> _JudgeValidationVoteExecution:
            nonlocal completed
            job_position, fixture, case, candidate, context, repetition = job
            started = time.monotonic()
            observer: ModelCallObserver | None = None
            vote: JudgeVote | None = None
            error: Exception | None = None
            try:
                judge, observer = self._judge(
                    lock,
                    session,
                    packet,
                    repetitions=1,
                )
                vote = judge.evaluate_vote(
                    case,
                    candidate,
                    skill_context=context,
                )
            except Exception as caught:
                error = caught
            duration = time.monotonic() - started
            usage = _usage(observer.records) if observer is not None else _usage(())
            endpoint_id = None
            preflight_status = None
            if observer is not None and observer.endpoint_routes:
                last_route = observer.endpoint_routes[-1]
                value = last_route.get("endpoint_id")
                endpoint_id = value if isinstance(value, str) else None
                preflight = last_route.get("preflight")
                if isinstance(preflight, Mapping):
                    value = preflight.get("status")
                    preflight_status = value if isinstance(value, str) else None
            with progress_lock:
                completed += 1
                completion_position = completed
            fixture_id = fixture.id
            self._emit_packet(
                (
                    "judge_validation_vote_completed"
                    if error is None
                    else "judge_validation_vote_failed"
                ),
                packet,
                position=completion_position,
                total=len(jobs),
                duration_seconds=duration,
                details={
                    "fixture_id": fixture_id,
                    "vote": repetition,
                    "endpoint_id": endpoint_id,
                    "preflight_status": preflight_status,
                    "request_position": job_position,
                    **(
                        {
                            "error": self._redacted_error(
                                error,
                                session.environment,
                            )
                        }
                        if error is not None
                        else {}
                    ),
                },
            )
            return _JudgeValidationVoteExecution(
                job_position,
                fixture_id,
                repetition,
                vote,
                endpoint_id,
                usage,
                duration,
                error,
            )

        maximum_workers = min(len(jobs), configuration.total_concurrency)
        with ThreadPoolExecutor(
            max_workers=maximum_workers,
            thread_name_prefix=f"ms-agent-eval-{configuration.id}-judge-validation",
        ) as workers:
            next_index = 0
            futures: dict[Future[_JudgeValidationVoteExecution], int] = {}
            completed_executions: list[_JudgeValidationVoteExecution] = []
            stopped = False
            while next_index < maximum_workers:
                future = workers.submit(execute_vote, jobs[next_index])
                futures[future] = next_index
                next_index += 1
            while futures:
                finished, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in finished:
                    futures.pop(future)
                    execution = future.result()
                    completed_executions.append(execution)
                    if execution.error is not None:
                        stopped = True
                while (
                    not stopped
                    and next_index < len(jobs)
                    and len(futures) < maximum_workers
                ):
                    future = workers.submit(execute_vote, jobs[next_index])
                    futures[future] = next_index
                    next_index += 1
            executions = tuple(
                sorted(completed_executions, key=lambda item: item.position)
            )

        provenance = session.endpoint_provenance(packet.packet_id)
        failed = next(
            (item for item in sorted(executions, key=lambda item: item.position) if item.error),
            None,
        )
        if failed is not None:
            error = failed.error
            assert error is not None
            if isinstance(error, AutomaticExecutionFailed):
                raise AutomaticExecutionFailed(
                    error.kind,
                    str(error),
                    usage=session.usage(packet.packet_id),
                    provenance=provenance,
                    version=error.version,
                    observed_model=error.observed_model,
                )
            raise AutomaticExecutionFailed(
                "judge_validation_error",
                self._redacted_error(error, session.environment),
                usage=session.usage(packet.packet_id),
                provenance=provenance,
                observed_model=lock.models["judge"].model,
            )

        by_fixture: dict[str, list[JudgeVote]] = {}
        for execution in sorted(executions, key=lambda item: item.position):
            assert execution.vote is not None
            by_fixture.setdefault(execution.fixture_id, []).append(execution.vote)
        fixture_scores: dict[str, float] = {}
        for fixture in self.workspace.judge_validation.fixtures:
            case = cases[fixture.case_id]
            evaluation = aggregate_judge_votes(
                case,
                by_fixture[fixture.id],
                model_hash=lock.models["judge"].content_hash,
                program_hash=lock.judge_program_hash,
            )
            fixture_scores[fixture.id] = evaluation.score
        result = judge_validation_result_from_scores(
            self.workspace.judge_validation,
            fixture_scores,
        )
        value = json_value(result)
        assert isinstance(value, Mapping)
        return ExecutorOutput(
            value,
            session.usage(packet.packet_id),
            None,
            lock.models["judge"].model,
            provenance=provenance,
        )

    def _execute_solve(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> ExecutorOutput:
        values = _mapping(self._input(packet, "case"), "solve input")
        program = create_solver_program()
        fitted = next((item for item in packet.inputs if item.id == "fitted_state"), None)
        if fitted is not None:
            reference = self.artifacts.reference(fitted.content_id, fitted.media_type)
            with tempfile.TemporaryDirectory() as directory:
                state_file = Path(directory) / "state.json"
                with self.artifacts.get_blob(reference) as source:
                    state_file.write_bytes(source.read())
                load_state_json(program, state_file)
        lm, observer = session.lm("solver", packet)
        start = len(observer.records)
        result = self.dspy.execute(
            contract=DspyExecutionContract(
                role="solver",
                program_hash=program_hash(program),
                inputs={
                    "global_context": values["global_context"],
                    "skill_context": values["skill_context"],
                    "task": values["task"],
                },
                required_outputs=("response",),
                primary_output="response",
            ),
            program=program,
            lm=lm,
            observer=observer,
        )
        if result.status != "completed" or result.primary_response is None:
            raise _dspy_execution_failure("solver", result)
        return ExecutorOutput(
            {
                "case_id": values["case_id"],
                "split": values["split"],
                "repetition": values["repetition"],
                "response": result.primary_response,
                "program_hash": program_hash(program),
                "evaluation_phase": values["evaluation_phase"],
                "fold": values["fold"],
                "candidate_id": values["candidate_id"],
                "candidate_k": values["candidate_k"],
                "call_ids": [item.call_id for item in result.calls],
            },
            _usage(observer.records[start:]),
            None,
            lock.models["solver"].model,
        )

    def _execute_judge(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> ExecutorOutput:
        values = _mapping(self._input(packet, "evaluation"), "judge input")
        case_id = _string(values.get("case_id"), "judge case_id")
        case = next((item for item in self.workspace.case_bank.cases if item.id == case_id), None)
        if case is None:
            raise IntegrityError(f"judge packet references unknown case {case_id!r}")
        judge, observer = self._judge(
            lock,
            session,
            packet,
            repetitions=self._judge_repetitions(packet.run_id),
        )
        start = len(observer.records)
        result = judge.evaluate(
            case,
            _string(values.get("candidate_response"), "candidate response"),
            skill_context=_string(values.get("skill_context"), "skill context"),
        )
        value = json_value(result)
        assert isinstance(value, Mapping)
        return ExecutorOutput(
            {
                **value,
                "split": values["split"],
                "repetition": values["repetition"],
                "evaluation_phase": values["evaluation_phase"],
                "fold": values["fold"],
                "candidate_id": values["candidate_id"],
                "candidate_k": values["candidate_k"],
            },
            _usage(observer.records[start:]),
            None,
            lock.models["judge"].model,
        )

    def _execute_local(
        self, packet: StagePacket, lock: ExperimentLock
    ) -> Mapping[str, object]:
        if packet.stage is StageKind.FIT:
            experiment = lock.configuration
            if experiment.optimizer is None:
                raise IntegrityError("fit stage has no optimizer")
            configuration = _mapping(packet.work_spec.get("candidate"), "fit candidate")
            raw_k = _integer(configuration.get("k"), "fit candidate k")
            candidate_id = _string(configuration.get("id"), "fit candidate id")
            phase = _string(packet.work_spec.get("phase"), "fit phase")
            raw_fold = packet.work_spec.get("fold")
            fold = None if raw_fold is None else _integer(raw_fold, "fit fold")
            cross_validation = experiment.cross_validation
            if cross_validation is None:
                raise IntegrityError("fit stage has no cross-validation configuration")
            training_input = _mapping(self._input(packet, "training_set"), "training set")
            training_cases = _list(training_input.get("cases"), "training cases")
            if raw_k > len(training_cases):
                raise PreflightError(
                    f"LabeledFewShot k={raw_k} exceeds the {len(training_cases)} fit cases"
                )
            train = [
                dspy.Example(
                    global_context=_string(case.get("global_context"), "global context"),
                    skill_context=_string(case.get("skill_context"), "skill context"),
                    task=_string(case.get("task"), "training task"),
                    response=_string(case.get("expected_response"), "training response"),
                ).with_inputs("global_context", "skill_context", "task")
                for raw_case in training_cases
                for case in [_mapping(raw_case, "training case")]
            ]
            compiled = create_solver_program()
            previous = random.getstate()
            try:
                random.seed(f"{cross_validation.seed}:{candidate_id}:{fold}")
                if raw_k:
                    compiled = dspy.LabeledFewShot(k=raw_k).compile(
                        compiled, trainset=train, sample=True
                    )
            finally:
                random.setstate(previous)
            state = self.dspy.save_state_artifact(compiled)
            return {
                "state_artifact": json_value(state),
                "fitted_program_hash": program_hash(compiled),
                "training_case_hash": content_hash(packet.work_spec.get("training_cases")),
                "fold_assignment_hash": _string(
                    packet.work_spec.get("fold_assignment_hash"), "fold assignment hash"
                ),
                "candidate_id": candidate_id,
                "candidate_k": raw_k,
                "phase": phase,
                "fold": fold,
                "state_format": "json",
            }
        if packet.stage is StageKind.SELECT:
            cross_validation = _mapping(
                self._input(packet, "cross_validation"), "cross-validation input"
            )
            raw_results = _list(cross_validation.get("results"), "cross-validation results")
            by_candidate: dict[str, list[Mapping[str, object]]] = {}
            for raw_result in raw_results:
                result = _mapping(raw_result, "cross-validation result")
                candidate_id = _string(result.get("candidate_id"), "candidate id")
                by_candidate.setdefault(candidate_id, []).append(result)
            expected_cases = {
                _string(case_id, "training case id")
                for case_id in _list(
                    cross_validation.get("training_case_ids"), "training case ids"
                )
            }
            summaries = []
            for candidate_id, results in sorted(by_candidate.items()):
                case_ids = [_string(item.get("case_id"), "case id") for item in results]
                if len(case_ids) != len(set(case_ids)) or set(case_ids) != expected_cases:
                    raise IntegrityError(
                        f"candidate {candidate_id!r} must have exactly one out-of-fold score per training case"
                    )
                k_values = {_integer(item.get("k"), "candidate k") for item in results}
                if len(k_values) != 1:
                    raise IntegrityError("candidate cross-validation results disagree on k")
                scores = [_number(item.get("score"), "cross-validation score") for item in results]
                summaries.append(
                    {
                        "candidate_id": candidate_id,
                        "k": next(iter(k_values)),
                        "mean_score": sum(scores) / len(scores),
                        "case_count": len(scores),
                    }
                )
            if not summaries:
                raise IntegrityError("cross-validation selection requires candidate results")
            selected = min(
                summaries,
                key=lambda item: (
                    -float(item["mean_score"]),
                    int(item["k"]),
                    str(item["candidate_id"]),
                ),
            )
            return {
                "metric": "mean_score",
                "fold_assignment_hash": _string(
                    cross_validation.get("fold_assignment_hash"), "fold assignment hash"
                ),
                "candidates": summaries,
                "selected_candidate_id": selected["candidate_id"],
                "selected_k": selected["k"],
                "selected_mean_score": selected["mean_score"],
            }
        if packet.stage is StageKind.AGGREGATE:
            cases = []
            graph_nodes = []
            execution_by_stage: dict[str, Counter[str]] = {}
            workspace = self.workspace.repository.workspace
            endpoint_execution: dict[str, dict[str, object]] = {
                f"{executor.id}/{endpoint.id}": {
                    "executor_id": executor.id,
                    "endpoint_id": endpoint.id,
                    "base_url": endpoint.base_url,
                    "pool_hash": executor.pool_hash,
                    "endpoint_configuration_hash": endpoint.configuration_hash,
                    "configured_capacity": endpoint.max_concurrent_requests,
                    "packets": 0,
                    "requests": 0,
                    "preflight_checks": 0,
                    "preflight_rejections": 0,
                    "preflight_skips": 0,
                    "model_calls": 0,
                    "completed_calls": 0,
                    "failed_calls": 0,
                    "latency_seconds": 0.0,
                    "queue_wait_seconds": 0.0,
                    "peak_observed_endpoint_concurrency": 0,
                    "peak_observed_pool_concurrency": 0,
                }
                for executor in workspace.executors.values()
                if executor.type is ExecutorType.ENDPOINT
                for endpoint in executor.endpoints
            }
            role_usage: dict[str, Counter[str]] = {
                "solver": Counter(),
                "judge": Counter(),
            }
            for status in self.ledger.list(packet.run_id):
                if status.state is not StageState.PERSISTED or status.result is None:
                    continue
                graph_nodes.append(
                    {
                        "stage": status.packet.stage.value,
                        "work_id": status.packet.work_id,
                        "result_id": status.result.result_id,
                    }
                )
                disposition = (
                    "reused_this_run"
                    if status.result.provenance.get("cache_hit") is True
                    else "executed_this_run"
                )
                execution_by_stage.setdefault(
                    status.packet.stage.value, Counter()
                )[disposition] += 1
                raw_endpoint = status.result.provenance.get("endpoint_pool")
                if isinstance(raw_endpoint, Mapping):
                    for route in endpoint_provenance_routes(
                        raw_endpoint,
                        status.result.usage,
                    ):
                        endpoint_id = _string(
                            route.get("endpoint_id"), "endpoint provenance id"
                        )
                        key = f"{status.packet.executor.id}/{endpoint_id}"
                        endpoint = endpoint_execution.setdefault(
                            key,
                            {
                                "executor_id": status.packet.executor.id,
                                "endpoint_id": endpoint_id,
                                "pool_hash": raw_endpoint.get("pool_hash"),
                                "endpoint_configuration_hash": route.get(
                                    "endpoint_configuration_hash"
                                ),
                                "configured_capacity": route.get(
                                    "max_concurrent_requests"
                                ),
                                "packets": 0,
                                "requests": 0,
                                "preflight_checks": 0,
                                "preflight_rejections": 0,
                                "preflight_skips": 0,
                                "model_calls": 0,
                                "completed_calls": 0,
                                "failed_calls": 0,
                                "latency_seconds": 0.0,
                                "queue_wait_seconds": 0.0,
                                "peak_observed_endpoint_concurrency": 0,
                                "peak_observed_pool_concurrency": 0,
                            },
                        )
                        requests = route.get("requests", 0)
                        if isinstance(requests, int) and not isinstance(requests, bool):
                            endpoint["requests"] = int(endpoint["requests"]) + requests
                            if requests > 0:
                                endpoint["packets"] = int(endpoint["packets"]) + 1
                        for preflight_key in (
                            "preflight_checks",
                            "preflight_rejections",
                            "preflight_skips",
                        ):
                            raw_count = route.get(preflight_key, 0)
                            if isinstance(raw_count, int) and not isinstance(
                                raw_count, bool
                            ):
                                endpoint[preflight_key] = (
                                    int(endpoint[preflight_key]) + raw_count
                                )
                        for usage_key in (
                            "model_calls",
                            "completed_calls",
                            "failed_calls",
                        ):
                            raw_usage = route.get(usage_key, 0)
                            if isinstance(raw_usage, int) and not isinstance(
                                raw_usage, bool
                            ):
                                endpoint[usage_key] = int(endpoint[usage_key]) + raw_usage
                        for duration_key in (
                            "latency_seconds",
                            "queue_wait_seconds",
                        ):
                            raw_duration = route.get(duration_key, 0.0)
                            if isinstance(raw_duration, (int, float)) and not isinstance(
                                raw_duration, bool
                            ):
                                endpoint[duration_key] = float(
                                    endpoint[duration_key]
                                ) + float(raw_duration)
                        for peak_key in (
                            "peak_observed_endpoint_concurrency",
                            "peak_observed_pool_concurrency",
                        ):
                            raw_peak = route.get(peak_key, 0)
                            if isinstance(raw_peak, int) and not isinstance(raw_peak, bool):
                                endpoint[peak_key] = max(
                                    int(endpoint[peak_key]),
                                    raw_peak,
                                )
                role = None
                if status.packet.stage is StageKind.SOLVE:
                    role = "solver"
                elif status.packet.stage in {StageKind.VALIDATE_JUDGE, StageKind.JUDGE}:
                    role = "judge"
                if status.packet.stage is StageKind.JUDGE:
                    evaluation = _mapping(
                        self.ledger.read_output(status.result), "judge output"
                    )
                    if evaluation.get("evaluation_phase") in {"test", "pilot"}:
                        cases.append(dict(evaluation))
                if role is not None:
                    for key, value in status.result.usage.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            role_usage[role][key] += value
            run = self.ledger.load_run(packet.run_id)
            execution_mode = _execution_mode(run)
            value: dict[str, object] = {
                "schema_version": 3,
                "run_id": packet.run_id,
                "experiment_id": packet.experiment_id,
                "lock_hash": lock.content_hash,
                "run_graph_id": content_hash(
                    {
                        "experiment_revision_id": lock.content_hash,
                        "nodes": sorted(
                            graph_nodes,
                            key=lambda item: (
                                str(item["stage"]),
                                str(item["work_id"]),
                                str(item["result_id"]),
                            ),
                        ),
                        "terminal_work_id": packet.work_id,
                    }
                ),
                "mode": lock.mode,
                "execution_mode": execution_mode,
                "benchmark_valid": execution_mode == "benchmark",
                "coverage_status": run.get("coverage_status", "unknown"),
                "evaluated_splits": sorted(
                    {
                        str(item["split"])
                        for item in cases
                        if isinstance(item.get("split"), str)
                    }
                ),
                "cases": cases,
                "case_count": len(cases),
                "role_usage": {
                    role: dict(values) for role, values in role_usage.items()
                },
                "execution": {
                    "executed_this_run": sum(
                        values["executed_this_run"]
                        for values in execution_by_stage.values()
                    )
                    + 1,
                    "reused_this_run": sum(
                        values["reused_this_run"]
                        for values in execution_by_stage.values()
                    ),
                    "stages": {
                        **{
                            stage: dict(values)
                            for stage, values in sorted(execution_by_stage.items())
                        },
                        "aggregate": {
                            "executed_this_run": 1,
                            "reused_this_run": 0,
                        },
                    },
                },
                "endpoint_execution": {
                    "configured_capacity": sum(
                        executor.total_concurrency
                        for executor in workspace.executors.values()
                        if executor.type is ExecutorType.ENDPOINT
                    ),
                    "endpoints": [
                        endpoint_execution[key] for key in sorted(endpoint_execution)
                    ],
                },
                "independent": (
                    workspace.stages["solve"].executor
                    != workspace.stages["judge"].executor
                ),
            }
            if execution_mode == "benchmark":
                value["mean_score"] = (
                    sum(float(item["score"]) for item in cases) / len(cases)
                    if cases
                    else 0.0
                )
            else:
                value["diagnostic_only"] = True
                value["benchmark_metrics"] = None
            return value
        raise ConfigurationError(f"local executor cannot execute {packet.stage.value}")

    def _judge(
        self,
        lock: ExperimentLock,
        session: _ExecutionSession,
        packet: StagePacket,
        *,
        repetitions: int | None = None,
    ) -> tuple[LlmJudge, ModelCallObserver]:
        lm, observer = session.lm("judge", packet)
        program = create_judge_program()

        def observed(**inputs: object) -> Mapping[str, object]:
            result = self.dspy.execute(
                contract=DspyExecutionContract(
                    role="judge",
                    program_hash=program_hash(program),
                    inputs=inputs,
                    required_outputs=("criterion_scores", "hard_failures", "feedback"),
                    primary_output=None,
                ),
                program=program,
                lm=lm,
                observer=observer,
            )
            if result.status != "completed":
                raise _dspy_execution_failure("judge", result)
            return {**result.outputs, "call_ids": [item.call_id for item in result.calls]}

        return (
            LlmJudge(
                observed,
                model_hash=lock.models["judge"].content_hash,
                program_hash=lock.judge_program_hash,
                repetitions=(
                    repetitions
                    if repetitions is not None
                    else self.workspace.repository.workspace.evaluation.judge.repetitions
                ),
            ),
            observer,
        )

    def _judge_repetitions(self, run_id: str) -> int:
        return (
            1
            if _execution_mode(self.ledger.load_run(run_id)) == "pilot"
            else self.workspace.repository.workspace.evaluation.judge.repetitions
        )

    def _author_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        *,
        campaign: str | None,
    ) -> StagePacket:
        value = {
            "authoring_instructions": self.workspace.repository.case_authoring_instructions(),
            "capability_catalog": json_value(self.workspace.capability_catalog),
            "global_context": self.workspace.context.global_context,
            "skill_contexts": dict(self.workspace.context.skill_contexts),
            "source_evidence": self.workspace.context.supporting_source(
                "case-authoring", campaign or "complete capability coverage"
            ),
            "source_snapshot_hash": self.workspace.context.snapshot_hash,
            "campaign": campaign or "Author the next complete, grounded case campaign.",
        }
        return self._packet(
            run_id,
            lock,
            StageKind.AUTHOR,
            inputs=(self.ledger.put_json("authoring_context", value),),
            instructions={
                "task": "Produce candidate case packages for explicit review.",
                "output": {"candidate_packages": "list[case-package]"},
            },
            dependencies=(),
            output_schema="authored-cases-v1",
        )

    def _judge_validation_packet(self, run_id: str, lock: ExperimentLock) -> StagePacket:
        cases = {item.id: item for item in self.workspace.case_bank.cases}
        fixtures = []
        semantic_fixtures = []
        for fixture in self.workspace.judge_validation.fixtures:
            case = cases[fixture.case_id]
            candidate_response = fixture.candidate_path.read_text(encoding="utf-8")
            judge_case = self._judge_case_input(case)
            fixtures.append(
                {
                    "id": fixture.id,
                    "candidate_response": candidate_response,
                    "case": judge_case,
                }
            )
            semantic_fixtures.append(
                {
                    "id": fixture.id,
                    "candidate_response_hash": content_hash(candidate_response),
                    "judge_view": {
                        key: value
                        for key, value in judge_case.items()
                        if key not in {"case_id", "case_hash"}
                    },
                }
            )
        judge_validation_input = self.ledger.put_json(
            "judge_validation",
            {
                "corpus_hash": self.workspace.judge_validation.content_hash,
                "fixtures": fixtures,
            },
        )
        return self._packet(
            run_id,
            lock,
            StageKind.VALIDATE_JUDGE,
            inputs=(judge_validation_input,),
            instructions={
                "task": "Score every locked anchor response for ordinal judge validation.",
                "output": "JudgeValidationResult",
            },
            dependencies=(),
            output_schema="judge-validation-v1",
            work_spec={
                "fixtures": semantic_fixtures,
                "judge_program_hash": lock.judge_program_hash,
                "repetitions": self._judge_repetitions(run_id),
            },
        )

    def _candidate_configurations(
        self, lock: ExperimentLock
    ) -> tuple[Mapping[str, object], ...]:
        optimizer = lock.configuration.optimizer
        if optimizer is None:
            raise IntegrityError("optimization experiment has no optimizer")
        return (
            {"id": "baseline", "k": 0},
            *(
                {"id": f"labeled-few-shot-k-{k}", "k": k}
                for k in optimizer.k_values
            ),
        )

    def _fold_assignment(
        self, lock: ExperimentLock
    ) -> tuple[Mapping[str, int], str]:
        cross_validation = lock.configuration.cross_validation
        if cross_validation is None:
            raise IntegrityError("optimization experiment has no cross-validation configuration")
        train = self._cases_by_split()["train"]
        assignments = deterministic_group_k_fold(
            train,
            folds=cross_validation.folds,
            seed=cross_validation.seed,
        )
        mapping = {item.group: item.fold for item in assignments}
        return mapping, content_hash(assignments)

    def _fit_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        *,
        cases: Sequence[object],
        candidate: Mapping[str, object],
        phase: str,
        fold: int | None,
        fold_assignment_hash: str,
        selection: StagePacket | None = None,
    ) -> StagePacket:
        train = [
            {
                "case_id": case.id,
                "split": "train",
                "global_context": self.workspace.context.global_context,
                "skill_context": self.workspace.context.skill(case.skill),
                "task": case.prompt,
                "expected_response": case.expected_response,
            }
            for case in cases
        ]
        dependencies = [judge_validation.packet_id]
        work_dependencies: tuple[str, ...] = ()
        if selection is not None:
            dependencies.append(selection.packet_id)
            selection_status = self.ledger.status(selection.packet_id)
            assert selection_status.result is not None
            work_dependencies = (selection_status.result.outputs[0].content_id,)
        return self._packet(
            run_id,
            lock,
            StageKind.FIT,
            inputs=(self.ledger.put_json("training_set", {"cases": train}),),
            instructions={
                "task": "Fit a state-only DSPy program using only the supplied training cases.",
                "candidate": dict(candidate),
                "phase": phase,
                "forbidden_splits": ["test", "challenge"],
            },
            dependencies=tuple(dependencies),
            output_schema="fitted-program-v1",
            work_spec={
                "training_cases": [
                    (case.id, case.content_hash) for case in cases
                ],
                "candidate": dict(candidate),
                "phase": phase,
                "fold": fold,
                "fold_assignment_hash": fold_assignment_hash,
                "selection": work_dependencies[0] if work_dependencies else None,
            },
            work_dependencies=work_dependencies,
        )

    def _cross_validation_fit_packets(
        self, run_id: str, lock: ExperimentLock, judge_validation: StagePacket
    ) -> tuple[StagePacket, ...]:
        train = self._cases_by_split()["train"]
        fold_by_group, assignment_hash = self._fold_assignment(lock)
        folds = lock.configuration.cross_validation
        assert folds is not None
        packets = []
        for candidate in self._candidate_configurations(lock):
            for fold in range(folds.folds):
                fit_cases = (
                    ()
                    if candidate["k"] == 0
                    else tuple(case for case in train if fold_by_group[case.group] != fold)
                )
                packets.append(
                    self._fit_packet(
                        run_id,
                        lock,
                        judge_validation,
                        cases=fit_cases,
                        candidate=candidate,
                        phase="cross_validation",
                        fold=fold,
                        fold_assignment_hash=assignment_hash,
                    )
                )
        return tuple(packets)

    def _cross_validation_solve_packets(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        fits: Sequence[StagePacket],
    ) -> tuple[StagePacket, ...]:
        train = self._cases_by_split()["train"]
        fold_by_group, _ = self._fold_assignment(lock)
        packets = []
        for fit in fits:
            status = self.ledger.status(fit.packet_id)
            assert status.result is not None
            output = _mapping(self.ledger.read_output(status.result), "fitted program")
            fold = _integer(output.get("fold"), "fitted program fold")
            candidate_id = _string(output.get("candidate_id"), "fitted program candidate_id")
            held_out = tuple(
                case.id for case in train if fold_by_group[case.group] == fold
            )
            packets.extend(
                self._solve_packets(
                    run_id,
                    lock,
                    judge_validation,
                    selected_case_ids=held_out,
                    program_dependency=fit,
                    evaluation_phase="cross_validation",
                    fold=fold,
                    candidate_id=candidate_id,
                )
            )
        return tuple(packets)

    def _selection_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        judges: Sequence[StagePacket],
    ) -> StagePacket:
        results = []
        output_ids = []
        for judge in sorted(judges, key=self._judge_packet_key):
            status = self.ledger.status(judge.packet_id)
            assert status.result is not None
            output_ids.append(status.result.outputs[0].content_id)
            output = _mapping(self.ledger.read_output(status.result), "cross-validation judgment")
            results.append(
                {
                    "candidate_id": _string(output.get("candidate_id"), "candidate_id"),
                    "k": _integer(output.get("candidate_k"), "candidate_k"),
                    "fold": _integer(output.get("fold"), "fold"),
                    "case_id": _string(output.get("case_id"), "case_id"),
                    "score": _number(output.get("score"), "score"),
                }
            )
        _, assignment_hash = self._fold_assignment(lock)
        selection_input = {
            "metric": "mean_score",
            "fold_assignment_hash": assignment_hash,
            "training_case_ids": [case.id for case in self._cases_by_split()["train"]],
            "results": results,
        }
        return self._packet(
            run_id,
            lock,
            StageKind.SELECT,
            inputs=(self.ledger.put_json("cross_validation", selection_input),),
            instructions={
                "task": "Select the candidate with the highest mean out-of-fold score.",
                "tie_break": "prefer smaller k, then candidate id",
            },
            dependencies=(judge_validation.packet_id, *(item.packet_id for item in judges)),
            output_schema="cross-validation-selection-v1",
            work_spec=selection_input,
            work_dependencies=tuple(output_ids),
        )

    def _final_fit_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        selection: StagePacket,
    ) -> StagePacket:
        status = self.ledger.status(selection.packet_id)
        assert status.result is not None
        selected = _mapping(self.ledger.read_output(status.result), "cross-validation selection")
        candidate = {
            "id": _string(selected.get("selected_candidate_id"), "selected candidate id"),
            "k": _integer(selected.get("selected_k"), "selected k"),
        }
        _, assignment_hash = self._fold_assignment(lock)
        return self._fit_packet(
            run_id,
            lock,
            judge_validation,
            cases=self._cases_by_split()["train"] if candidate["k"] else (),
            candidate=candidate,
            phase="final_fit",
            fold=None,
            fold_assignment_hash=assignment_hash,
            selection=selection,
        )

    def _solve_packets(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        *,
        selected_split: str | None = None,
        selected_splits: Sequence[str] | None = None,
        selected_case_ids: Sequence[str] | None = None,
        program_dependency: StagePacket | None = None,
        evaluation_phase: str = "test",
        fold: int | None = None,
        candidate_id: str | None = None,
    ) -> tuple[StagePacket, ...]:
        selectors = sum(
            item is not None for item in (selected_split, selected_splits, selected_case_ids)
        )
        if selectors > 1:
            raise IntegrityError("solve selection must use exactly one selection mechanism")
        split_selection = (
            None
            if selected_split is None and selected_splits is None
            else frozenset(
                (selected_split,) if selected_split is not None else selected_splits or ()
            )
        )
        if split_selection is not None and not split_selection:
            raise IntegrityError("solve split selection must not be empty")
        if split_selection is not None and not split_selection <= {
            "train",
            "test",
            "challenge",
        }:
            raise IntegrityError("solve split selection contains an unknown split")
        case_selection = None if selected_case_ids is None else frozenset(selected_case_ids)
        if case_selection is not None and not case_selection:
            raise IntegrityError("solve case selection must not be empty")
        state_artifact = None
        solver_program_hash = lock.solver_program_hash
        dependencies = [judge_validation.packet_id]
        candidate_k = 0
        if program_dependency is not None:
            dependencies.append(program_dependency.packet_id)
            if program_dependency.stage is not StageKind.FIT:
                raise IntegrityError("program dependency must be a fitted program")
            status = self.ledger.status(program_dependency.packet_id)
            if status.state is not StageState.PERSISTED or status.result is None:
                raise IntegrityError("fitted program dependency is missing")
            assert status.result is not None
            output = _mapping(self.ledger.read_output(status.result), "fit output")
            state = _mapping(output.get("state_artifact"), "fitted state artifact")
            solver_program_hash = _string(
                output.get("fitted_program_hash"), "fitted program hash"
            )
            candidate_id = _string(output.get("candidate_id"), "candidate id")
            candidate_k = _integer(output.get("candidate_k"), "candidate k")
            state_artifact = StageArtifact(
                "fitted_state",
                _string(state.get("content_id"), "compiled state content_id"),
                _string(state.get("media_type"), "compiled state media_type"),
            )
        packets = []
        reuse = self.workspace.repository.workspace.stages["solve"].reuse
        repetitions = (
            1
            if evaluation_phase == "cross_validation"
            else (
                reuse.samples
                if reuse.policy.value == "replicate"
                else lock.configuration.repetitions
            )
        )
        for case in self.workspace.case_bank.cases:
            split = self.workspace.splits.split_for(case)
            if split_selection is not None and split not in split_selection:
                continue
            if case_selection is not None and case.id not in case_selection:
                continue
            for repetition in range(1, repetitions + 1):
                inputs = [
                    self.ledger.put_json(
                        "case",
                        {
                            "case_id": case.id,
                            "case_hash": case.content_hash,
                            "skill": case.skill,
                            "split": split,
                            "repetition": repetition,
                            "global_context": self.workspace.context.global_context,
                            "skill_context": self.workspace.context.skill(case.skill),
                            "task": case.prompt,
                            "solver_attachments": dict(case.solver_attachments()),
                            "program_hash": solver_program_hash,
                            "evaluation_phase": evaluation_phase,
                            "fold": fold,
                            "candidate_id": candidate_id or "baseline",
                            "candidate_k": candidate_k,
                        },
                    )
                ]
                if state_artifact is not None:
                    inputs.append(state_artifact)
                solver_view = {
                    "case_solver_view_hash": case.solver_view_hash,
                    "global_context": self.workspace.context.global_context,
                    "skill_context": self.workspace.context.skill(case.skill),
                    "task": case.prompt,
                    "solver_attachments": dict(case.solver_attachments()),
                    "program_hash": solver_program_hash,
                            "fitted_state": (
                                None if state_artifact is None else state_artifact.content_id
                            ),
                            "evaluation_phase": evaluation_phase,
                            "fold": fold,
                            "candidate_id": candidate_id or "baseline",
                            "candidate_k": candidate_k,
                    "runtime": content_hash(
                        self.workspace.repository.workspace.runtime(lock.configuration.id)
                    ),
                    **(
                        {"sample_slot": repetition}
                        if reuse.policy.value != "replicate"
                        and lock.configuration.repetitions > 1
                        else {}
                    ),
                }
                packets.append(
                    self._packet(
                        run_id,
                        lock,
                        StageKind.SOLVE,
                        inputs=tuple(inputs),
                        instructions={
                            "task": "Answer using only global context, skill context, and task.",
                            "information_firewall": [
                            "rubric",
                                "expected_response",
                                "expected_artifacts",
                                "judge_feedback",
                            ],
                        },
                        dependencies=tuple(dependencies),
                        output_schema="solver-response-v1",
                        work_spec={"solver_view": solver_view},
                        work_dependencies=(),
                        replicate_slot=repetition,
                    )
                )
        return tuple(packets)

    def _judge_packets(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        solves: Sequence[StagePacket],
    ) -> tuple[StagePacket, ...]:
        packets = []
        for solve in solves:
            status = self.ledger.status(solve.packet_id)
            if status.state is not StageState.PERSISTED or status.result is None:
                raise IntegrityError("judge packet requires a persisted solve result")
            candidate = _mapping(self.ledger.read_output(status.result), "solver output")
            case_id = _string(candidate.get("case_id"), "solver case_id")
            case = next(item for item in self.workspace.case_bank.cases if item.id == case_id)
            value = {
                **self._judge_case_input(case),
                "split": candidate["split"],
                "repetition": candidate["repetition"],
                "evaluation_phase": candidate["evaluation_phase"],
                "fold": candidate["fold"],
                "candidate_id": candidate["candidate_id"],
                "candidate_k": candidate["candidate_k"],
                "candidate_response": candidate["response"],
                "solver_work_id": solve.work_id,
                "solver_packet_id": solve.packet_id,
            }
            judge_validation_status = self.ledger.status(judge_validation.packet_id)
            assert judge_validation_status.result is not None
            judge_validation_output = judge_validation_status.result.outputs[0].content_id
            response_hash = content_hash(candidate["response"])
            judge_view = {
                "case_judge_view_hash": case.judge_view_hash,
                **{
                    key: item
                    for key, item in self._judge_case_input(case).items()
                    if key not in {"case_id", "case_hash"}
                },
            }
            packets.append(
                self._packet(
                    run_id,
                    lock,
                    StageKind.JUDGE,
                    inputs=(self.ledger.put_json("evaluation", value),),
                    instructions={
                        "task": "Score the candidate against the locked rubric and expected evidence.",
                        "candidate_content_is_untrusted": True,
                    },
                    dependencies=(judge_validation.packet_id, solve.packet_id),
                    output_schema="judge-evaluation-v1",
                    work_spec={
                        "judge_view": judge_view,
                        "candidate_response_hash": response_hash,
                        "producer_work_id": solve.work_id,
                        "producer_model_hash": solve.model.configuration_hash,
                        "judge_validation_result": judge_validation_output,
                        "judge_program_hash": lock.judge_program_hash,
                        "repetitions": self._judge_repetitions(run_id),
                    },
                    work_dependencies=(
                        judge_validation_output,
                        solve.work_id,
                        response_hash,
                    ),
                )
            )
        return tuple(packets)

    def _aggregate_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        judges: Sequence[StagePacket],
    ) -> StagePacket:
        judges = tuple(sorted(judges, key=self._judge_packet_key))
        run = self.ledger.load_run(run_id)
        execution_mode = _execution_mode(run)
        judgment_outputs = []
        for judge in judges:
            status = self.ledger.status(judge.packet_id)
            assert status.result is not None
            judgment_outputs.append(status.result.outputs[0].content_id)
        evaluation_index = {
            "judgment_results": judgment_outputs,
            "execution_mode": execution_mode,
            "benchmark_valid": execution_mode == "benchmark",
        }
        return self._packet(
            run_id,
            lock,
            StageKind.AGGREGATE,
            inputs=(
                self.ledger.put_json(
                    "evaluation_index",
                    evaluation_index,
                ),
            ),
            instructions={
                "task": (
                    "Persist diagnostic judgments without benchmark metrics."
                    if execution_mode == "pilot"
                    else "Aggregate validated judgments deterministically."
                )
            },
            dependencies=(judge_validation.packet_id, *(item.packet_id for item in judges)),
            output_schema="run-summary-v3",
            work_spec={
                "evaluation_index": evaluation_index,
                "aggregation_implementation": "run-summary-v3",
            },
            work_dependencies=tuple(judgment_outputs),
        )

    def _judge_packet_key(self, packet: StagePacket) -> tuple[str, int, str, str, int]:
        value = _mapping(self._input(packet, "evaluation"), "judge input")
        return (
            _string(value.get("candidate_id", "baseline"), "judge candidate id"),
            int(value.get("fold") if value.get("fold") is not None else -1),
            _string(value.get("case_id"), "judge case_id"),
            _string(value.get("split"), "judge split"),
            int(value.get("repetition", 0)),
        )

    def _packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        stage: StageKind,
        *,
        inputs: Sequence[StageArtifact],
        instructions: Mapping[str, object],
        dependencies: Sequence[str],
        output_schema: str,
        work_spec: Mapping[str, object] | None = None,
        work_dependencies: Sequence[str] | None = None,
        replicate_slot: int = 1,
    ) -> StagePacket:
        workspace = self.workspace.repository.workspace
        configuration = workspace.stages[stage.value]
        executor = workspace.executors[configuration.executor]
        model = resolve_stage_model(
            workspace,
            stage.value,
            role="judge" if stage in {StageKind.VALIDATE_JUDGE, StageKind.JUDGE} else None,
        )
        dependency_artifacts: tuple[str, ...]
        if work_dependencies is not None:
            dependency_artifacts = tuple(work_dependencies)
        else:
            dependency_artifacts = tuple(
                output.content_id
                for dependency in dependencies
                for output in (
                    self.ledger.status(dependency).result.outputs
                    if self.ledger.status(dependency).result is not None
                    else ()
                )
            )
        packet = StagePacket.create(
            run_id=run_id,
            experiment_id=lock.configuration.id,
            experiment_revision_id=lock.content_hash,
            stage=stage,
            attempt=1,
            replicate_slot=replicate_slot,
            reuse_policy=configuration.reuse.policy.value,
            dependencies=dependencies,
            dependency_artifacts=dependency_artifacts,
            work_spec=(
                work_spec
                if work_spec is not None
                else {"inputs": [item.content_id for item in inputs]}
            ),
            inputs=inputs,
            instructions=self.ledger.put_json("instructions", instructions),
            output_schema=output_schema,
            executor=StageExecutorIdentity(
                executor.id, executor.adapter, executor.execution_hash
            ),
            model=StageModelIdentity(
                None if model is None else model.model,
                None if model is None else model.content_hash,
            ),
            budget=StageBudget(
                configuration.budget.model_calls,
                configuration.budget.tokens,
                configuration.budget.wall_seconds,
            ),
        )
        self._validate_packet(packet)
        matching_attempts = [
            item.packet
            for item in self.ledger.list(run_id)
            if self._same_stage_work(item.packet, packet)
        ]
        if matching_attempts:
            return max(matching_attempts, key=lambda item: item.attempt)
        return self.ledger.prepare(packet)

    @staticmethod
    def _same_stage_work(left: StagePacket, right: StagePacket) -> bool:
        """Match retry attempts while excluding their attempt-derived packet ids."""

        return (
            left.run_id == right.run_id
            and left.experiment_id == right.experiment_id
            and left.experiment_revision_id == right.experiment_revision_id
            and left.work_id == right.work_id
            and left.replicate_slot == right.replicate_slot
            and left.dependencies == right.dependencies
            and left.inputs == right.inputs
            and left.budget == right.budget
        )

    def _validate_packet(self, packet: StagePacket) -> None:
        if packet.stage is StageKind.SOLVE:
            if {item.id for item in packet.inputs} - {"case", "fitted_state"}:
                raise IntegrityError("solve packet contains an unauthorized input")
            forbidden = {
                "rubric",
                "expected_response",
                "expected_artifacts",
                "candidate_response",
                "judge_feedback",
            }
            if _contains_key(self._input(packet, "case"), forbidden):
                raise IntegrityError("solve packet crosses the hidden judging-material firewall")
        if packet.stage is StageKind.VALIDATE_JUDGE:
            forbidden = {"label"}
            if _contains_key(self._input(packet, "judge_validation"), forbidden):
                raise IntegrityError(
                    "judge validation packet exposes coordinator-only labels"
                )
        if packet.stage is StageKind.FIT:
            if _contains_value(self._input(packet, "training_set"), {"test", "challenge"}):
                raise IntegrityError("fit packet contains a held-out split")

    def _validate_result(self, packet: StagePacket, result: StageResult) -> None:
        if result.status != "completed":
            raise IntegrityError("failed result cannot be validated")
        self._validate_result_budget(packet, result)
        if len(result.outputs) != 1 or result.outputs[0].id != "result":
            raise IntegrityError("stage result must contain exactly one result output")
        payload = _mapping(self.ledger.read_output(result), f"{packet.output_schema} output")
        if packet.output_schema == "authored-cases-v1":
            _list(payload.get("candidate_packages"), "candidate_packages")
        elif packet.output_schema == "judge-validation-v1":
            if not isinstance(payload.get("ordering_consistent"), bool):
                raise IntegrityError(
                    "judge validation output requires boolean ordering_consistent"
                )
            scores = _mapping(payload.get("fixture_scores"), "fixture_scores")
            judge_validation = _mapping(self._input(packet, "judge_validation"), "judge_validation input")
            corpus_hash = _string(judge_validation.get("corpus_hash"), "judge_validation corpus_hash")
            if payload.get("corpus_hash") != corpus_hash:
                raise IntegrityError("judge-validation output has the wrong corpus hash")
            expected_ids = {item.id for item in self.workspace.judge_validation.fixtures}
            if set(scores) != expected_ids:
                raise IntegrityError("judge validation must score every fixture exactly")
            for score in scores.values():
                if not isinstance(score, (int, float)) or isinstance(score, bool):
                    raise IntegrityError("judge validation scores must be numeric")
                if not 0 <= float(score) <= 1:
                    raise IntegrityError("judge validation scores must be in [0, 1]")
            expected_result = judge_validation_result_from_scores(
                self.workspace.judge_validation,
                {key: float(value) for key, value in scores.items()},
            )
            if payload.get("ordering_consistent") is not expected_result.ordering_consistent:
                raise IntegrityError("judge validation ordering result was not deterministic")
            if payload.get("content_hash") != expected_result.content_hash:
                raise IntegrityError("judge-validation result identity hash is invalid")
        elif packet.output_schema == "solver-response-v1":
            for field in (
                "case_id",
                "split",
                "response",
                "program_hash",
                "evaluation_phase",
                "candidate_id",
            ):
                _string(payload.get(field), f"solver response {field}")
            if not isinstance(payload.get("repetition"), int) or not isinstance(
                payload.get("candidate_k"), int
            ):
                raise IntegrityError("solver response repetition must be an integer")
            case_input = _mapping(self._input(packet, "case"), "solve case input")
            for field in (
                "case_id",
                "split",
                "repetition",
                "program_hash",
                "evaluation_phase",
                "fold",
                "candidate_id",
                "candidate_k",
            ):
                if payload.get(field) != case_input.get(field):
                    raise IntegrityError(f"solver response {field} does not match its packet")
        elif packet.output_schema == "judge-evaluation-v1":
            _string(payload.get("case_id"), "judge evaluation case_id")
            score = payload.get("score")
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not 0 <= score <= 1
            ):
                raise IntegrityError("judge evaluation score must be in [0, 1]")
            _list(payload.get("votes"), "judge evaluation votes")
            evaluation_input = _mapping(self._input(packet, "evaluation"), "judge input")
            for field in (
                "case_id",
                "split",
                "repetition",
                "evaluation_phase",
                "fold",
                "candidate_id",
                "candidate_k",
            ):
                if payload.get(field) != evaluation_input.get(field):
                    raise IntegrityError(f"judge evaluation {field} does not match its packet")
        elif packet.output_schema == "fitted-program-v1":
            if payload.get("state_format") != "json":
                raise IntegrityError("fitted program must be state-only JSON")
            state = _mapping(payload.get("state_artifact"), "state_artifact")
            self.artifacts.reference(
                _string(state.get("content_id"), "state artifact content_id"),
                _string(state.get("media_type"), "state artifact media_type"),
            )
        elif packet.output_schema == "cross-validation-selection-v1":
            _string(payload.get("selected_candidate_id"), "selected candidate id")
            _integer(payload.get("selected_k"), "selected k")
            _number(payload.get("selected_mean_score"), "selected mean score")
            _list(payload.get("candidates"), "candidate summaries")
        elif packet.output_schema == "run-summary-v3":
            _list(payload.get("cases"), "run summary cases")
            _string(payload.get("run_graph_id"), "run summary graph id")
            if payload.get("run_id") != packet.run_id:
                raise IntegrityError("run summary is bound to another run")
        else:
            raise IntegrityError(f"unknown output schema: {packet.output_schema}")

    @staticmethod
    def _validate_result_budget(packet: StagePacket, result: StageResult) -> None:
        for key, limit in (
            ("model_calls", packet.budget.model_calls),
            ("tokens", packet.budget.tokens),
        ):
            value = result.usage.get(key)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise IntegrityError(f"stage result usage {key} must be non-negative")
            if value > limit:
                raise IntegrityError(f"stage result exceeds its {key} budget")
        try:
            started = datetime.fromisoformat(_string(result.timing.get("started_at"), "started_at"))
            completed = datetime.fromisoformat(
                _string(result.timing.get("completed_at"), "completed_at")
            )
        except ValueError as error:
            raise IntegrityError("stage result timing must use ISO-8601 timestamps") from error
        duration = (completed - started).total_seconds()
        if duration < 0:
            raise IntegrityError("stage result completion precedes its start")
        if duration > packet.budget.wall_seconds:
            raise IntegrityError("stage result exceeds its wall_seconds budget")

    def _judge_case_input(self, case) -> Mapping[str, object]:  # type: ignore[no-untyped-def]
        return {
            "case_id": case.id,
            "case_hash": case.content_hash,
            "task": case.prompt,
            "skill_context": self.workspace.context.skill(case.skill),
            "rubric": case.rubric.prompt_text(),
            "expected_response": case.expected_response,
            "expected_artifacts": json.dumps(
                case.expected_artifacts(), ensure_ascii=False, sort_keys=True
            ),
        }

    def _input(self, packet: StagePacket, input_id: str) -> object:
        artifact = next((item for item in packet.inputs if item.id == input_id), None)
        if artifact is None:
            raise IntegrityError(f"packet has no {input_id!r} input")
        reference = self.artifacts.reference(artifact.content_id, artifact.media_type)
        with self.artifacts.get_blob(reference) as handle:
            return json.load(handle)

    def _executor(self, packet: StagePacket) -> ExecutorConfiguration:
        workspace = self.workspace.repository.workspace
        try:
            executor = workspace.executors[packet.executor.id]
        except KeyError as error:
            raise IntegrityError("packet executor is no longer configured") from error
        if executor.execution_hash != packet.executor.configuration_hash:
            raise IntegrityError("packet executor configuration has changed")
        return executor

    def _budget_ledger(self, stage: StageKind, *, concurrency: int = 1) -> BudgetLedger:
        budget: RoleBudget = self.workspace.repository.workspace.stages[stage.value].budget
        return BudgetLedger(
            BudgetLimits(
                model_calls=budget.model_calls,
                configured_cost=float("inf"),
                tokens=budget.tokens,
                wall_seconds=budget.wall_seconds,
                concurrency=concurrency,
            )
        )

    def _persisted_packet(self, run_id: str, stage: StageKind) -> StagePacket | None:
        return next(
            (
                item.packet
                for item in self.ledger.list(run_id)
                if item.packet.stage is stage and item.state is StageState.PERSISTED
            ),
            None,
        )

    def _persisted_judges(
        self,
        run_id: str,
        *,
        split: str | None = None,
        phase: str | None = None,
    ) -> tuple[StagePacket, ...]:
        values = []
        for item in self.ledger.list(run_id):
            if item.packet.stage is not StageKind.JUDGE or item.state is not StageState.PERSISTED:
                continue
            if split is not None or phase is not None:
                assert item.result is not None
                output = _mapping(self.ledger.read_output(item.result), "judge output")
                if split is not None and output.get("split") != split:
                    continue
                if phase is not None and output.get("evaluation_phase") != phase:
                    continue
            values.append(item.packet)
        return tuple(values)

    def _persisted_fit_packets(
        self, run_id: str, *, phase: str
    ) -> tuple[StagePacket, ...]:
        values = []
        for item in self.ledger.list(run_id):
            if item.packet.stage is not StageKind.FIT or item.state is not StageState.PERSISTED:
                continue
            assert item.result is not None
            output = _mapping(self.ledger.read_output(item.result), "fit output")
            if output.get("phase") == phase:
                values.append(item.packet)
        return tuple(values)

    def _judge_validation_succeeded(self, packet: StagePacket) -> bool:
        status = self.ledger.status(packet.packet_id)
        if status.state is not StageState.PERSISTED or status.result is None:
            return False
        result = _mapping(self.ledger.read_output(status.result), "judge validation output")
        return result.get("ordering_consistent") is True

    def _cases_by_split(self):  # type: ignore[no-untyped-def]
        values = {key: [] for key in ("train", "test", "challenge")}
        for case in self.workspace.case_bank.cases:
            values[self.workspace.splits.split_for(case)].append(case)
        return {key: tuple(items) for key, items in values.items()}

    def _progress(
        self,
        run_id: str,
        experiment_id: str,
        status: str,
        stage: StageKind | None,
        *,
        result: Mapping[str, object] | None = None,
    ) -> RunProgress:
        run = self.ledger.load_run(run_id)
        execution_mode = _execution_mode(run)
        packets = tuple(
            {
                "packet_id": item.packet.packet_id,
                "work_id": item.packet.work_id,
                "attempt_id": item.packet.attempt_id,
                "stage": item.packet.stage.value,
                "attempt": item.packet.attempt,
                "state": item.state.value,
                "executor": item.packet.executor.id,
                "model": item.packet.model.id,
                "reused": (
                    item.result is not None
                    and item.result.provenance.get("cache_hit") is True
                ),
                "source_result_id": (
                    None
                    if item.result is None
                    else item.result.provenance.get("source_result_id")
                ),
                **{
                    key: value
                    for key, value in self._packet_event_context(item.packet).items()
                    if key != "details" and value is not None
                },
            }
            for item in self.ledger.list(run_id)
        )
        progress = RunProgress(
            3,
            run_id,
            experiment_id,
            execution_mode,
            execution_mode == "benchmark",
            str(run.get("coverage_status", "unknown")),
            status,
            None if stage is None else stage.value,
            packets,
            result,
        )
        self._emit(
            "run_status",
            run_id,
            experiment_id,
            stage=None if stage is None else stage.value,
            details={
                "status": status,
                "coverage_status": str(run.get("coverage_status", "unknown")),
                "execution_mode": execution_mode,
                "result": dict(result or {}),
                "data_root": self.artifacts.data_root.as_posix(),
                "experiment_revision_id": run.get("experiment_revision_id"),
            },
        )
        return progress

    def _validate_judge_validation_split(
        self, lock: ExperimentLock, *, pilot: bool = False
    ) -> None:
        if lock.configuration.mode is not ExperimentMode.OPTIMIZE and not pilot:
            return
        cases = {case.id: case for case in self.workspace.case_bank.cases}
        held_out = sorted(
            fixture.id
            for fixture in self.workspace.judge_validation.fixtures
            if self.workspace.splits.split_for(cases[fixture.case_id])
            in {"test", "challenge"}
        )
        if held_out:
            raise PreflightError(
                "optimization and pilot judge validation cannot use held-out "
                "test/challenge cases: "
                f"{held_out}"
            )

    @staticmethod
    def _redacted_error(error: Exception, environment: Mapping[str, str]) -> str:
        message = str(error)
        for key, value in environment.items():
            if ("KEY" in key or "TOKEN" in key or "SECRET" in key) and value:
                message = message.replace(value, "[REDACTED]")
        return message


def _usage(records: Sequence) -> Mapping[str, object]:  # type: ignore[type-arg]
    completed = [item for item in records if item.status == "completed"]
    failed = [item for item in records if item.status == "failed"]
    return {
        "model_calls": len(records),
        "completed_calls": len(completed),
        "failed_calls": len(failed),
        "tokens": sum(
            int(item.usage.get("total_tokens", 0) or 0)
            for item in completed
            if isinstance(item.usage.get("total_tokens", 0), int)
        ),
        "configured_cost": round(sum(item.configured_cost for item in completed), 10),
        "latency_seconds": round(sum(item.latency_seconds for item in records), 10),
        "error_kinds": dict(Counter(item.error_kind for item in failed)),
    }


def _dspy_execution_failure(role: str, result: ProgramResult) -> AutomaticExecutionFailed:
    kind = result.error_kind or "unknown_error"
    message = result.error_message
    detail = f": {message}" if isinstance(message, str) and message else ""
    if kind == "context_window_exceeded":
        summary = f"{role} context preflight rejected request{detail}"
    elif kind == "typed_output_parse_error":
        summary = f"{role} DSPy could not parse the completed model response{detail}"
    elif kind == "runtimeerror" and not result.calls and isinstance(message, str):
        summary = message
    else:
        summary = f"{role} DSPy call failed ({kind}){detail}"
    return AutomaticExecutionFailed(
        kind,
        summary,
        usage=_usage(result.calls),
    )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _execution_mode(run: Mapping[str, object]) -> str:
    value = run.get("execution_mode")
    if value is None and run.get("schema_version") == 1:
        return "benchmark"
    if value not in {"benchmark", "pilot"}:
        raise IntegrityError("run execution_mode must be benchmark or pilot")
    expected_validity = value == "benchmark"
    if run.get("benchmark_valid") is not expected_validity:
        raise IntegrityError("run benchmark_valid contradicts its execution_mode")
    if value == "pilot" and run.get("selected_splits") != ["train"]:
        raise IntegrityError("pilot run must be restricted to the training split")
    if value == "pilot" and run.get("judge_repetitions") != 1:
        raise IntegrityError("pilot run must use exactly one diagnostic judge repetition")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IntegrityError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise IntegrityError(f"{field} must be a list")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrityError(f"{field} must be a non-empty string")
    return value


def _integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise IntegrityError(f"{field} must be an integer")
    return value


def _number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise IntegrityError(f"{field} must be numeric")
    return float(value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise IntegrityError(f"{field} must be a string")
    return value


def _contains_key(value: object, keys: set[str]) -> bool:
    if isinstance(value, Mapping):
        return bool(set(map(str, value)) & keys) or any(
            _contains_key(item, keys) for item in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_key(item, keys) for item in value)
    return False


def _contains_value(value: object, forbidden: set[str]) -> bool:
    if isinstance(value, str):
        return value in forbidden
    if isinstance(value, Mapping):
        return any(_contains_value(item, forbidden) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_value(item, forbidden) for item in value)
    return False
