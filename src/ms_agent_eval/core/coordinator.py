from __future__ import annotations

import json
import random
import tempfile
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from io import BytesIO
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
    create_copro_proposal_program,
    create_judge_program,
    create_solver_program,
    load_state_json,
    program_hash,
    solver_prompt_state,
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
    deterministic_group_validation_split,
    judge_validation_result_from_scores,
    validate_judge,
)
from .endpoint_pool import EndpointLease, EndpointPool, endpoint_provenance_routes
from .garbage_collection import collect_artifact_garbage
from .hashing import canonical_json_bytes, content_hash, json_value
from .lifecycle import RunLifecycleLedger, RunLifecycleState
from .models import ArtifactReference, ProgramResult, SnapshotLock
from .optimization import (
    normalize_copro_proposals,
    validate_copro_proposal_accounting,
)
from .planning import CompiledWorkspace, ExperimentLock, lock_as_dict
from .progress import ProgressEvent, ProgressSink
from .providers import ModelCallObserver, failure_kind
from .recorder import RecorderFactory, RecorderWorkerService, recorder_worker_service
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
    CoproConfiguration,
    EndpointConfiguration,
    ExecutorConfiguration,
    ExecutorType,
    ExperimentMode,
    LabeledFewShotConfiguration,
    ResolvedRoleModel,
    RoleBudget,
    WORKSPACE_SCHEMA_VERSION,
    WorkspaceRepository,
    load_yaml,
    resolve_stage_model,
)
from .workload import calculate_workload


LmFactory = Callable[
    [ResolvedRoleModel, ModelCallObserver, BudgetLedger | None, str | None], dspy.BaseLM
]

_EXPECTED_RESPONSE_TOKEN_BANDS = (128, 256, 512, 1024, 2048)


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


@dataclass(frozen=True)
class _CoproTargetPlan:
    target_kind: str
    target_id: str
    training_cases: tuple[CaseDefinition, ...]
    validation_cases: tuple[CaseDefinition, ...]
    proposal: StagePacket
    candidates: tuple[Mapping[str, object], ...]
    current_candidate_id: str
    fits: tuple[StagePacket, ...]
    solves: tuple[StagePacket, ...]


@dataclass(frozen=True)
class _CoproSearchResult:
    bundle: Mapping[str, object]
    dependencies: tuple[StagePacket, ...]


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
            experiment_id=packet.experiment_id,
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
                eligible_endpoints = (
                    model.eligible_endpoints(executor)
                    if executor.type is ExecutorType.ENDPOINT
                    else ()
                )
                budget = self.coordinator._budget_ledger(
                    packet.stage,
                    concurrency=(
                        sum(endpoint.max_concurrent_requests for endpoint in eligible_endpoints)
                        if executor.type is ExecutorType.ENDPOINT
                        else 1
                    ),
                    model_call_multiplier=max(1, len(eligible_endpoints)),
                )
                self.budgets[packet.packet_id] = budget
        if executor.type is ExecutorType.ENDPOINT:
            lm = RoutedDspyLM(
                model=f"openai/{model.model}",
                route_call=lambda prompt, messages, rendered, parameters: self._call_routed_lm(
                    model,
                    executor,
                    observer,
                    budget,
                    prompt,
                    messages,
                    rendered,
                    parameters,
                ),
            )
        else:
            lm = self.coordinator.lm_factory(model, observer, budget, None)
        return lm, observer

    def _call_routed_lm(
        self,
        model: ResolvedRoleModel,
        executor: ExecutorConfiguration,
        observer: ModelCallObserver,
        budget: BudgetLedger,
        prompt: object,
        raw_messages: object,
        rendered_messages: Sequence[Mapping[str, object]],
        call_parameters: Mapping[str, object],
    ) -> object:
        try:
            pool = self.coordinator.endpoint_pools[executor.id]
        except KeyError as error:
            raise IntegrityError("endpoint executor pool is unavailable") from error
        eligible_endpoints = model.eligible_endpoints(executor)
        configured_ids = {endpoint.id for endpoint in eligible_endpoints}
        excluded: set[str] = {
            endpoint.id for endpoint in executor.endpoints if endpoint.id not in configured_ids
        }
        maximum_output_tokens = call_parameters.get("max_tokens")
        if maximum_output_tokens is None:
            maximum_output_tokens = call_parameters.get("max_completion_tokens")
        if maximum_output_tokens is None:
            maximum_output_tokens = model.parameters.get("max_tokens")
        if maximum_output_tokens is None:
            maximum_output_tokens = model.parameters.get("max_completion_tokens")
        if not isinstance(maximum_output_tokens, int) or isinstance(maximum_output_tokens, bool):
            maximum_output_tokens = None

        last_endpoint_error: Exception | None = None
        while True:
            rejected: ContextWindowExceededError | None = None
            quarantined = set(pool.quarantined_endpoint_ids()) & configured_ids
            lease_exclusions = excluded | quarantined
            if configured_ids <= lease_exclusions:
                # A quarantine is a cooldown, not a permanent outage declaration.
                # If no non-quarantined replica remains, probe an endpoint that
                # has not already failed this logical model call.
                lease_exclusions = excluded
            if configured_ids <= lease_exclusions:
                if last_endpoint_error is not None:
                    raise last_endpoint_error
                assert rejected is not None
                raise rejected
            with pool.lease(excluded_endpoint_ids=frozenset(lease_exclusions)) as lease:
                endpoint = lease.endpoint
                bound_model = model.bind_endpoint(endpoint)
                api_key = endpoint.api_key(self.environment)
                preflight = self.coordinator.token_preflight.evaluate(
                    endpoint,
                    model=bound_model.model,
                    deployment_revision=model.deployment_revision,
                    messages=rendered_messages,
                    maximum_output_tokens=maximum_output_tokens,
                    reasoning_effort=model.reasoning_effort,
                    api_key=api_key,
                )
                if not preflight.permits_dispatch:
                    observer.record_endpoint_route(
                        self._endpoint_route(
                            lease,
                            bound_model,
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
                    response: object | None = None
                    endpoint_error: Exception | None = None
                    try:
                        delegate_key = (id(observer), endpoint.id)
                        with self._lock:
                            delegate = self.delegates.get(delegate_key)
                            if delegate is None:
                                delegate = self.coordinator.lm_factory(
                                    bound_model,
                                    observer,
                                    budget,
                                    api_key,
                                )
                                self.delegates[delegate_key] = delegate
                        response = delegate.forward(
                            prompt=prompt,
                            messages=raw_messages,
                            **dict(call_parameters),
                        )
                    except Exception as error:
                        request_status = "failed"
                        endpoint_error = error
                    finally:
                        records = tuple(observer.records[record_start:])
                        if records and records[-1].status == "failed":
                            request_status = "failed"
                        observer.record_endpoint_route(
                            self._endpoint_route(
                                lease,
                                bound_model,
                                preflight,
                                generation_attempted=True,
                                request_status=request_status,
                                usage=_usage(records),
                                duration_seconds=time.monotonic() - started,
                            )
                        )
                    if endpoint_error is None:
                        pool.restore(endpoint.id)
                        return response
                    endpoint_failure = failure_kind(endpoint_error)
                    remaining = configured_ids - excluded - {endpoint.id}
                    if (
                        endpoint_failure
                        in {
                            "connection_error",
                            "provider_server_error",
                            "rate_limit_error",
                        }
                        and remaining
                    ):
                        pool.quarantine(endpoint.id)
                        excluded.add(endpoint.id)
                        last_endpoint_error = endpoint_error
                        continue
                    raise endpoint_error
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
                "deployment_id": model.deployment_id,
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
        return _usage([record for observer in observers for record in observer.records])

    def endpoint_provenance(self, packet_id: str) -> Mapping[str, object]:
        with self._lock:
            observers = tuple(self.observers.get(packet_id, ()))
        observed = [dict(route) for observer in observers for route in observer.endpoint_routes]
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
                    "endpoint_configuration_hash": route.get("endpoint_configuration_hash"),
                    "model": route.get("model"),
                    "deployment_id": route.get("deployment_id"),
                    "deployment_revision": route.get("deployment_revision"),
                    "max_concurrent_requests": route.get("max_concurrent_requests"),
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
                    aggregate["preflight_rejections"] = int(aggregate["preflight_rejections"]) + 1
                elif status == "skipped":
                    aggregate["preflight_skips"] = int(aggregate["preflight_skips"]) + 1
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
            experiment_id=self.lock.configuration.id,
            role=role,
        )
        if resolved_model is None:
            raise IntegrityError("endpoint provenance has no resolved model")
        executor = self.coordinator.workspace.repository.workspace.executors[
            resolved_model.executor_id
        ]
        eligible_capacity = sum(
            endpoint.max_concurrent_requests
            for endpoint in resolved_model.eligible_endpoints(executor)
        )
        pool_value: dict[str, object] = {
            "pool_hash": executor.pool_hash,
            "total_concurrency": eligible_capacity,
            "deployment_id": resolved_model.deployment_id,
            "requests": sum(int(route["requests"]) for route in routes),
            "completed_requests": sum(int(route["completed_requests"]) for route in routes),
            "failed_requests": sum(int(route["failed_requests"]) for route in routes),
            "preflight_checks": sum(int(route["preflight_checks"]) for route in routes),
            "preflight_rejections": sum(int(route["preflight_rejections"]) for route in routes),
            "preflight_skips": sum(int(route["preflight_skips"]) for route in routes),
            "routes": routes,
        }
        if len(routes) == 1:
            singular = dict(routes[0])
            singular["total_concurrency"] = eligible_capacity
            singular["deployment_id"] = resolved_model.deployment_id
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
        recorder_factory: RecorderFactory | None = None,
    ) -> None:
        self.workspace = workspace
        repository = (
            workspace if isinstance(workspace, WorkspaceRepository) else workspace.repository
        )
        self.artifacts = FilesystemArtifactStore(
            repository.data_root,
            workspace_root=repository.root,
        )
        self.ledger = StageLedger(self.artifacts)
        self.lifecycle = RunLifecycleLedger(self.artifacts)
        self.dspy = DspyExecutor(self.artifacts)
        self.lm_factory = lm_factory
        self.recorder_factory = recorder_factory
        self.token_preflight = token_preflight or EndpointTokenPreflight()
        self.progress_sink = progress_sink
        self._progress_lock = Lock()
        executor_lock_root = self.artifacts.operations_root / "locks" / "executors"
        self.endpoint_pools = {
            executor.id: EndpointPool(executor, executor_lock_root)
            for executor in repository.workspace.executors.values()
            if executor.type is ExecutorType.ENDPOINT
        }
        self.recorder_service: RecorderWorkerService = recorder_worker_service(
            self.artifacts,
            recording=repository.workspace.recording,
            recorders=repository.workspace.recorders,
            recorder_queues=repository.workspace.recorder_queues,
            environment=repository.environment(),
            adapter_factory=recorder_factory,
        )

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
        published = build_and_publish_run_report(
            self.workspace,
            self.ledger,
            run_id,
            replace=replace,
        )
        self.lifecycle.append(
            run_id,
            RunLifecycleState.REPORT_PUBLISHED,
            reason="report_built",
            details={"report_hash": published.content_hash},
            if_changed=True,
        )
        self._recording_checkpoint(run_id, force=True, flush=True)
        return published

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
                "benchmark_revision_id": lock.benchmark_revision_id,
                "comparison_group_id": lock.comparison_group_id,
                "comparison_id": lock.comparison_id,
                "arm_id": lock.arm_id,
                "optimization": (
                    json_value(lock.configuration.optimizer)
                    if lock.configuration.optimizer is not None
                    else None
                ),
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
                "selected_splits": (["train"] if pilot else ["test"]),
                "judge_repetitions": (
                    1 if pilot else self.workspace.repository.workspace.evaluation.judge.repetitions
                ),
                "workspace_id": lock.workspace_id,
                "workspace_hash": lock.workspace_hash,
                "experiment_revision_id": lock.content_hash,
                "benchmark_revision_id": lock.benchmark_revision_id,
                "comparison_group_id": lock.comparison_group_id,
                "comparison_id": lock.comparison_id,
                "arm_id": lock.arm_id,
                "optimization": (
                    json_value(lock.configuration.optimizer)
                    if lock.configuration.optimizer is not None
                    else None
                ),
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
                "benchmark_revision_id": lock.benchmark_revision_id,
                "comparison_group_id": lock.comparison_group_id,
                "comparison_id": lock.comparison_id,
                "arm_id": lock.arm_id,
            },
        )
        self.lifecycle.append(
            run_id,
            RunLifecycleState.CREATED,
            reason="run_created",
            details={
                "experiment_id": experiment_id,
                "experiment_revision_id": lock.content_hash,
                "execution_mode": execution_mode,
            },
        )
        self.lifecycle.append(
            run_id,
            RunLifecycleState.RUNNING,
            reason="run_started",
            details={"experiment_id": experiment_id},
        )
        self._recording_checkpoint(run_id)
        self._emit(
            "run_started",
            run_id,
            experiment_id,
            details={
                "execution_mode": execution_mode,
                "case_count": self._run_case_count(
                    lock.configuration,
                    pilot=pilot,
                ),
                "data_root": self.artifacts.data_root.as_posix(),
                "experiment_revision_id": lock.content_hash,
                "optimization": (
                    json_value(lock.configuration.optimizer)
                    if lock.configuration.optimizer is not None
                    else None
                ),
                "workload": calculate_workload(
                    self.workspace,
                    experiment_id,
                    environment=resolved_environment,
                    pilot=pilot,
                ),
            },
        )
        try:
            return self._advance(
                run_id,
                lock,
                environment=resolved_environment,
                through=through,
                pilot=pilot,
            )
        except KeyboardInterrupt:
            self.lifecycle.append(
                run_id,
                RunLifecycleState.CANCELLED,
                reason="keyboard_interrupt",
                if_changed=True,
            )
            self._recording_checkpoint(run_id, force=True, flush=True)
            raise
        except Exception as error:
            self.lifecycle.append(
                run_id,
                RunLifecycleState.FAILED,
                reason="coordinator_error",
                details={"error_kind": type(error).__name__.lower()},
                if_changed=True,
            )
            self._recording_checkpoint(run_id, force=True, flush=True)
            raise

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
                "instruction_imports": json_value(self.workspace.context.instruction_imports),
                "instruction_units": json_value(self.workspace.context.instruction_units),
                "source_paths": list(self.workspace.context.source_paths),
                "workspace_source_paths": list(self.workspace.context.workspace_source_paths),
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
                        "candidate_response": fixture.candidate_path.read_text(encoding="utf-8"),
                        "label": fixture.label,
                    }
                    for fixture in self.workspace.judge_validation.fixtures
                ],
            },
        }

    def _selected_workspace_configuration(self, lock: ExperimentLock) -> str:
        """Project the workspace to only the selected experiment family."""

        payload = dict(load_yaml(self.workspace.repository.workspace_file))
        experiments = _mapping(payload.get("experiments"), "workspace experiments")
        selected_ids = [lock.configuration.id]
        if lock.configuration.based_on is not None:
            selected_ids.append(lock.configuration.based_on)
        payload["experiments"] = {
            experiment_id: experiments[experiment_id] for experiment_id in selected_ids
        }
        # Keep the complete deployment inventory because endpoint pool
        # configuration is frozen as attempt provenance. Only selected
        # experiments enter the semantic lock; unused models and deployments
        # therefore do not change its revision.
        payload.pop("comparisons", None)
        # Recording is an operational projection and is injected from the
        # active workspace when an old semantic revision is rehydrated. Keeping
        # it out of this immutable definition prevents a tracking destination
        # edit from changing or colliding with the experiment revision bundle.
        payload.pop("recording", None)
        payload.pop("recorders", None)
        payload.pop("recorder_queues", None)
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
                        path for path in root.rglob("*") if path.is_file() and not path.is_symlink()
                    )
        inventory: list[Mapping[str, object]] = []
        workspace_relative = repository.workspace_file.relative_to(repository.root).as_posix()
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
        revision = _string(run.get("experiment_revision_id"), "run experiment_revision_id")
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
            try:
                workspace_payload = json.loads(workspace_json)
            except json.JSONDecodeError as error:
                raise IntegrityError("frozen workspace configuration is invalid") from error
            if not isinstance(workspace_payload, dict):
                raise IntegrityError("frozen workspace configuration must be a mapping")
            workspace_payload = _upgrade_frozen_workspace_payload(workspace_payload)
            active_repository = (
                self.workspace
                if isinstance(self.workspace, WorkspaceRepository)
                else self.workspace.repository
            )
            active_recording = active_repository.workspace.recording
            trace_projection = active_recording.projection.traces
            workspace_payload["recording"] = {
                "enabled": active_recording.enabled,
                "recorder": active_recording.recorder,
                "queue": active_recording.queue,
                "failure_policy": active_recording.failure_policy.value,
                "wait_on_run_exit": active_recording.wait_on_run_exit,
                "artifact_policy": active_recording.artifact_policy.value,
                "content_policy": active_recording.content_policy.value,
                "projection": {
                    "live": {
                        "enabled": active_recording.projection.live.enabled,
                        "minimum_interval_seconds": active_recording.projection.live.minimum_interval_seconds,
                    },
                    "stage_boundary": {
                        "tables": active_recording.projection.stage_boundary.tables,
                        "summaries": active_recording.projection.stage_boundary.summaries,
                    },
                    "finalization": {
                        "reports": active_recording.projection.finalization.reports,
                        "plotly": active_recording.projection.finalization.plotly,
                        "evidence": active_recording.projection.finalization.evidence,
                    },
                    "traces": {
                        "optimization": {
                            "policy": trace_projection.optimization_policy.value,
                            "sample_rate": trace_projection.optimization_sample_rate,
                        },
                        "validation": {"policy": trace_projection.validation_policy},
                        "test": {"policy": trace_projection.test_policy},
                    },
                },
            }
            # Recorder IDs are mapping keys in workspace.yaml, not fields in
            # the recorder payload.  Serializing the dataclass directly would
            # leak its normalized ``id`` back into the external schema.
            workspace_payload["recorders"] = {
                recorder_id: {
                    "type": recorder.type,
                    "tracking_uri": recorder.tracking_uri,
                    "experiment_name": recorder.experiment_name,
                    "authentication": json_value(recorder.authentication),
                }
                for recorder_id, recorder in active_repository.workspace.recorders.items()
            }
            workspace_payload["recorder_queues"] = {
                queue_id: {
                    "type": queue.type,
                    "path": queue.path,
                    "live_coalescing": queue.live_coalescing,
                    "lease_seconds": queue.lease_seconds,
                    "max_attempts": queue.max_attempts,
                }
                for queue_id, queue in active_repository.workspace.recorder_queues.items()
            }
            (root / "workspace.yaml").write_text(
                canonical_json_bytes(workspace_payload).decode("utf-8"),
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
            capability_catalog = CapabilityCatalog.load(repository.capability_catalog_path)
            splits = SplitManifest.load(repository.split_file, case_bank)
            judge_validation = JudgeValidationCorpus.load(
                repository.judge_validation_root, case_bank
            )
            validation = _mapping(definition.get("validation"), "frozen validation")
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
            generated_lock = frozen.experiment_lock(family, environment=environment)
            if generated_lock.content_hash != revision:
                historical_lock = _rehydrate_historical_experiment_lock(
                    _mapping(definition.get("lock"), "frozen experiment lock"),
                    generated_lock,
                    expected_revision=revision,
                    judge_repetitions=repository.workspace.evaluation.judge.repetitions,
                )
                frozen = replace(
                    frozen,
                    lock_overrides=MappingProxyType({family: historical_lock}),
                )
            frozen_lock = frozen.experiment_lock(family, environment=environment)
            if frozen_lock.content_hash != revision:
                raise IntegrityError("stored experiment definition does not reproduce its revision")
            yield StageCoordinator(
                frozen,
                lm_factory=self.lm_factory,
                progress_sink=self.progress_sink,
                token_preflight=self.token_preflight,
                recorder_factory=self.recorder_factory,
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
    def _snapshot_from_hash(store: ExternalSnapshotStore, snapshot_hash: str) -> SnapshotLock:
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
                repository_url=_string(item.get("repository_url"), "frozen source repository_url"),
                requested_ref=_string(item.get("requested_ref"), "frozen source requested_ref"),
                resolved_commit=_string(
                    item.get("resolved_commit"), "frozen source resolved_commit"
                ),
                snapshot_hash=_string(item.get("snapshot_hash"), "frozen source snapshot_hash"),
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
                source_files[qualified] = store.directory(source_lock) / locked_file.snapshot_path
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
                for item in _list(payload.get("source_paths"), "frozen context source_paths")
            )
        )
        if declared_paths != tuple(sorted(source_files)):
            raise IntegrityError("frozen context source inventory is incomplete")
        skills = _mapping(payload.get("skills"), "frozen context skills")
        instruction_units = tuple(
            InstructionUnitIdentity(
                id=_string(item.get("id"), "frozen instruction unit id"),
                source_id=_string(item.get("source_id"), "frozen instruction unit source_id"),
                source_type=_string(item.get("source_type"), "frozen instruction unit source_type"),
                source_path=self._safe_bundle_path(item.get("source_path")),
                content_hash=_string(
                    item.get("content_hash"), "frozen instruction unit content_hash"
                ),
            )
            for item in (
                _mapping(raw, "frozen instruction units[]")
                for raw in _list(payload.get("instruction_units"), "frozen instruction units")
            )
        )
        if {item.id for item in instruction_units} != set(skills):
            raise IntegrityError("frozen instruction units do not match frozen skill contexts")
        instruction_imports: list[InstructionImportIdentity] = []
        for raw_import in _list(payload.get("instruction_imports"), "frozen instruction imports"):
            item = _mapping(raw_import, "frozen instruction imports[]")
            imported_units = tuple(
                InstructionUnitIdentity(
                    id=_string(unit.get("id"), "frozen imported unit id"),
                    source_id=_string(unit.get("source_id"), "frozen imported unit source_id"),
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
            snapshot_hash=_string(payload.get("snapshot_hash"), "frozen context snapshot_hash"),
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
                    lock.configuration,
                    pilot=pilot,
                ),
                "data_root": self.artifacts.data_root.as_posix(),
                "experiment_revision_id": lock.content_hash,
                "optimization": (
                    json_value(lock.configuration.optimizer)
                    if lock.configuration.optimizer is not None
                    else None
                ),
                "workload": calculate_workload(
                    self.workspace,
                    experiment_id,
                    environment=resolved_environment,
                    pilot=pilot,
                ),
            },
        )
        self.lifecycle.append(
            run_id,
            RunLifecycleState.RUNNING,
            reason="run_resumed",
            details={"through": through},
            if_changed=True,
        )
        self._recording_checkpoint(run_id)
        try:
            return self._advance(
                run_id,
                lock,
                environment=resolved_environment,
                through=through,
                pilot=pilot,
            )
        except KeyboardInterrupt:
            self.lifecycle.append(
                run_id,
                RunLifecycleState.CANCELLED,
                reason="keyboard_interrupt",
                if_changed=True,
            )
            self._recording_checkpoint(run_id, force=True, flush=True)
            raise
        except Exception as error:
            self.lifecycle.append(
                run_id,
                RunLifecycleState.FAILED,
                reason="coordinator_error",
                details={"error_kind": type(error).__name__.lower()},
                if_changed=True,
            )
            self._recording_checkpoint(run_id, force=True, flush=True)
            raise

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
        if stage is StageKind.OPTIMIZE:
            optimizer = lock.configuration.optimizer
            if not isinstance(optimizer, CoproConfiguration):
                raise PreflightError("optimize is valid only for COPRO experiments")
            training, _, _ = self._optimization_cases(lock)
            target_kind, target_id = next(
                (kind, target)
                for kind, target in self._copro_targets(optimizer)
                if kind == "global_instructions"
                or any(case.skill == target for case in training)
            )
            return (
                self._copro_proposal_packet(
                    run_id,
                    lock,
                    judge_validation,
                    bundle=self._base_copro_bundle(optimizer),
                    target_kind=target_kind,
                    target_id=target_id,
                    round_number=1,
                    scope="optimization",
                    fitting_cases=(
                        training
                        if target_kind == "global_instructions"
                        else tuple(case for case in training if case.skill == target_id)
                    ),
                    history=(),
                    dependencies=(),
                ),
            )
        if stage is StageKind.FIT:
            if pilot:
                raise PreflightError("pilot runs cannot prepare fitting")
            if lock.configuration.mode is not ExperimentMode.OPTIMIZE:
                raise PreflightError("fit is valid only for optimization experiments")
            if isinstance(lock.configuration.optimizer, CoproConfiguration):
                raise PreflightError(
                    "COPRO fit packets are created from persisted proposal selections; "
                    "resume the staged run to prepare the next dependency"
                )
            selection = self._persisted_packet(run_id, StageKind.SELECT)
            if selection is not None:
                return (self._final_fit_packet(run_id, lock, judge_validation, selection),)
            return self._validation_fit_packets(run_id, lock, judge_validation)
        if stage is StageKind.SOLVE:
            if lock.configuration.mode is ExperimentMode.OPTIMIZE:
                fits = self._persisted_fit_packets(run_id, phase="final_fit")
                if fits:
                    if isinstance(lock.configuration.optimizer, CoproConfiguration):
                        return self._solve_packets(
                            run_id,
                            lock,
                            judge_validation,
                            selected_split="test",
                            evaluation_phase="test",
                            candidate_id="baseline",
                            gate_dependency=fits[0],
                        ) + self._solve_packets(
                            run_id,
                            lock,
                            judge_validation,
                            selected_split="test",
                            program_dependency=fits[0],
                            evaluation_phase="test",
                        )
                    return self._solve_packets(
                        run_id,
                        lock,
                        judge_validation,
                        selected_split="test",
                        program_dependency=fits[0],
                        evaluation_phase="test",
                    )
                fits = self._persisted_fit_packets(run_id, phase="validation")
                if not fits:
                    raise PreflightError("validation fits must persist before solve")
                return self._validation_solve_packets(run_id, lock, judge_validation, fits)
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
            judges = self._persisted_judges(run_id, phase="validation")
            if not judges:
                raise PreflightError("validation judgments must persist before selection")
            return (self._selection_packet(run_id, lock, judge_validation, judges),)
        if stage is StageKind.AGGREGATE:
            judges = self._persisted_judges(run_id)
            if not judges:
                raise PreflightError("judgments must persist before aggregate")
            return (
                self._aggregate_packet(
                    run_id,
                    lock,
                    judge_validation,
                    judges,
                    environment=environment,
                ),
            )
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
            self._record_packet_terminal(packet, result)
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
        self._recording_checkpoint(packet.run_id)
        self._emit_packet(
            "packet_completed",
            packet,
            details=self._output_event_details(packet.stage, payload),
        )
        if self._packet_ends_run(packet, persisted):
            self._finalize_run(packet.run_id, packet=packet)
            self._record_packet_terminal(packet, persisted)
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
        self._recording_checkpoint(
            run_id,
            force=True,
            stage=StageKind.VALIDATE_JUDGE.value,
            boundary_id="judge-validation-completed",
        )
        if through == StageKind.VALIDATE_JUDGE.value:
            return self._progress(run_id, lock.configuration.id, "paused", judge_validation.stage)

        if lock.configuration.mode is ExperimentMode.OPTIMIZE:
            if pilot:
                raise IntegrityError("pilot execution cannot enter optimization")
            optimizer = lock.configuration.optimizer
            if isinstance(optimizer, CoproConfiguration):
                copro = self._advance_copro(
                    run_id,
                    lock,
                    judge_validation,
                    session,
                    through=through,
                )
                if isinstance(copro, RunProgress):
                    return copro
                final_fit = copro
            else:
                validation_fits = self._validation_fit_packets(run_id, lock, judge_validation)
                pending = self._execute_all(validation_fits, lock, session)
                if pending is not None:
                    return self._blocked_progress(run_id, lock.configuration.id, pending)
                self._recording_checkpoint(
                    run_id,
                    force=True,
                    stage=StageKind.FIT.value,
                    boundary_id="cross-validation-fit-completed",
                )
                if through == StageKind.FIT.value:
                    return self._progress(run_id, lock.configuration.id, "paused", StageKind.FIT)
                validation_solves = self._validation_solve_packets(
                    run_id, lock, judge_validation, validation_fits
                )
                if through == StageKind.SOLVE.value:
                    pending = self._execute_all(validation_solves, lock, session)
                    if pending is not None:
                        return self._blocked_progress(run_id, lock.configuration.id, pending)
                    return self._progress(run_id, lock.configuration.id, "paused", StageKind.SOLVE)
                validation_judges, pending = self._execute_solve_judge_pipeline(
                    validation_solves,
                    lock,
                    session,
                    judge_validation,
                )
                if pending is not None:
                    return self._blocked_progress(run_id, lock.configuration.id, pending)
                self._recording_checkpoint(
                    run_id,
                    force=True,
                    stage=StageKind.SOLVE.value,
                    boundary_id="cross-validation-solve-completed",
                )
                self._recording_checkpoint(
                    run_id,
                    force=True,
                    stage=StageKind.JUDGE.value,
                    boundary_id="cross-validation-judge-completed",
                )
                if through == StageKind.JUDGE.value:
                    return self._progress(run_id, lock.configuration.id, "paused", StageKind.JUDGE)
                selection = self._selection_packet(
                    run_id, lock, judge_validation, validation_judges
                )
                if not self._ensure_executed(selection, lock, session):
                    return self._blocked_progress(run_id, lock.configuration.id, selection)
                self._recording_checkpoint(
                    run_id,
                    force=True,
                    stage=StageKind.SELECT.value,
                    boundary_id="selection-completed",
                )
                if through == StageKind.SELECT.value:
                    return self._progress(run_id, lock.configuration.id, "paused", selection.stage)
                final_fit = self._final_fit_packet(run_id, lock, judge_validation, selection)
                if not self._ensure_executed(final_fit, lock, session):
                    return self._blocked_progress(run_id, lock.configuration.id, final_fit)
            self._recording_checkpoint(
                run_id,
                force=True,
                stage=StageKind.FIT.value,
                boundary_id="final-program-completed",
            )
            if isinstance(lock.configuration.optimizer, CoproConfiguration):
                baseline_solves = self._solve_packets(
                    run_id,
                    lock,
                    judge_validation,
                    selected_split="test",
                    evaluation_phase="test",
                    candidate_id="baseline",
                    gate_dependency=final_fit,
                )
                optimized_solves = self._solve_packets(
                    run_id,
                    lock,
                    judge_validation,
                    selected_split="test",
                    program_dependency=final_fit,
                    evaluation_phase="test",
                )
                solves = baseline_solves + optimized_solves
            else:
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

        if through == StageKind.SOLVE.value:
            pending = self._execute_all(solves, lock, session)
            if pending is not None:
                return self._blocked_progress(run_id, lock.configuration.id, pending)
            return self._progress(run_id, lock.configuration.id, "paused", StageKind.SOLVE)
        judges, pending = self._execute_solve_judge_pipeline(
            solves,
            lock,
            session,
            judge_validation,
        )
        if pending is not None:
            return self._blocked_progress(run_id, lock.configuration.id, pending)
        self._recording_checkpoint(
            run_id,
            force=True,
            stage=StageKind.SOLVE.value,
            boundary_id="test-solve-completed",
        )
        self._recording_checkpoint(
            run_id,
            force=True,
            stage=StageKind.JUDGE.value,
            boundary_id="test-judge-completed",
        )
        if through == StageKind.JUDGE.value:
            return self._progress(run_id, lock.configuration.id, "paused", StageKind.JUDGE)
        aggregate = self._aggregate_packet(
            run_id,
            lock,
            judge_validation,
            judges,
            environment=session.environment,
        )
        if not self._ensure_executed(aggregate, lock, session):
            return self._blocked_progress(run_id, lock.configuration.id, aggregate)
        self._recording_checkpoint(
            run_id,
            force=True,
            stage=StageKind.AGGREGATE.value,
            boundary_id="aggregate-completed",
        )
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
        eligible_concurrency = (
            StageCoordinator._stage_concurrency(self, lock, packets[0].stage, first)
            if isinstance(packets[0], StagePacket)
            else first.total_concurrency
        )
        if (
            first.type is not ExecutorType.ENDPOINT
            or any(configuration.id != first.id for configuration in configurations)
            or eligible_concurrency <= 1
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
        maximum_workers = min(total, eligible_concurrency)

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

    def _execute_solve_judge_pipeline(
        self,
        solves: Sequence[StagePacket],
        lock: ExperimentLock,
        session: _ExecutionSession,
        judge_validation: StagePacket,
    ) -> tuple[tuple[StagePacket, ...], StagePacket | None]:
        """Execute solve -> judge edges without a batch-wide barrier.

        Endpoint pools remain the source of truth for capacity. This scheduler
        limits each stage to its eligible capacity, gives newly-ready judgments
        priority over additional solves, and creates a judgment only after its
        individual solve has persisted.
        """

        if not solves:
            return (), None
        if any(packet.stage is not StageKind.SOLVE for packet in solves):
            raise IntegrityError("solve/judge pipeline accepts only solve packets")

        solve_executor = self._executor(solves[0])
        if any(self._executor(packet).id != solve_executor.id for packet in solves):
            raise IntegrityError("solve/judge pipeline requires one solve executor")
        solve_capacity = self._stage_concurrency(
            lock,
            StageKind.SOLVE,
            solve_executor,
        )

        judge_model = lock.models.get("judge") or resolve_stage_model(
            self.workspace.repository.workspace,
            StageKind.JUDGE.value,
            experiment_id=lock.configuration.id,
            role="judge",
        )
        if judge_model is None:
            raise ConfigurationError("stage 'judge' has no model")
        judge_executor = self.workspace.repository.workspace.executors[judge_model.executor_id]
        judge_capacity = self._stage_concurrency(
            lock,
            StageKind.JUDGE,
            judge_executor,
        )
        maximum_workers = min(
            len(solves) * 2,
            (
                max(solve_capacity, judge_capacity)
                if solve_executor.id == judge_executor.id
                else solve_capacity + judge_capacity
            ),
        )
        maximum_workers = max(1, maximum_workers)

        total = len(solves)
        next_solve = 0
        active_solves = 0
        active_judges = 0
        ready_judges: deque[tuple[int, StagePacket]] = deque()
        judges_by_position: dict[int, StagePacket] = {}
        futures: dict[Future[bool], tuple[str, int, StagePacket]] = {}
        failures: list[tuple[int, StagePacket]] = []
        errors: list[tuple[int, Exception]] = []
        stopped = False

        def execute(
            kind: str,
            position: int,
            packet: StagePacket,
        ) -> bool:
            worker_session = _ExecutionSession(self, lock, session.environment)
            return self._ensure_executed(
                packet,
                lock,
                worker_session,
                position=position,
                total=total,
            )

        def submit_ready(pool: ThreadPoolExecutor) -> None:
            nonlocal next_solve, active_solves, active_judges
            while (
                not stopped
                and ready_judges
                and active_judges < judge_capacity
                and len(futures) < maximum_workers
            ):
                position, packet = ready_judges.popleft()
                future = pool.submit(execute, "judge", position, packet)
                futures[future] = ("judge", position, packet)
                active_judges += 1
            while (
                not stopped
                and next_solve < total
                and active_solves < solve_capacity
                and len(futures) < maximum_workers
            ):
                position = next_solve + 1
                packet = solves[next_solve]
                next_solve += 1
                future = pool.submit(execute, "solve", position, packet)
                futures[future] = ("solve", position, packet)
                active_solves += 1

        with ThreadPoolExecutor(
            max_workers=maximum_workers,
            thread_name_prefix="ms-agent-eval-solve-judge",
        ) as pool:
            submit_ready(pool)
            while futures:
                completed, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in completed:
                    kind, position, packet = futures.pop(future)
                    if kind == "solve":
                        active_solves -= 1
                    else:
                        active_judges -= 1
                    try:
                        succeeded = future.result()
                    except Exception as error:
                        errors.append((position, error))
                        stopped = True
                        continue
                    if not succeeded:
                        failures.append((position, packet))
                        stopped = True
                        continue
                    if kind == "solve" and not stopped:
                        judge = self._judge_packets(
                            packet.run_id,
                            lock,
                            judge_validation,
                            (packet,),
                        )[0]
                        judges_by_position[position] = judge
                        ready_judges.append((position, judge))
                submit_ready(pool)

        if errors:
            raise min(errors, key=lambda item: item[0])[1]
        if failures:
            return (), min(failures, key=lambda item: item[0])[1]
        if len(judges_by_position) != total:
            raise IntegrityError("solve/judge pipeline did not materialize every judgment")
        return tuple(judges_by_position[position] for position in range(1, total + 1)), None

    def _stage_concurrency(
        self,
        lock: ExperimentLock,
        stage: StageKind,
        executor: ExecutorConfiguration,
    ) -> int:
        """Return the endpoint capacity eligible for the model driving a stage."""

        if executor.type is not ExecutorType.ENDPOINT:
            return 1
        role = (
            "judge"
            if stage in {StageKind.VALIDATE_JUDGE, StageKind.JUDGE}
            else (
                "solver"
                if stage is StageKind.SOLVE
                else ("optimizer" if stage is StageKind.OPTIMIZE else stage.value)
            )
        )
        model = lock.models.get(role) or resolve_stage_model(
            self.workspace.repository.workspace,
            stage.value,
            experiment_id=lock.configuration.id,
            role=role,
        )
        if model is None:
            return executor.total_concurrency
        capacity = sum(
            endpoint.max_concurrent_requests for endpoint in model.eligible_endpoints(executor)
        )
        if capacity <= 0:
            raise ConfigurationError(
                f"deployment {model.deployment_id!r} has no eligible endpoint capacity "
                f"for stage {stage.value!r}"
            )
        return capacity

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
        if status.state is StageState.PREPARED and packet.attempt > 1:
            self._emit_packet(
                "packet_retrying",
                packet,
                position=position,
                total=total,
                details={
                    "attempt": packet.attempt,
                },
            )
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

    @staticmethod
    def _retryable_endpoint_failure(result: StageResult | None) -> bool:
        """Permit explicit resume to create one new attempt for transient I/O failures."""

        return bool(
            result is not None
            and result.error is not None
            and result.error.kind
            in {"connection_error", "provider_server_error", "rate_limit_error"}
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
        source_output = _mapping(self.ledger.read_output(source), "reusable stage output")
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
        self._recording_checkpoint(packet.run_id)
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
                "candidate_id",
                "candidate_k",
                "strategy",
            ):
                value[field] = current[field]
        elif packet.stage is StageKind.FIT:
            current = _mapping(self._input(packet, "training_set"), "fit input")
            context = _mapping(current.get("context"), "fit input context")
            value["phase"] = context["phase"]
            value["validation_assignment_hash"] = context["validation_assignment_hash"]
        elif packet.stage is StageKind.JUDGE:
            current = _mapping(self._input(packet, "evaluation"), "judge input")
            value["case_id"] = current["case_id"]
            value["split"] = current["split"]
            value["repetition"] = current["repetition"]
            value["evaluation_phase"] = current["evaluation_phase"]
            value["candidate_id"] = current["candidate_id"]
            value["candidate_k"] = current["candidate_k"]
            value["strategy"] = current["strategy"]
        elif packet.stage is StageKind.OPTIMIZE:
            current = _mapping(self._input(packet, "optimizer_context"), "optimizer context")
            value["target_kind"] = current["target_kind"]
            value["target_id"] = current["target_id"]
            value["round"] = current["round"]
        elif packet.stage is StageKind.SELECT and packet.output_schema == "copro-selection-v1":
            current = _mapping(self._input(packet, "candidate_selection"), "candidate selection")
            value["scope"] = current["scope"]
            value["round"] = current["round"]
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
            execution["executed_this_run"] = max(0, int(execution.get("executed_this_run", 1)) - 1)
            execution["reused_this_run"] = int(execution.get("reused_this_run", 0)) + 1
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
                endpoint_details = {
                    "pool_capacity": self._stage_concurrency(lock, packet.stage, configuration)
                }
                if packet.stage is StageKind.VALIDATE_JUDGE:
                    endpoint_details["request_count"] = len(
                        self.workspace.judge_validation.fixtures
                    ) * self._judge_repetitions(packet.run_id)
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
            self._recording_checkpoint(packet.run_id)
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
            self._recording_checkpoint(packet.run_id)
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
            self._recording_checkpoint(packet.run_id)
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
        self._recording_checkpoint(packet.run_id)
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

    def _record_packet_terminal(
        self,
        packet: StagePacket,
        result: StageResult | None,
    ) -> None:
        state = RunLifecycleState.COMPLETED
        reason = "aggregate_completed"
        if packet.stage is StageKind.VALIDATE_JUDGE and result is not None:
            output = _mapping(self.ledger.read_output(result), "judge validation output")
            if output.get("ordering_consistent") is False:
                state = RunLifecycleState.FAILED
                reason = "judge_validation_failed"
        self.lifecycle.append(
            packet.run_id,
            state,
            reason=reason,
            details={"packet_id": packet.packet_id},
            if_changed=True,
        )
        self._recording_checkpoint(packet.run_id, force=True, flush=True)

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
                "skill": value.get("skill")
                if isinstance(value.get("skill"), str)
                else (None if case is None else case.skill),
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
                "details": {"fixture_count": len(fixtures) if isinstance(fixtures, list) else 0}
            }
        if packet.stage is StageKind.FIT:
            value = _mapping(self._input(packet, "training_set"), "fit input")
            cases = value.get("cases")
            return {
                "details": {"training_case_count": len(cases) if isinstance(cases, list) else 0}
            }
        return {}

    def _run_case_count(self, configuration: object, *, pilot: bool) -> int:
        selected_splits = {"train"} if pilot else {"test"}
        count = sum(
            self.workspace.splits.split_for(case) in selected_splits
            for case in self.workspace.case_bank.cases
        )
        if (
            not pilot
            and getattr(configuration, "mode", None) is ExperimentMode.OPTIMIZE
            and isinstance(getattr(configuration, "optimizer", None), CoproConfiguration)
        ):
            return count * 2
        return count

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
        if stage is StageKind.OPTIMIZE:
            candidates = output.get("candidates")
            accounting = output.get("proposal_accounting")
            return {
                "target_id": output.get("target_id"),
                "round": output.get("round"),
                "candidate_count": (
                    len(candidates)
                    if isinstance(candidates, Sequence)
                    and not isinstance(candidates, (str, bytes, bytearray))
                    else None
                ),
                **(
                    {
                        "requested_candidate_count": accounting.get("requested_count"),
                        "proposal_shortfall_count": accounting.get("shortfall_count"),
                        "proposal_degraded": accounting.get("degraded"),
                    }
                    if isinstance(accounting, Mapping)
                    else {}
                ),
            }
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
        pool_capacity = raw_pool.get("total_concurrency", configuration.total_concurrency)
        endpoint_id = raw_pool.get("endpoint_id")
        if isinstance(endpoint_id, str):
            preflight = raw_pool.get("last_preflight")
            return {
                "endpoint_id": endpoint_id,
                "pool_capacity": pool_capacity,
                "preflight_status": (
                    preflight.get("status") if isinstance(preflight, Mapping) else None
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
                if isinstance(route, Mapping) and isinstance(route.get("endpoint_id"), str)
            ]
            if isinstance(routes, Sequence) and not isinstance(routes, (str, bytes, bytearray))
            else []
        )
        return {
            "pool_capacity": pool_capacity,
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

            return CodexCliExecutor(self.workspace, lock, executor).execute(packet, self.ledger)
        if executor.adapter == "local":
            return ExecutorOutput(self._execute_local(packet, lock), {}, executor.version, None)
        if executor.adapter != "openai_compatible":
            raise ConfigurationError(f"unsupported executor adapter: {executor.adapter}")
        if packet.stage is StageKind.AUTHOR:
            return self._execute_author(packet, session)
        if packet.stage is StageKind.VALIDATE_JUDGE:
            return self._execute_judge_validation(packet, lock, session)
        if packet.stage is StageKind.OPTIMIZE:
            return self._execute_optimize(packet, lock, session)
        if packet.stage is StageKind.SOLVE:
            return self._execute_solve(packet, lock, session)
        if packet.stage is StageKind.JUDGE:
            return self._execute_judge(packet, lock, session)
        raise ConfigurationError(
            f"openai_compatible cannot automatically execute {packet.stage.value}"
        )

    def _execute_optimize(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> ExecutorOutput:
        optimizer = lock.configuration.optimizer
        if not isinstance(optimizer, CoproConfiguration):
            raise IntegrityError("optimize packets are valid only for COPRO experiments")
        values = _mapping(self._input(packet, "optimizer_context"), "optimizer context")
        program = create_copro_proposal_program()
        lm, observer = session.lm("optimizer", packet)
        start = len(observer.records)
        result = self.dspy.execute(
            contract=DspyExecutionContract(
                role="optimizer",
                program_hash=program_hash(program),
                inputs={
                    "target_kind": values["target_kind"],
                    "target_id": values["target_id"],
                    "current_text": values["current_text"],
                    "current_output_prefix": values["current_output_prefix"],
                    "scored_attempts": json.dumps(
                        values.get("scored_attempts", []),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "requirements": values["requirements"],
                    "candidate_count": values["candidate_count"],
                },
                required_outputs=("candidates",),
                primary_output=None,
                call_parameters={"temperature": optimizer.init_temperature},
            ),
            program=program,
            lm=lm,
            observer=observer,
        )
        if result.status != "completed":
            raise _dspy_execution_failure("optimizer", result)
        raw_candidates = _list(result.outputs.get("candidates"), "optimizer candidates")
        expected_count = _integer(values.get("candidate_count"), "optimizer candidate_count")
        target_kind = _string(values.get("target_kind"), "optimizer target_kind")
        candidates, proposal_accounting = normalize_copro_proposals(
            raw_candidates,
            requested_count=expected_count,
            target_kind=target_kind,
            current_text=_string(values.get("current_text"), "optimizer current_text"),
            current_output_prefix=_text(
                values.get("current_output_prefix", ""),
                "optimizer current_output_prefix",
            ),
        )
        return ExecutorOutput(
            {
                "strategy": "copro",
                "operation": "propose",
                "target_kind": target_kind,
                "target_id": values["target_id"],
                "round": values["round"],
                "candidates": candidates,
                "proposal_accounting": proposal_accounting,
                "proposal_hash": content_hash(candidates),
            },
            _usage(observer.records[start:]),
            None,
            lock.models["optimizer"].model,
        )

    def _execute_author(self, packet: StagePacket, session: _ExecutionSession) -> ExecutorOutput:
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
            and self._stage_concurrency(lock, packet.stage, configuration) > 1
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
        jobs: list[tuple[int, JudgeValidationFixture, CaseDefinition, str, str, int]] = []
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

        maximum_workers = min(
            len(jobs),
            self._stage_concurrency(lock, packet.stage, configuration),
        )
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
                while not stopped and next_index < len(jobs) and len(futures) < maximum_workers:
                    future = workers.submit(execute_vote, jobs[next_index])
                    futures[future] = next_index
                    next_index += 1
            executions = tuple(sorted(completed_executions, key=lambda item: item.position))

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
                "candidate_id": values["candidate_id"],
                "candidate_k": values["candidate_k"],
                "strategy": values["strategy"],
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
                "candidate_id": values["candidate_id"],
                "candidate_k": values["candidate_k"],
                "strategy": values["strategy"],
            },
            _usage(observer.records[start:]),
            None,
            lock.models["judge"].model,
        )

    def _execute_local(self, packet: StagePacket, lock: ExperimentLock) -> Mapping[str, object]:
        if packet.stage is StageKind.FIT:
            experiment = lock.configuration
            if experiment.optimizer is None:
                raise IntegrityError("fit stage has no optimizer")
            configuration = _mapping(packet.work_spec.get("candidate"), "fit candidate")
            candidate_id = _string(configuration.get("id"), "fit candidate id")
            training_input = _mapping(self._input(packet, "training_set"), "training set")
            training_cases = _list(training_input.get("cases"), "training cases")
            fit_context = _mapping(training_input.get("context"), "fit context")
            phase = _string(fit_context.get("phase"), "fit phase")
            validation_assignment_hash = _string(
                fit_context.get("validation_assignment_hash"),
                "validation assignment hash",
            )
            validation = experiment.validation
            if validation is None:
                raise IntegrityError("fit stage has no validation configuration")
            if (phase == "final_fit") is not (
                packet.work_spec.get("materialize_final_artifacts") is True
            ):
                raise IntegrityError("fit context and semantic finalization flag disagree")
            compiled = create_solver_program()
            skill_overlays: Mapping[str, str] = {}
            global_context = self.workspace.context.global_context
            global_instruction_overlay = ""
            search_space_type: str | None = None
            if isinstance(experiment.optimizer, LabeledFewShotConfiguration):
                raw_k = _integer(configuration.get("k"), "fit candidate k")
                if set(configuration) != {"id", "k"}:
                    raise IntegrityError("labeled_few_shot candidate contains invalid fields")
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
                previous = random.getstate()
                try:
                    random.seed(f"{validation.seed}:{candidate_id}")
                    if raw_k:
                        compiled = dspy.LabeledFewShot(k=raw_k).compile(
                            compiled, trainset=train, sample=True
                        )
                finally:
                    random.setstate(previous)
                strategy = "labeled_few_shot"
            elif isinstance(experiment.optimizer, CoproConfiguration):
                raw_k = 0
                if set(configuration) != {"id", "bundle"}:
                    raise IntegrityError("COPRO candidate contains invalid fields")
                bundle = _mapping(configuration.get("bundle"), "COPRO candidate bundle")
                instruction = _string(bundle.get("instruction"), "COPRO solver instruction")
                prefix = _string(bundle.get("output_prefix"), "COPRO output prefix")
                global_context = _string(
                    bundle.get("global_context"), "COPRO effective global context"
                )
                global_instruction_overlay = _text(
                    bundle.get("global_overlay", ""), "COPRO global instruction overlay"
                )
                skill_overlays = {
                    _string(skill_id, "COPRO skill id"): _string(text, "COPRO skill text")
                    for skill_id, text in _mapping(
                        bundle.get("skills", {}), "COPRO skill overlays"
                    ).items()
                }
                frozen_instruction, frozen_prefix = solver_prompt_state(compiled)
                if (instruction, prefix) != (frozen_instruction, frozen_prefix):
                    raise IntegrityError(
                        "COPRO cannot change the frozen DSPy solver instruction or output prefix"
                    )
                search_space_type = experiment.optimizer.search_space_type
                if search_space_type == "skills":
                    if global_context != self.workspace.context.global_context:
                        raise IntegrityError(
                            "skill optimization cannot change global instructions"
                        )
                    if global_instruction_overlay:
                        raise IntegrityError(
                            "skill optimization cannot contain a global instruction overlay"
                        )
                    allowed_skills = set(self._selected_copro_skills(experiment.optimizer))
                    unknown_overlays = sorted(set(skill_overlays) - allowed_skills)
                    if unknown_overlays:
                        raise IntegrityError(
                            f"COPRO candidate contains unselected skill overlays: {unknown_overlays}"
                        )
                elif search_space_type == "global_instructions":
                    if skill_overlays:
                        raise IntegrityError(
                            "global-instruction optimization cannot contain skill overlays"
                        )
                    if not global_instruction_overlay:
                        raise IntegrityError(
                            "global-instruction optimization requires one global bundle"
                        )
                    expected_global = self._effective_global_context(
                        experiment.optimizer,
                        global_instruction_overlay,
                    )
                    if global_context != expected_global:
                        raise IntegrityError(
                            "COPRO effective global context does not match its selected bundle"
                        )
                else:  # pragma: no cover - workspace parser closes the union
                    raise IntegrityError("unsupported COPRO search space")
                if any(predictor.demos for _, predictor in compiled.named_predictors()):
                    raise IntegrityError("COPRO compiled programs must remain zero-shot")
                strategy = "copro"
            else:  # pragma: no cover - closed configuration union
                raise IntegrityError("unsupported optimizer configuration")
            state = self.dspy.save_state_artifact(compiled)
            overlay_artifacts = {
                skill_id: json_value(
                    self.artifacts.put_blob(
                        BytesIO(text.encode("utf-8")),
                        "text/markdown",
                    )
                )
                for skill_id, text in sorted(skill_overlays.items())
            }
            instruction, output_prefix = solver_prompt_state(compiled)
            instruction_artifact = self.artifacts.put_blob(
                BytesIO(instruction.encode("utf-8")), "text/markdown"
            )
            prefix_artifact = self.artifacts.put_blob(
                BytesIO(output_prefix.encode("utf-8")), "text/plain"
            )
            global_instruction_artifact = (
                self.artifacts.put_blob(
                    BytesIO(global_instruction_overlay.encode("utf-8")),
                    "text/markdown",
                )
                if global_instruction_overlay
                else None
            )
            source_skill_hashes = {
                item.id: item.content_hash
                for item in self.workspace.context.instruction_units
            }
            frozen_skill_text_hashes = {
                skill_id: content_hash(text)
                for skill_id, text in sorted(
                    self.workspace.context.skill_contexts.items()
                )
            }
            effective_skill_hashes = {
                skill_id: content_hash(skill_overlays.get(skill_id, text))
                for skill_id, text in sorted(
                    self.workspace.context.skill_contexts.items()
                )
            }
            frozen_skill_bank_hash = content_hash(frozen_skill_text_hashes)
            effective_skill_bank_hash = content_hash(effective_skill_hashes)
            compiled_manifest = None
            candidate_lineage_artifact = None
            search_summary_artifact = None
            if phase == "final_fit":
                selection_reference = None
                if len(packet.dependencies) > 1:
                    selection_status = self.ledger.status(packet.dependencies[-1])
                    if selection_status.result is not None:
                        selection_reference = selection_status.result.outputs[0].content_id
                selected_evidence = None
                if strategy == "copro":
                    lineage = []
                    for item in self.ledger.list(packet.run_id):
                        if (
                            item.packet.output_schema != "copro-selection-v1"
                            or item.state is not StageState.PERSISTED
                            or item.result is None
                        ):
                            continue
                        selection_output = _mapping(
                            self.ledger.read_output(item.result), "COPRO selection output"
                        )
                        if selection_output.get("scope") != "optimization":
                            continue
                        output = item.result.outputs[0]
                        lineage.append(
                            {
                                "scope": selection_output.get("scope"),
                                "round": selection_output.get("round"),
                                "target_kind": selection_output.get("target_kind"),
                                "target_id": selection_output.get("target_id"),
                                "current_candidate_id": selection_output.get(
                                    "current_candidate_id"
                                ),
                                "selected_candidate_id": selection_output.get(
                                    "selected_candidate_id"
                                ),
                                "selected_mean_score": selection_output.get("selected_mean_score"),
                                "candidates": selection_output.get("candidates"),
                                "proposal_accounting": selection_output.get("proposal_accounting"),
                                "selection_artifact": json_value(
                                    self.artifacts.reference(output.content_id, output.media_type)
                                ),
                            }
                        )
                    lineage.sort(
                        key=lambda item: (
                            int(item.get("round") or 0),
                            0 if item.get("target_kind") == "global_instructions" else 1,
                            str(item.get("target_id")),
                        )
                    )
                    if not lineage:
                        raise IntegrityError("final COPRO fit has no candidate lineage")
                    final_round = max(int(item.get("round") or 0) for item in lineage)
                    terminal_selections = [
                        item["selection_artifact"]
                        for item in lineage
                        if int(item.get("round") or 0) == final_round
                    ]
                    selected_evidence = {
                        "selected_candidate_id": candidate_id,
                        "selected_bundle_hash": content_hash(configuration.get("bundle")),
                        "search_space_type": search_space_type,
                        "round": final_round,
                        "terminal_selection_artifacts": terminal_selections,
                    }
                    candidate_lineage_artifact = self.artifacts.put_blob(
                        BytesIO(canonical_json_bytes({"schema_version": 1, "rounds": lineage})),
                        "application/json",
                    )
                    search_summary_artifact = self.artifacts.put_blob(
                        BytesIO(
                            canonical_json_bytes(
                                {
                                    "schema_version": 1,
                                    "strategy": "copro",
                                    "configuration": json_value(experiment.optimizer),
                                    "round_count": len(lineage),
                                    "selected_candidate_id": candidate_id,
                                    "selected_evidence": selected_evidence,
                                }
                            )
                        ),
                        "application/json",
                    )
                compiled_manifest = self.artifacts.put_manifest(
                    "experiments/"
                    f"{lock.configuration.id}/"
                    f"{lock.content_hash.removeprefix('sha256:')}/"
                    f"compiled-program/{candidate_id}/"
                    f"{content_hash(selection_reference or 'unselected').removeprefix('sha256:')}",
                    {
                        "schema_version": 2,
                        "strategy": strategy,
                        "search_space_type": search_space_type,
                        "candidate_id": candidate_id,
                        "base_solver_program_hash": lock.solver_program_hash,
                        "fitted_program_hash": program_hash(compiled),
                        "state_artifact": json_value(state),
                        "solver_instruction_artifact": json_value(instruction_artifact),
                        "output_prefix_artifact": json_value(prefix_artifact),
                        "global_instruction_artifact": json_value(
                            global_instruction_artifact
                        ),
                        "frozen_global_context_hash": content_hash(
                            self.workspace.context.global_context
                        ),
                        "effective_global_context_hash": content_hash(global_context),
                        "global_instruction_files": (
                            list(self._selected_global_instruction_files(experiment.optimizer))
                            if isinstance(experiment.optimizer, CoproConfiguration)
                            and experiment.optimizer.search_space_type
                            == "global_instructions"
                            else []
                        ),
                        "source_global_hashes": {
                            item.source_path: item.content_hash
                            for item in self.workspace.snapshot.files
                            if item.source_path
                            in set(
                                self.workspace.repository.workspace.evaluation.instructions.global_paths
                            )
                        },
                        "skill_overlay_artifacts": overlay_artifacts,
                        "candidate_lineage_artifact": json_value(candidate_lineage_artifact),
                        "search_summary_artifact": json_value(search_summary_artifact),
                        "selected_candidate_evidence": selected_evidence,
                        "source_skill_hashes": source_skill_hashes,
                        "frozen_skill_text_hashes": frozen_skill_text_hashes,
                        "effective_skill_hashes": effective_skill_hashes,
                        "frozen_skill_bank_hash": frozen_skill_bank_hash,
                        "effective_skill_bank_hash": effective_skill_bank_hash,
                        "optimized_skill_ids": sorted(skill_overlays),
                        "demonstration_count": sum(
                            len(predictor.demos) for _, predictor in compiled.named_predictors()
                        ),
                        "training_case_ids": [
                            _mapping(item, "training case").get("case_id")
                            for item in training_cases
                        ],
                        "training_case_hash": content_hash(packet.work_spec.get("training_cases")),
                        "validation_assignment_hash": validation_assignment_hash,
                        "selection_result": selection_reference,
                        "models": json_value(lock.models),
                        "optimizer_implementation": (
                            "staged-copro-v1" if strategy == "copro" else "dspy-labeled-few-shot-v1"
                        ),
                        "dspy_version": str(dspy.__version__),
                    },
                )
            return {
                "state_artifact": json_value(state),
                "fitted_program_hash": program_hash(compiled),
                "training_case_hash": content_hash(packet.work_spec.get("training_cases")),
                "validation_assignment_hash": validation_assignment_hash,
                "candidate_id": candidate_id,
                "candidate_k": raw_k,
                "strategy": strategy,
                "search_space_type": search_space_type,
                "solver_instruction": instruction,
                "output_prefix": output_prefix,
                "skill_overlays": dict(skill_overlays),
                "skill_overlay_artifacts": overlay_artifacts,
                "solver_instruction_artifact": json_value(instruction_artifact),
                "output_prefix_artifact": json_value(prefix_artifact),
                "global_context": global_context,
                "frozen_global_context_hash": content_hash(
                    self.workspace.context.global_context
                ),
                "effective_global_context_hash": content_hash(global_context),
                "global_instruction_overlay": global_instruction_overlay,
                "global_instruction_artifact": json_value(global_instruction_artifact),
                "frozen_skill_bank_hash": frozen_skill_bank_hash,
                "effective_skill_bank_hash": effective_skill_bank_hash,
                "compiled_manifest_artifact": (
                    json_value(compiled_manifest) if compiled_manifest is not None else None
                ),
                "candidate_lineage_artifact": (
                    json_value(candidate_lineage_artifact)
                    if candidate_lineage_artifact is not None
                    else None
                ),
                "search_summary_artifact": (
                    json_value(search_summary_artifact)
                    if search_summary_artifact is not None
                    else None
                ),
                "demonstration_count": sum(
                    len(predictor.demos) for _, predictor in compiled.named_predictors()
                ),
                "phase": phase,
                "state_format": "json",
            }
        if packet.stage is StageKind.SELECT:
            if packet.output_schema == "copro-selection-v1":
                selection = _mapping(
                    self._input(packet, "candidate_selection"), "candidate selection input"
                )
                candidates = {
                    _string(candidate.get("id"), "COPRO candidate id"): _mapping(
                        candidate.get("bundle"), "COPRO candidate bundle"
                    )
                    for candidate in (
                        _mapping(raw, "COPRO candidate")
                        for raw in _list(selection.get("candidates"), "COPRO candidates")
                    )
                }
                expected_cases = {
                    _string(case_id, "COPRO expected case id")
                    for case_id in _list(selection.get("expected_case_ids"), "COPRO expected cases")
                }
                by_candidate: dict[str, list[Mapping[str, object]]] = {}
                for raw in _list(selection.get("results"), "COPRO selection results"):
                    result = _mapping(raw, "COPRO selection result")
                    candidate_id = _string(result.get("candidate_id"), "COPRO candidate id")
                    if candidate_id not in candidates:
                        raise IntegrityError("COPRO result references an unknown candidate")
                    by_candidate.setdefault(candidate_id, []).append(result)
                summaries = []
                target_kind = _string(selection.get("target_kind"), "COPRO target kind")
                target_id = _string(selection.get("target_id"), "COPRO target id")
                for candidate_id, bundle in sorted(candidates.items()):
                    results = by_candidate.get(candidate_id, [])
                    case_ids = [_string(item.get("case_id"), "COPRO case id") for item in results]
                    if len(case_ids) != len(set(case_ids)) or set(case_ids) != expected_cases:
                        raise IntegrityError(
                            f"COPRO candidate {candidate_id!r} must have one score per permitted case"
                        )
                    scores = [_number(item.get("score"), "COPRO score") for item in results]
                    text = (
                        _string(
                            bundle.get("global_overlay"),
                            "COPRO global instruction bundle",
                        )
                        if target_kind == "global_instructions"
                        else _string(
                            _mapping(bundle.get("skills", {}), "COPRO skills").get(
                                target_id, self.workspace.context.skill(target_id)
                            ),
                            "COPRO skill text",
                        )
                    )
                    summaries.append(
                        {
                            "candidate_id": candidate_id,
                            "mean_score": sum(scores) / len(scores),
                            "case_count": len(scores),
                            "text": text,
                            "output_prefix": (
                                ""
                            ),
                            "text_hash": content_hash(text),
                            "normalized_length": len(" ".join(text.split())),
                        }
                    )
                current_id = _string(
                    selection.get("current_candidate_id"), "current COPRO candidate id"
                )
                selected = min(
                    summaries,
                    key=lambda item: (
                        -float(item["mean_score"]),
                        0 if item["candidate_id"] == current_id else 1,
                        int(item["normalized_length"]),
                        str(item["candidate_id"]),
                    ),
                )
                selected_id = str(selected["candidate_id"])
                return {
                    "strategy": "copro",
                    "operation": "copro_round",
                    "scope": selection["scope"],
                    "round": selection["round"],
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "current_candidate_id": current_id,
                    "candidates": summaries,
                    "selected_candidate_id": selected_id,
                    "selected_mean_score": selected["mean_score"],
                    "selected_bundle": dict(candidates[selected_id]),
                    "tie_break": "current_then_shorter_then_content_hash",
                    **(
                        {
                            "proposal_accounting": dict(
                                _mapping(
                                    selection.get("proposal_accounting"),
                                    "COPRO proposal accounting",
                                )
                            )
                        }
                        if selection.get("proposal_accounting") is not None
                        else {}
                    ),
                }
            validation = _mapping(self._input(packet, "validation"), "validation input")
            raw_results = _list(validation.get("results"), "validation results")
            by_candidate: dict[str, list[Mapping[str, object]]] = {}
            for raw_result in raw_results:
                result = _mapping(raw_result, "validation result")
                candidate_id = _string(result.get("candidate_id"), "candidate id")
                by_candidate.setdefault(candidate_id, []).append(result)
            expected_cases = {
                _string(case_id, "validation case id")
                for case_id in _list(validation.get("validation_case_ids"), "validation case ids")
            }
            summaries = []
            for candidate_id, results in sorted(by_candidate.items()):
                case_ids = [_string(item.get("case_id"), "case id") for item in results]
                if len(case_ids) != len(set(case_ids)) or set(case_ids) != expected_cases:
                    raise IntegrityError(
                        f"candidate {candidate_id!r} must have exactly one score per validation case"
                    )
                k_values = {_integer(item.get("k"), "candidate k") for item in results}
                if len(k_values) != 1:
                    raise IntegrityError("candidate validation results disagree on k")
                scores = [_number(item.get("score"), "validation score") for item in results]
                summaries.append(
                    {
                        "candidate_id": candidate_id,
                        "k": next(iter(k_values)),
                        "mean_score": sum(scores) / len(scores),
                        "case_count": len(scores),
                    }
                )
            if not summaries:
                raise IntegrityError("validation selection requires candidate results")
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
                "validation_assignment_hash": _string(
                    validation.get("validation_assignment_hash"),
                    "validation assignment hash",
                ),
                "candidates": summaries,
                "selected_candidate_id": selected["candidate_id"],
                "selected_k": selected["k"],
                "selected_mean_score": selected["mean_score"],
            }
        if packet.stage is StageKind.AGGREGATE:
            evaluation_index = _mapping(self._input(packet, "evaluation_index"), "evaluation index")
            expected_response_tokens = _mapping(
                evaluation_index.get("expected_response_tokens", {}),
                "expected response token measurements",
            )
            token_measurements = {
                _string(item.get("case_id"), "token measurement case id"): item
                for raw_item in _list(
                    expected_response_tokens.get("cases", []),
                    "expected response token measurement cases",
                )
                for item in [_mapping(raw_item, "expected response token measurement")]
            }
            cases = []
            graph_nodes = []
            execution_by_stage: dict[str, Counter[str]] = {}
            workspace = self.workspace.repository.workspace
            eligible_deployments: dict[str, set[str]] = defaultdict(set)
            eligible_endpoints: dict[str, tuple[ExecutorConfiguration, EndpointConfiguration]] = {}
            for role_model in lock.models.values():
                role_executor = workspace.executors[role_model.executor_id]
                if role_executor.type is not ExecutorType.ENDPOINT:
                    continue
                for role_endpoint in role_model.eligible_endpoints(role_executor):
                    endpoint_key = f"{role_executor.id}/{role_endpoint.id}"
                    eligible_endpoints[endpoint_key] = (role_executor, role_endpoint)
                    eligible_deployments[endpoint_key].add(role_model.deployment_id)
            endpoint_execution: dict[str, dict[str, object]] = {
                key: {
                    "executor_id": executor.id,
                    "endpoint_id": endpoint.id,
                    "base_url": endpoint.base_url,
                    "pool_hash": executor.pool_hash,
                    "endpoint_configuration_hash": endpoint.configuration_hash,
                    "configured_capacity": endpoint.max_concurrent_requests,
                    "deployments": sorted(eligible_deployments[key]),
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
                for key, (executor, endpoint) in eligible_endpoints.items()
            }
            role_usage: dict[str, Counter[str]] = {
                "optimizer": Counter(),
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
                execution_by_stage.setdefault(status.packet.stage.value, Counter())[
                    disposition
                ] += 1
                raw_endpoint = status.result.provenance.get("endpoint_pool")
                if isinstance(raw_endpoint, Mapping):
                    for route in endpoint_provenance_routes(
                        raw_endpoint,
                        status.result.usage,
                    ):
                        endpoint_id = _string(route.get("endpoint_id"), "endpoint provenance id")
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
                                "configured_capacity": route.get("max_concurrent_requests"),
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
                            if isinstance(raw_count, int) and not isinstance(raw_count, bool):
                                endpoint[preflight_key] = int(endpoint[preflight_key]) + raw_count
                        for usage_key in (
                            "model_calls",
                            "completed_calls",
                            "failed_calls",
                        ):
                            raw_usage = route.get(usage_key, 0)
                            if isinstance(raw_usage, int) and not isinstance(raw_usage, bool):
                                endpoint[usage_key] = int(endpoint[usage_key]) + raw_usage
                        for duration_key in (
                            "latency_seconds",
                            "queue_wait_seconds",
                        ):
                            raw_duration = route.get(duration_key, 0.0)
                            if isinstance(raw_duration, (int, float)) and not isinstance(
                                raw_duration, bool
                            ):
                                endpoint[duration_key] = float(endpoint[duration_key]) + float(
                                    raw_duration
                                )
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
                if status.packet.stage is StageKind.OPTIMIZE:
                    role = "optimizer"
                elif status.packet.stage is StageKind.SOLVE:
                    role = "solver"
                elif status.packet.stage in {StageKind.VALIDATE_JUDGE, StageKind.JUDGE}:
                    role = "judge"
                if status.packet.stage is StageKind.JUDGE:
                    evaluation = _mapping(self.ledger.read_output(status.result), "judge output")
                    if evaluation.get("evaluation_phase") in {"test", "pilot"}:
                        case_id = _string(evaluation.get("case_id"), "judge case id")
                        measurement = token_measurements.get(case_id)
                        cases.append(
                            {
                                **dict(evaluation),
                                "expected_response_tokens": (
                                    None if measurement is None else measurement.get("token_count")
                                ),
                                "expected_response_length_band": (
                                    None if measurement is None else measurement.get("length_band")
                                ),
                            }
                        )
                if role is not None:
                    for key, value in status.result.usage.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            role_usage[role][key] += value
            run = self.ledger.load_run(packet.run_id)
            execution_mode = _execution_mode(run)
            value: dict[str, object] = {
                "schema_version": 4,
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
                    {str(item["split"]) for item in cases if isinstance(item.get("split"), str)}
                ),
                "cases": cases,
                "case_count": len(cases),
                "expected_response_token_measurements": expected_response_tokens,
                "role_usage": {role: dict(values) for role, values in role_usage.items() if values},
                "execution": {
                    "executed_this_run": sum(
                        values["executed_this_run"] for values in execution_by_stage.values()
                    )
                    + 1,
                    "reused_this_run": sum(
                        values["reused_this_run"] for values in execution_by_stage.values()
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
                        endpoint.max_concurrent_requests
                        for _, endpoint in eligible_endpoints.values()
                    ),
                    "endpoints": [endpoint_execution[key] for key in sorted(endpoint_execution)],
                },
                "independent": (
                    lock.models["solver"].content_hash != lock.models["judge"].content_hash
                ),
            }
            if execution_mode == "benchmark":
                optimized = [
                    item
                    for item in cases
                    if item.get("strategy") != "baseline"
                    or not any(other.get("strategy") != "baseline" for other in cases)
                ]
                value["mean_score"] = (
                    sum(float(item["score"]) for item in optimized) / len(optimized)
                    if optimized
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

    def _candidate_configurations(self, lock: ExperimentLock) -> tuple[Mapping[str, object], ...]:
        optimizer = lock.configuration.optimizer
        if not isinstance(optimizer, LabeledFewShotConfiguration):
            raise IntegrityError("candidate k configurations require labeled_few_shot")
        return (
            {"id": "baseline", "k": 0},
            *({"id": f"labeled-few-shot-k-{k}", "k": k} for k in optimizer.k_values),
        )

    def _selected_copro_skills(self, optimizer: CoproConfiguration) -> tuple[str, ...]:
        if optimizer.search_space_type != "skills":
            return ()
        available = set(self.workspace.context.skill_contexts)
        selected = (
            tuple(sorted(available))
            if optimizer.skills == "all"
            else tuple(optimizer.skills)
        )
        unknown = sorted(set(selected) - available)
        if unknown:
            raise PreflightError(f"COPRO targets unknown skills: {unknown}")
        return selected

    def _selected_global_instruction_files(
        self, optimizer: CoproConfiguration
    ) -> tuple[str, ...]:
        if optimizer.search_space_type != "global_instructions":
            return ()
        configured = self.workspace.repository.workspace.evaluation.instructions.global_paths
        return (
            configured
            if optimizer.global_instruction_files == "all"
            else tuple(optimizer.global_instruction_files)
        )

    def _render_global_instruction_files(self, paths: Sequence[str]) -> str:
        parts = []
        for source_path in paths:
            try:
                path = self.workspace.context.source_files[source_path]
            except KeyError as error:
                raise IntegrityError(
                    f"global instruction source is absent from the frozen context: {source_path}"
                ) from error
            parts.append(f"# {source_path}\n\n" + path.read_text(encoding="utf-8"))
        value = "\n\n".join(parts)
        if not value.strip():
            raise IntegrityError("global instruction target must not be empty")
        return value

    def _effective_global_context(
        self,
        optimizer: CoproConfiguration,
        selected_bundle: str,
    ) -> str:
        selected = self._selected_global_instruction_files(optimizer)
        configured = self.workspace.repository.workspace.evaluation.instructions.global_paths
        if selected == configured:
            return selected_bundle
        selected_set = set(selected)
        parts: list[str] = []
        inserted = False
        for source_path in configured:
            if source_path in selected_set:
                if not inserted:
                    parts.append(selected_bundle)
                    inserted = True
                continue
            parts.append(self._render_global_instruction_files((source_path,)))
        if not inserted:
            raise IntegrityError("selected global instruction bundle was not inserted")
        return "\n\n".join(parts)

    def _base_copro_bundle(self, optimizer: CoproConfiguration) -> Mapping[str, object]:
        instruction, output_prefix = solver_prompt_state(create_solver_program())
        global_overlay = (
            self._render_global_instruction_files(
                self._selected_global_instruction_files(optimizer)
            )
            if optimizer.search_space_type == "global_instructions"
            else ""
        )
        return {
            "instruction": instruction,
            "output_prefix": output_prefix,
            "global_context": self.workspace.context.global_context,
            "global_overlay": global_overlay,
            "skills": {},
        }

    def _copro_targets(self, optimizer: CoproConfiguration) -> tuple[tuple[str, str], ...]:
        targets = (
            [("global_instructions", "global")]
            if optimizer.search_space_type == "global_instructions"
            else [("skill", skill_id) for skill_id in self._selected_copro_skills(optimizer)]
        )
        if not targets:
            raise PreflightError("COPRO has no enabled optimization target")
        return tuple(targets)

    @staticmethod
    def _copro_candidate(bundle: Mapping[str, object]) -> Mapping[str, object]:
        normalized = {
            "instruction": _string(bundle.get("instruction"), "COPRO bundle instruction"),
            "output_prefix": _string(bundle.get("output_prefix"), "COPRO bundle prefix"),
            "global_context": _string(
                bundle.get("global_context"), "COPRO bundle global context"
            ),
            "global_overlay": _text(
                bundle.get("global_overlay", ""), "COPRO bundle global overlay"
            ),
            "skills": {
                _string(skill_id, "COPRO bundle skill id"): _string(text, "COPRO bundle skill text")
                for skill_id, text in sorted(
                    _mapping(bundle.get("skills", {}), "COPRO bundle skills").items()
                )
            },
        }
        return {
            "id": f"copro-{content_hash(normalized).removeprefix('sha256:')[:16]}",
            "bundle": normalized,
        }

    def _copro_proposal_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        *,
        bundle: Mapping[str, object],
        target_kind: str,
        target_id: str,
        round_number: int,
        scope: str,
        fitting_cases: Sequence[object],
        history: Sequence[Mapping[str, object]],
        dependencies: Sequence[StagePacket],
    ) -> StagePacket:
        optimizer = lock.configuration.optimizer
        if not isinstance(optimizer, CoproConfiguration):
            raise IntegrityError("COPRO proposal requires COPRO configuration")
        skills = _mapping(bundle.get("skills", {}), "COPRO bundle skills")
        if target_kind == "global_instructions":
            current_text = _string(
                bundle.get("global_overlay"), "global instruction bundle"
            )
            current_prefix = ""
            selected_files = self._selected_global_instruction_files(optimizer)
            requirements = (
                "Rewrite only the configured global instruction bundle. Preserve its technical "
                "facts and repository-wide responsibilities. Do not rewrite skills or the DSPy "
                "signature. Selected source paths: "
                + ", ".join(selected_files)
            )
        else:
            current_text = _string(
                skills.get(target_id, self.workspace.context.skill(target_id)),
                "skill text",
            )
            current_prefix = ""
            requirements = (
                "Rewrite and reorganize this skill without dropping or inventing technical facts. "
                "The result must remain a self-contained skill for the same capability."
            )
        safe_history = [
            {
                "candidate_id": _string(item.get("candidate_id"), "history candidate id"),
                "mean_score": _number(item.get("mean_score"), "history mean score"),
                "text_hash": _string(item.get("text_hash"), "history text hash"),
                "text": _string(item.get("text"), "history candidate text"),
                "output_prefix": _text(
                    item.get("output_prefix", ""), "history candidate output prefix"
                ),
            }
            for item in history
        ]
        optimizer_context = {
            "strategy": "copro",
            "target_kind": target_kind,
            "target_id": target_id,
            "current_text": current_text,
            "current_output_prefix": current_prefix,
            "scored_attempts": safe_history,
            "requirements": requirements,
            "candidate_count": optimizer.breadth - 1,
            "round": round_number,
            "scope": scope,
            "fitting_corpus_hash": content_hash(
                [(case.id, case.solver_view_hash) for case in fitting_cases]
            ),
        }
        packet_dependencies = [judge_validation.packet_id]
        packet_dependencies.extend(item.packet_id for item in dependencies)
        return self._packet(
            run_id,
            lock,
            StageKind.OPTIMIZE,
            inputs=(self.ledger.put_json("optimizer_context", optimizer_context),),
            instructions={
                "task": "Propose the next bounded COPRO candidate set.",
                "hidden_material": [
                    "expected_response",
                    "expected_artifacts",
                    "rubric",
                    "judge_feedback",
                    "test",
                    "challenge",
                ],
            },
            dependencies=tuple(packet_dependencies),
            output_schema="copro-proposal-v1",
            work_spec={
                "strategy": "copro",
                "operation": "propose",
                "target": {"kind": target_kind, "id": target_id},
                "current_target_hash": content_hash(
                    {"text": current_text, "output_prefix": current_prefix}
                ),
                "history": safe_history,
                "requirements": requirements,
                "breadth": optimizer.breadth,
                "init_temperature": optimizer.init_temperature,
                "optimizer_program_hash": program_hash(create_copro_proposal_program()),
            },
            work_dependencies=(),
        )

    def _copro_candidate_bundles(
        self,
        bundle: Mapping[str, object],
        proposal: StagePacket,
    ) -> tuple[Mapping[str, object], ...]:
        optimizer = self.workspace.repository.workspace.experiments[
            proposal.experiment_id
        ].optimizer
        if not isinstance(optimizer, CoproConfiguration):
            raise IntegrityError("COPRO candidates require COPRO configuration")
        status = self.ledger.status(proposal.packet_id)
        if status.result is None:
            raise IntegrityError("COPRO proposal is not persisted")
        output = _mapping(self.ledger.read_output(status.result), "COPRO proposal output")
        target_kind = _string(output.get("target_kind"), "COPRO target kind")
        target_id = _string(output.get("target_id"), "COPRO target id")
        candidates = [self._copro_candidate(bundle)]
        for raw in _list(output.get("candidates"), "COPRO proposal candidates"):
            proposed = _mapping(raw, "COPRO proposed candidate")
            next_bundle = {
                "instruction": bundle["instruction"],
                "output_prefix": bundle["output_prefix"],
                "global_context": bundle["global_context"],
                "global_overlay": bundle.get("global_overlay", ""),
                "skills": dict(_mapping(bundle.get("skills", {}), "COPRO bundle skills")),
            }
            if target_kind == "global_instructions":
                overlay = _string(
                    proposed.get("instruction"), "proposed global instruction bundle"
                )
                next_bundle["global_overlay"] = overlay
                next_bundle["global_context"] = self._effective_global_context(
                    optimizer,
                    overlay,
                )
            else:
                next_bundle["skills"][target_id] = proposed["instruction"]
            candidate = self._copro_candidate(next_bundle)
            if candidate["id"] not in {item["id"] for item in candidates}:
                candidates.append(candidate)
        return tuple(candidates)

    def _retry_legacy_copro_exhaustion(self, proposal: StagePacket) -> StagePacket:
        """Make one new attempt for proposal failures made recoverable by normalization."""

        status = self.ledger.status(proposal.packet_id)
        if status.state is not StageState.FAILED or status.result is None:
            return proposal
        error = status.result.error
        if error is None or error.kind != "integrityerror":
            return proposal
        recoverable_messages = (
            "optimizer candidates must be distinct",
            "optimizer must not repeat the unchanged candidate",
            "optimizer must return exactly",
        )
        if not any(message in error.message for message in recoverable_messages):
            return proposal
        return self.ledger.retry(proposal.packet_id)

    def _copro_selection_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        *,
        proposal: StagePacket,
        judges: Sequence[StagePacket],
        candidates: Sequence[Mapping[str, object]],
        expected_case_ids: Sequence[str],
        target_kind: str,
        target_id: str,
        round_number: int,
        scope: str,
        current_candidate_id: str,
    ) -> StagePacket:
        proposal_status = self.ledger.status(proposal.packet_id)
        if proposal_status.result is None:
            raise IntegrityError("COPRO selection requires a persisted proposal")
        proposal_output = _mapping(
            self.ledger.read_output(proposal_status.result), "COPRO proposal output"
        )
        proposal_accounting = proposal_output.get("proposal_accounting")
        results = []
        for judge in sorted(judges, key=self._judge_packet_key):
            status = self.ledger.status(judge.packet_id)
            if status.result is None:
                raise IntegrityError("COPRO selection requires persisted judgments")
            output = _mapping(self.ledger.read_output(status.result), "COPRO judgment")
            results.append(
                {
                    "candidate_id": output["candidate_id"],
                    "case_id": output["case_id"],
                    "score": output["score"],
                }
            )
        selection = {
            "operation": "copro_round",
            "metric": "rubric_mean_score",
            "scope": scope,
            "round": round_number,
            "target_kind": target_kind,
            "target_id": target_id,
            "current_candidate_id": current_candidate_id,
            "expected_case_ids": list(expected_case_ids),
            "candidates": list(candidates),
            "results": results,
        }
        if proposal_accounting is not None:
            selection["proposal_accounting"] = dict(
                _mapping(proposal_accounting, "COPRO proposal accounting")
            )
        return self._packet(
            run_id,
            lock,
            StageKind.SELECT,
            inputs=(self.ledger.put_json("candidate_selection", selection),),
            instructions={
                "task": "Select the highest mean-scoring COPRO candidate deterministically.",
                "tie_break": "current candidate, shorter normalized target, candidate id",
            },
            dependencies=(
                judge_validation.packet_id,
                proposal.packet_id,
                *(item.packet_id for item in judges),
            ),
            output_schema="copro-selection-v1",
            work_spec={
                "operation": "copro_round",
                "metric": selection["metric"],
                "target_kind": target_kind,
                "target_id": target_id,
                "current_candidate_id": current_candidate_id,
                "expected_case_ids": selection["expected_case_ids"],
                "candidates": selection["candidates"],
                "results": selection["results"],
                **(
                    {"proposal_accounting": selection["proposal_accounting"]}
                    if "proposal_accounting" in selection
                    else {}
                ),
            },
            # Candidate scores and bundle contents are already normalized into
            # work_spec. Run-specific judge-result artifacts must not make an
            # otherwise identical deterministic selection a new work node.
            work_dependencies=(),
        )

    def _run_copro_search(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        session: _ExecutionSession,
        *,
        training_cases: Sequence[CaseDefinition],
        validation_cases: Sequence[CaseDefinition],
        scope: str,
        validation_assignment_hash: str,
        initial_bundle: Mapping[str, object],
    ) -> _CoproSearchResult | RunProgress:
        optimizer = lock.configuration.optimizer
        if not isinstance(optimizer, CoproConfiguration):
            raise IntegrityError("COPRO search requires COPRO configuration")
        bundle: Mapping[str, object] = initial_bundle
        dependencies: tuple[StagePacket, ...] = ()
        history_by_target: dict[str, list[Mapping[str, object]]] = {}
        for round_number in range(1, optimizer.depth + 1):
            targets = self._copro_targets(optimizer)
            target_batches = (targets,)
            for target_batch in target_batches:
                if not target_batch:
                    continue

                # Every target in this factorized search round starts from the
                # same frozen bundle. The workspace schema makes the batch
                # either one global target or independent skill targets.
                frozen_bundle = bundle
                target_inputs: list[
                    tuple[
                        str,
                        str,
                        tuple[CaseDefinition, ...],
                        tuple[CaseDefinition, ...],
                        StagePacket,
                    ]
                ] = []
                proposals: list[StagePacket] = []
                for target_kind, target_id in target_batch:
                    target_training_cases = (
                        tuple(training_cases)
                        if target_kind == "global_instructions"
                        else tuple(case for case in training_cases if case.skill == target_id)
                    )
                    if not target_training_cases:
                        continue
                    target_validation_cases = (
                        tuple(validation_cases)
                        if target_kind == "global_instructions"
                        else tuple(case for case in validation_cases if case.skill == target_id)
                    )
                    if not target_validation_cases:
                        raise PreflightError(
                            f"COPRO target {target_id!r} has no fixed validation cases"
                        )
                    proposal = self._copro_proposal_packet(
                        run_id,
                        lock,
                        judge_validation,
                        bundle=frozen_bundle,
                        target_kind=target_kind,
                        target_id=target_id,
                        round_number=round_number,
                        scope=scope,
                        fitting_cases=target_training_cases,
                        history=history_by_target.get(target_id, ()),
                        dependencies=dependencies,
                    )
                    proposal = self._retry_legacy_copro_exhaustion(proposal)
                    proposals.append(proposal)
                    target_inputs.append(
                        (
                            target_kind,
                            target_id,
                            target_training_cases,
                            target_validation_cases,
                            proposal,
                        )
                    )
                if not target_inputs:
                    continue

                pending = self._execute_all(tuple(proposals), lock, session)
                if pending is not None:
                    return self._blocked_progress(run_id, lock.configuration.id, pending)

                prepared: list[
                    tuple[
                        str,
                        str,
                        tuple[CaseDefinition, ...],
                        tuple[CaseDefinition, ...],
                        StagePacket,
                        tuple[Mapping[str, object], ...],
                        str,
                        tuple[StagePacket, ...],
                    ]
                ] = []
                all_fits: list[StagePacket] = []
                for (
                    target_kind,
                    target_id,
                    target_training_cases,
                    target_validation_cases,
                    proposal,
                ) in target_inputs:
                    candidates = self._copro_candidate_bundles(frozen_bundle, proposal)
                    current_id = _string(candidates[0].get("id"), "current candidate id")
                    fits = tuple(
                        self._fit_packet(
                            run_id,
                            lock,
                            judge_validation,
                            cases=target_training_cases,
                            candidate=candidate,
                            phase="copro_candidate",
                            validation_assignment_hash=validation_assignment_hash,
                            selection=proposal,
                        )
                        for candidate in candidates
                    )
                    all_fits.extend(fits)
                    prepared.append(
                        (
                            target_kind,
                            target_id,
                            target_training_cases,
                            target_validation_cases,
                            proposal,
                            candidates,
                            current_id,
                            fits,
                        )
                    )
                pending = self._execute_all(tuple(all_fits), lock, session)
                if pending is not None:
                    return self._blocked_progress(run_id, lock.configuration.id, pending)

                plans: list[_CoproTargetPlan] = []
                all_solves: list[StagePacket] = []
                for (
                    target_kind,
                    target_id,
                    target_training_cases,
                    target_validation_cases,
                    proposal,
                    candidates,
                    current_id,
                    fits,
                ) in prepared:
                    solves = tuple(
                        solve
                        for fit in fits
                        for solve in self._solve_packets(
                            run_id,
                            lock,
                            judge_validation,
                            selected_case_ids=[case.id for case in target_validation_cases],
                            program_dependency=fit,
                            evaluation_phase="validation",
                        )
                    )
                    all_solves.extend(solves)
                    plans.append(
                        _CoproTargetPlan(
                            target_kind=target_kind,
                            target_id=target_id,
                            training_cases=target_training_cases,
                            validation_cases=target_validation_cases,
                            proposal=proposal,
                            candidates=candidates,
                            current_candidate_id=current_id,
                            fits=fits,
                            solves=solves,
                        )
                    )

                all_judges, pending = self._execute_solve_judge_pipeline(
                    tuple(all_solves),
                    lock,
                    session,
                    judge_validation,
                )
                if pending is not None:
                    return self._blocked_progress(run_id, lock.configuration.id, pending)

                selections: list[StagePacket] = []
                judge_offset = 0
                for plan in plans:
                    target_judges = all_judges[judge_offset : judge_offset + len(plan.solves)]
                    judge_offset += len(plan.solves)
                    selections.append(
                        self._copro_selection_packet(
                            run_id,
                            lock,
                            judge_validation,
                            proposal=plan.proposal,
                            judges=target_judges,
                            candidates=plan.candidates,
                            expected_case_ids=[case.id for case in plan.validation_cases],
                            target_kind=plan.target_kind,
                            target_id=plan.target_id,
                            round_number=round_number,
                            scope=scope,
                            current_candidate_id=plan.current_candidate_id,
                        )
                    )
                pending = self._execute_all(tuple(selections), lock, session)
                if pending is not None:
                    return self._blocked_progress(run_id, lock.configuration.id, pending)

                selected_by_target: dict[str, Mapping[str, object]] = {}
                for plan, selection in zip(plans, selections, strict=True):
                    status = self.ledger.status(selection.packet_id)
                    assert status.result is not None
                    selected = _mapping(
                        self.ledger.read_output(status.result),
                        "COPRO selection",
                    )
                    selected_by_target[plan.target_id] = _mapping(
                        selected.get("selected_bundle"),
                        "selected COPRO bundle",
                    )
                    history_by_target.setdefault(plan.target_id, []).extend(
                        _mapping(item, "COPRO candidate summary")
                        for item in _list(
                            selected.get("candidates"),
                            "COPRO candidate summaries",
                        )
                    )

                if plans[0].target_kind == "global_instructions":
                    bundle = selected_by_target[plans[0].target_id]
                else:
                    merged_skills = dict(
                        _mapping(frozen_bundle.get("skills", {}), "COPRO bundle skills")
                    )
                    for plan in plans:
                        selected_skills = _mapping(
                            selected_by_target[plan.target_id].get("skills", {}),
                            "selected COPRO skills",
                        )
                        if plan.target_id in selected_skills:
                            merged_skills[plan.target_id] = selected_skills[plan.target_id]
                        else:
                            merged_skills.pop(plan.target_id, None)
                    bundle = {
                        "instruction": frozen_bundle["instruction"],
                        "output_prefix": frozen_bundle["output_prefix"],
                        "global_context": frozen_bundle["global_context"],
                        "global_overlay": frozen_bundle.get("global_overlay", ""),
                        "skills": merged_skills,
                    }
                dependencies = tuple(selections)

        if not dependencies:
            raise IntegrityError("COPRO search produced no selection")
        return _CoproSearchResult(bundle, dependencies)

    def _advance_copro(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        session: _ExecutionSession,
        *,
        through: str | None,
    ) -> StagePacket | RunProgress:
        training, validation, validation_assignment_hash = self._optimization_cases(lock)
        optimizer = lock.configuration.optimizer
        assert isinstance(optimizer, CoproConfiguration)
        search = self._run_copro_search(
            run_id,
            lock,
            judge_validation,
            session,
            training_cases=training,
            validation_cases=validation,
            scope="optimization",
            validation_assignment_hash=validation_assignment_hash,
            initial_bundle=self._base_copro_bundle(optimizer),
        )
        if isinstance(search, RunProgress):
            return search
        final_fit = self._fit_packet(
            run_id,
            lock,
            judge_validation,
            cases=training,
            candidate=self._copro_candidate(search.bundle),
            phase="final_fit",
            validation_assignment_hash=validation_assignment_hash,
            selection=search.dependencies[-1],
            upstream_dependencies=search.dependencies[:-1],
        )
        if not self._ensure_executed(final_fit, lock, session):
            return self._blocked_progress(run_id, lock.configuration.id, final_fit)
        if through in {StageKind.OPTIMIZE.value, StageKind.SELECT.value, StageKind.FIT.value}:
            return self._progress(run_id, lock.configuration.id, "paused", StageKind.FIT)
        return final_fit

    def _validation_assignment(self, lock: ExperimentLock) -> tuple[Mapping[str, str], str]:
        validation = lock.configuration.validation
        if validation is None:
            raise IntegrityError("optimization experiment has no validation configuration")
        train = self._cases_by_split()["train"]
        assignments = deterministic_group_validation_split(
            train,
            fraction=validation.fraction,
            seed=validation.seed,
        )
        mapping = {item.group: item.split for item in assignments}
        return mapping, content_hash(assignments)

    def _optimization_cases(
        self, lock: ExperimentLock
    ) -> tuple[tuple[CaseDefinition, ...], tuple[CaseDefinition, ...], str]:
        assignment, assignment_hash = self._validation_assignment(lock)
        permanent_train = self._cases_by_split()["train"]
        training = tuple(case for case in permanent_train if assignment[case.group] == "training")
        validation = tuple(
            case for case in permanent_train if assignment[case.group] == "validation"
        )
        return training, validation, assignment_hash

    def _fit_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        *,
        cases: Sequence[object],
        candidate: Mapping[str, object],
        phase: str,
        validation_assignment_hash: str,
        selection: StagePacket | None = None,
        upstream_dependencies: Sequence[StagePacket] = (),
    ) -> StagePacket:
        if isinstance(lock.configuration.optimizer, LabeledFewShotConfiguration):
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
        else:
            # COPRO fitting packages an already selected factorized instruction bundle.
            # Gold answers and rubrics are deliberately absent from this local
            # compilation packet; only the judge saw them while scoring.
            train = [
                {
                    "case_id": case.id,
                    "split": "train",
                    "skill": case.skill,
                    "solver_view_hash": case.solver_view_hash,
                }
                for case in cases
            ]
        dependencies = [judge_validation.packet_id]
        if selection is not None:
            dependencies.append(selection.packet_id)
        dependencies.extend(item.packet_id for item in upstream_dependencies)
        return self._packet(
            run_id,
            lock,
            StageKind.FIT,
            inputs=(
                self.ledger.put_json(
                    "training_set",
                    {
                        "cases": train,
                        "context": {
                            "phase": phase,
                            "validation_assignment_hash": validation_assignment_hash,
                        },
                    },
                ),
            ),
            instructions={
                "task": "Fit a state-only DSPy program using only the supplied training cases.",
                "candidate": dict(candidate),
                "forbidden_splits": ["test", "challenge"],
            },
            dependencies=tuple(dependencies),
            output_schema="fitted-program-v1",
            work_spec={
                "training_cases": [(case.id, case.content_hash) for case in cases],
                "candidate": dict(candidate),
                "materialize_final_artifacts": phase == "final_fit",
            },
            work_dependencies=(),
        )

    def _validation_fit_packets(
        self, run_id: str, lock: ExperimentLock, judge_validation: StagePacket
    ) -> tuple[StagePacket, ...]:
        training, _, assignment_hash = self._optimization_cases(lock)
        packets = []
        for candidate in self._candidate_configurations(lock):
            fit_cases = () if candidate["k"] == 0 else training
            packets.append(
                self._fit_packet(
                    run_id,
                    lock,
                    judge_validation,
                    cases=fit_cases,
                    candidate=candidate,
                    phase="validation",
                    validation_assignment_hash=assignment_hash,
                )
            )
        return tuple(packets)

    def _validation_solve_packets(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        fits: Sequence[StagePacket],
    ) -> tuple[StagePacket, ...]:
        _, validation, _ = self._optimization_cases(lock)
        packets = []
        for fit in fits:
            status = self.ledger.status(fit.packet_id)
            assert status.result is not None
            output = _mapping(self.ledger.read_output(status.result), "fitted program")
            candidate_id = _string(output.get("candidate_id"), "fitted program candidate_id")
            packets.extend(
                self._solve_packets(
                    run_id,
                    lock,
                    judge_validation,
                    selected_case_ids=tuple(case.id for case in validation),
                    program_dependency=fit,
                    evaluation_phase="validation",
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
        for judge in sorted(judges, key=self._judge_packet_key):
            status = self.ledger.status(judge.packet_id)
            assert status.result is not None
            output = _mapping(self.ledger.read_output(status.result), "validation judgment")
            results.append(
                {
                    "candidate_id": _string(output.get("candidate_id"), "candidate_id"),
                    "k": _integer(output.get("candidate_k"), "candidate_k"),
                    "case_id": _string(output.get("case_id"), "case_id"),
                    "score": _number(output.get("score"), "score"),
                }
            )
        _, validation, assignment_hash = self._optimization_cases(lock)
        selection_input = {
            "metric": "mean_score",
            "validation_assignment_hash": assignment_hash,
            "validation_case_ids": [case.id for case in validation],
            "results": results,
        }
        return self._packet(
            run_id,
            lock,
            StageKind.SELECT,
            inputs=(self.ledger.put_json("validation", selection_input),),
            instructions={
                "task": "Select the candidate with the highest mean validation score.",
                "tie_break": "prefer smaller k, then candidate id",
            },
            dependencies=(judge_validation.packet_id, *(item.packet_id for item in judges)),
            output_schema="validation-selection-v1",
            work_spec=selection_input,
            # The normalized validation results in work_spec are the complete
            # semantic input; judge output artifacts also contain reporting
            # coordinates and are attempt provenance only.
            work_dependencies=(),
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
        selected = _mapping(self.ledger.read_output(status.result), "validation selection")
        candidate = {
            "id": _string(selected.get("selected_candidate_id"), "selected candidate id"),
            "k": _integer(selected.get("selected_k"), "selected k"),
        }
        _, _, assignment_hash = self._optimization_cases(lock)
        return self._fit_packet(
            run_id,
            lock,
            judge_validation,
            cases=self._cases_by_split()["train"] if candidate["k"] else (),
            candidate=candidate,
            phase="final_fit",
            validation_assignment_hash=assignment_hash,
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
        candidate_id: str | None = None,
        gate_dependency: StagePacket | None = None,
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
        if gate_dependency is not None:
            if gate_dependency.stage is not StageKind.FIT:
                raise IntegrityError("solve gate dependency must be a fitted program")
            gate_status = self.ledger.status(gate_dependency.packet_id)
            if gate_status.state is not StageState.PERSISTED:
                raise IntegrityError("solve gate dependency is not persisted")
            dependencies.append(gate_dependency.packet_id)
        candidate_k = 0
        strategy = "baseline"
        skill_overlays: Mapping[str, str] = {}
        global_context = self.workspace.context.global_context
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
            solver_program_hash = _string(output.get("fitted_program_hash"), "fitted program hash")
            candidate_id = _string(output.get("candidate_id"), "candidate id")
            candidate_k = _integer(output.get("candidate_k"), "candidate k")
            strategy = _string(output.get("strategy"), "fit strategy")
            global_context = _string(
                output.get("global_context", self.workspace.context.global_context),
                "fit global context",
            )
            skill_overlays = {
                _string(skill_id, "skill overlay id"): _string(text, "skill overlay text")
                for skill_id, text in _mapping(
                    output.get("skill_overlays", {}), "fit skill overlays"
                ).items()
            }
            state_artifact = StageArtifact(
                "fitted_state",
                _string(state.get("content_id"), "compiled state content_id"),
                _string(state.get("media_type"), "compiled state media_type"),
            )
        packets = []
        reuse = self.workspace.repository.workspace.stages["solve"].reuse
        repetitions = (
            1
            if evaluation_phase not in {"test", "pilot"}
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
            skill_context = skill_overlays.get(case.skill, self.workspace.context.skill(case.skill))
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
                            "global_context": global_context,
                            "skill_context": skill_context,
                            "task": case.prompt,
                            "solver_attachments": dict(case.solver_attachments()),
                            "program_hash": solver_program_hash,
                            "evaluation_phase": evaluation_phase,
                            "candidate_id": candidate_id or "baseline",
                            "candidate_k": candidate_k,
                            "strategy": strategy,
                        },
                    )
                ]
                if state_artifact is not None:
                    inputs.append(state_artifact)
                solver_view = {
                    "case_solver_view_hash": case.solver_view_hash,
                    "global_context": global_context,
                    "effective_global_hash": content_hash(global_context),
                    "skill_context": skill_context,
                    "effective_skill_hash": content_hash(skill_context),
                    "task": case.prompt,
                    "solver_attachments": dict(case.solver_attachments()),
                    "program_hash": solver_program_hash,
                    "fitted_state": (None if state_artifact is None else state_artifact.content_id),
                    "runtime": content_hash(
                        self.workspace.repository.workspace.runtime(lock.configuration.id)
                    ),
                    **(
                        {"sample_slot": repetition}
                        if reuse.policy.value != "replicate" and lock.configuration.repetitions > 1
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
                "candidate_id": candidate["candidate_id"],
                "candidate_k": candidate["candidate_k"],
                "strategy": candidate["strategy"],
                "candidate_response": candidate["response"],
                "solver_work_id": solve.work_id,
                "solver_packet_id": solve.packet_id,
            }
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
                        "judge_program_hash": lock.judge_program_hash,
                        "repetitions": self._judge_repetitions(run_id),
                    },
                    work_dependencies=(response_hash,),
                )
            )
        return tuple(packets)

    def _aggregate_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        judge_validation: StagePacket,
        judges: Sequence[StagePacket],
        *,
        environment: Mapping[str, str],
    ) -> StagePacket:
        judges = tuple(sorted(judges, key=self._judge_packet_key))
        run = self.ledger.load_run(run_id)
        execution_mode = _execution_mode(run)
        judgment_outputs = []
        case_ids: set[str] = set()
        for judge in judges:
            status = self.ledger.status(judge.packet_id)
            assert status.result is not None
            judgment_outputs.append(status.result.outputs[0].content_id)
            output = _mapping(self.ledger.read_output(status.result), "judge output")
            if output.get("evaluation_phase") in {"test", "pilot"}:
                case_ids.add(_string(output.get("case_id"), "judge case id"))
        expected_response_tokens = self._expected_response_token_measurements(
            case_ids,
            lock=lock,
            environment=environment,
        )
        evaluation_index = {
            "judgment_results": judgment_outputs,
            "execution_mode": execution_mode,
            "benchmark_valid": execution_mode == "benchmark",
            "expected_response_tokens": expected_response_tokens,
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
            output_schema="run-summary-v4",
            work_spec={
                "evaluation_index": evaluation_index,
                "aggregation_implementation": "run-summary-v4",
            },
            work_dependencies=tuple(judgment_outputs),
        )

    def _expected_response_token_measurements(
        self,
        case_ids: set[str],
        *,
        lock: ExperimentLock,
        environment: Mapping[str, str],
    ) -> Mapping[str, object]:
        """Build hidden reporting metadata without exposing gold text to model stages."""

        cases = tuple(
            sorted(
                (case for case in self.workspace.case_bank.cases if case.id in case_ids),
                key=lambda case: case.id,
            )
        )
        if not cases:
            return {"schema_version": 1, "status": "empty", "cases": []}
        workspace = self.workspace.repository.workspace
        model = lock.models["solver"]
        executor = workspace.executors[model.executor_id]
        if executor.type is not ExecutorType.ENDPOINT:
            return {
                "schema_version": 1,
                "status": "unavailable",
                "reason": "solver_executor_has_no_exact_tokenizer",
                "cases": [],
            }
        measure_text = getattr(self.token_preflight, "measure_text", None)
        if not callable(measure_text):
            return {
                "schema_version": 1,
                "status": "unavailable",
                "reason": "exact_text_measurement_not_supported_by_preflight",
                "model": model.model,
                "deployment_id": model.deployment_id,
                "deployment_revision": model.deployment_revision,
                "cases": [],
            }
        selected_endpoint = None
        first_measurement = None
        failures: list[Mapping[str, object]] = []
        for endpoint in model.eligible_endpoints(executor):
            bound_model = model.bind_endpoint(endpoint)
            measurement = measure_text(
                endpoint,
                model=bound_model.model,
                deployment_revision=model.deployment_revision,
                content=cases[0].expected_response,
                api_key=endpoint.api_key(environment),
            )
            if measurement.is_exact:
                selected_endpoint = endpoint
                first_measurement = measurement
                break
            failures.append({"endpoint_id": endpoint.id, "reason": measurement.reason})
        if selected_endpoint is None or first_measurement is None:
            return {
                "schema_version": 1,
                "status": "unavailable",
                "reason": "no_exact_endpoint_tokenizer",
                "model": model.model,
                "deployment_revision": model.deployment_revision,
                "attempts": failures,
                "cases": [],
            }
        measurements = []
        for index, case in enumerate(cases):
            measurement = (
                first_measurement
                if index == 0
                else measure_text(
                    selected_endpoint,
                    model=model.bind_endpoint(selected_endpoint).model,
                    deployment_revision=model.deployment_revision,
                    content=case.expected_response,
                    api_key=selected_endpoint.api_key(environment),
                )
            )
            if not measurement.is_exact or measurement.token_count is None:
                return {
                    "schema_version": 1,
                    "status": "incomplete",
                    "reason": measurement.reason,
                    "model": model.model,
                    "deployment_revision": model.deployment_revision,
                    "endpoint_id": selected_endpoint.id,
                    "tokenizer_fingerprint": measurement.tokenizer_fingerprint,
                    "cases": measurements,
                }
            measurements.append(
                {
                    "case_id": case.id,
                    "expected_response_hash": measurement.content_hash,
                    "token_count": measurement.token_count,
                    "length_band": _expected_response_token_band(measurement.token_count),
                }
            )
        return {
            "schema_version": 1,
            "status": "complete",
            "measurement": "exact_raw_expected_response_tokens",
            "model": model.model,
            "deployment_id": model.deployment_id,
            "deployment_revision": model.deployment_revision,
            "executor_id": executor.id,
            "endpoint_id": selected_endpoint.id,
            "endpoint_configuration_hash": selected_endpoint.configuration_hash,
            "tokenizer_fingerprint": first_measurement.tokenizer_fingerprint,
            "add_special_tokens": False,
            "chat_template_applied": False,
            "bands": list(_EXPECTED_RESPONSE_TOKEN_BANDS),
            "cases": measurements,
        }

    def _judge_packet_key(self, packet: StagePacket) -> tuple[str, str, str, int]:
        value = _mapping(self._input(packet, "evaluation"), "judge input")
        return (
            _string(value.get("candidate_id", "baseline"), "judge candidate id"),
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
        role = (
            "judge"
            if stage in {StageKind.VALIDATE_JUDGE, StageKind.JUDGE}
            else (
                "solver"
                if stage is StageKind.SOLVE
                else ("optimizer" if stage is StageKind.OPTIMIZE else stage.value)
            )
        )
        model = lock.models.get(role) or resolve_stage_model(
            workspace,
            stage.value,
            experiment_id=lock.configuration.id,
            role=role,
        )
        executor_id = model.executor_id if model is not None else configuration.executor
        if executor_id is None:
            raise ConfigurationError(f"stage {stage.value!r} has no resolved executor")
        executor = workspace.executors[executor_id]
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
            executor=StageExecutorIdentity(executor.id, executor.adapter, executor.execution_hash),
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
            item
            for item in self.ledger.attempts_for_work(run_id, packet.work_id)
            if self._same_stage_work(item, packet)
        ]
        if matching_attempts:
            latest = max(matching_attempts, key=lambda item: item.attempt)
            latest_status = self.ledger.status(latest.packet_id)
            if latest_status.state is StageState.FAILED and self._retryable_endpoint_failure(
                latest_status.result
            ):
                return self.ledger.retry(latest.packet_id)
            return latest
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
                raise IntegrityError("judge validation packet exposes coordinator-only labels")
        if packet.stage is StageKind.OPTIMIZE:
            if {item.id for item in packet.inputs} != {"optimizer_context"}:
                raise IntegrityError("optimize packet must contain only optimizer_context")
            forbidden = {
                "expected_response",
                "expected_artifacts",
                "rubric",
                "judge_feedback",
                "test_score",
                "challenge_score",
            }
            if _contains_key(self._input(packet, "optimizer_context"), forbidden):
                raise IntegrityError("optimize packet crosses the hidden-material firewall")
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
        elif packet.output_schema == "copro-proposal-v1":
            if payload.get("strategy") != "copro" or payload.get("operation") != "propose":
                raise IntegrityError("COPRO proposal output has the wrong strategy or operation")
            _string(payload.get("target_kind"), "COPRO proposal target_kind")
            _string(payload.get("target_id"), "COPRO proposal target_id")
            _integer(payload.get("round"), "COPRO proposal round")
            _string(payload.get("proposal_hash"), "COPRO proposal hash")
            candidates = _list(payload.get("candidates"), "COPRO proposal candidates")
            context = _mapping(self._input(packet, "optimizer_context"), "optimizer context")
            expected_count = _integer(context.get("candidate_count"), "optimizer candidate_count")
            proposal_accounting = payload.get("proposal_accounting")
            if proposal_accounting is None:
                # Proposal results persisted before shortfall accounting was added
                # remain valid only when they satisfy the former exact-count contract.
                if len(candidates) != expected_count:
                    raise IntegrityError("COPRO proposal returned the wrong candidate count")
            else:
                validate_copro_proposal_accounting(
                    proposal_accounting,
                    expected_count=expected_count,
                    accepted_count=len(candidates),
                )
            if len(candidates) > expected_count:
                raise IntegrityError("COPRO proposal exceeds its candidate count")
            target_kind = _string(context.get("target_kind"), "optimizer target kind")
            if target_kind not in {"global_instructions", "skill"}:
                raise IntegrityError("COPRO proposal has an unsupported target kind")
            original = (
                _string(context.get("current_text"), "optimizer current text").strip(),
                "",
            )
            identities = set()
            for raw_candidate in candidates:
                candidate = _mapping(raw_candidate, "COPRO proposal candidate")
                if set(candidate) != {"instruction", "output_prefix"}:
                    raise IntegrityError(
                        "COPRO proposal candidates require only instruction and output_prefix"
                    )
                instruction = _string(candidate.get("instruction"), "candidate instruction")
                prefix = candidate.get("output_prefix")
                if not isinstance(prefix, str):
                    raise IntegrityError("candidate output_prefix must be a string")
                identity = (
                    instruction.strip(),
                    "",
                )
                if not identity[0]:
                    raise IntegrityError("COPRO proposal candidate instruction is empty")
                if prefix.strip():
                    raise IntegrityError(
                        "COPRO candidates cannot change the frozen output prefix"
                    )
                if identity == original or identity in identities:
                    raise IntegrityError("COPRO proposal candidates must be new and unique")
                identities.add(identity)
            if payload.get("proposal_hash") != content_hash(candidates):
                raise IntegrityError("COPRO proposal hash is invalid")
        elif packet.output_schema == "judge-validation-v1":
            if not isinstance(payload.get("ordering_consistent"), bool):
                raise IntegrityError("judge validation output requires boolean ordering_consistent")
            scores = _mapping(payload.get("fixture_scores"), "fixture_scores")
            judge_validation = _mapping(
                self._input(packet, "judge_validation"), "judge_validation input"
            )
            corpus_hash = _string(
                judge_validation.get("corpus_hash"), "judge_validation corpus_hash"
            )
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
                "strategy",
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
                "candidate_id",
                "candidate_k",
                "strategy",
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
                "candidate_id",
                "candidate_k",
                "strategy",
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
            strategy = _string(payload.get("strategy"), "fitted program strategy")
            demonstration_count = _integer(
                payload.get("demonstration_count"), "fitted program demonstration_count"
            )
            overlays = _mapping(payload.get("skill_overlays"), "fitted skill overlays")
            if strategy == "copro" and demonstration_count != 0:
                raise IntegrityError("COPRO fitted programs must have zero demonstrations")
            if strategy == "labeled_few_shot" and overlays:
                raise IntegrityError("labeled_few_shot programs cannot contain skill overlays")
            if strategy == "copro":
                search_space_type = _string(
                    payload.get("search_space_type"), "COPRO search space type"
                )
                frozen_global_hash = _string(
                    payload.get("frozen_global_context_hash"),
                    "frozen global context hash",
                )
                effective_global_hash = _string(
                    payload.get("effective_global_context_hash"),
                    "effective global context hash",
                )
                frozen_skill_bank_hash = _string(
                    payload.get("frozen_skill_bank_hash"),
                    "frozen skill-bank hash",
                )
                effective_skill_bank_hash = _string(
                    payload.get("effective_skill_bank_hash"),
                    "effective skill-bank hash",
                )
                if search_space_type == "skills":
                    if frozen_global_hash != effective_global_hash:
                        raise IntegrityError(
                            "skill optimization changed the global context hash"
                        )
                    if payload.get("global_instruction_overlay") != "":
                        raise IntegrityError(
                            "skill optimization changed global instructions"
                        )
                    if payload.get("global_instruction_artifact") is not None:
                        raise IntegrityError(
                            "skill optimization contains a global instruction artifact"
                        )
                elif search_space_type == "global_instructions":
                    if frozen_skill_bank_hash != effective_skill_bank_hash:
                        raise IntegrityError(
                            "global-instruction optimization changed the skill bank"
                        )
                    if overlays:
                        raise IntegrityError(
                            "global-instruction optimization contains skill overlays"
                        )
                    _string(
                        payload.get("global_instruction_overlay"),
                        "optimized global instruction bundle",
                    )
                    reference = _mapping(
                        payload.get("global_instruction_artifact"),
                        "global instruction artifact",
                    )
                    self.artifacts.reference(
                        _string(reference.get("content_id"), "global artifact content_id"),
                        _string(reference.get("media_type"), "global artifact media_type"),
                    )
                else:
                    raise IntegrityError("fitted program has an unsupported COPRO search space")
            if packet.work_spec.get("materialize_final_artifacts") is True:
                manifest = _mapping(
                    payload.get("compiled_manifest_artifact"), "compiled manifest artifact"
                )
                manifest_reference = ArtifactReference(
                    _string(manifest.get("content_id"), "compiled manifest content_id"),
                    _string(manifest.get("media_type"), "compiled manifest media_type"),
                    _integer(manifest.get("size_bytes"), "compiled manifest size_bytes"),
                    _string(manifest.get("relative_path"), "compiled manifest relative_path"),
                )
                if not self.artifacts.verify(manifest_reference):
                    raise IntegrityError("compiled manifest artifact failed verification")
                if strategy == "copro":
                    for field in (
                        "candidate_lineage_artifact",
                        "search_summary_artifact",
                    ):
                        reference = _mapping(payload.get(field), field)
                        artifact = self.artifacts.reference(
                            _string(reference.get("content_id"), f"{field} content_id"),
                            _string(reference.get("media_type"), f"{field} media_type"),
                        )
                        if artifact.media_type != "application/json":
                            raise IntegrityError(f"{field} must be JSON")
        elif packet.output_schema == "copro-selection-v1":
            if payload.get("strategy") != "copro" or payload.get("operation") != "copro_round":
                raise IntegrityError("COPRO selection output has the wrong strategy or operation")
            _string(payload.get("current_candidate_id"), "current candidate id")
            _string(payload.get("selected_candidate_id"), "selected candidate id")
            _number(payload.get("selected_mean_score"), "selected mean score")
            _mapping(payload.get("selected_bundle"), "selected COPRO bundle")
            _list(payload.get("candidates"), "COPRO candidate summaries")
            if payload.get("proposal_accounting") is not None:
                input_selection = _mapping(
                    self._input(packet, "candidate_selection"), "candidate selection input"
                )
                input_accounting = input_selection.get("proposal_accounting")
                if payload.get("proposal_accounting") != input_accounting:
                    raise IntegrityError(
                        "COPRO selection proposal_accounting does not match its packet"
                    )
            if payload.get("tie_break") != "current_then_shorter_then_content_hash":
                raise IntegrityError("COPRO selection output has the wrong tie-break policy")
        elif packet.output_schema == "validation-selection-v1":
            _string(payload.get("selected_candidate_id"), "selected candidate id")
            _integer(payload.get("selected_k"), "selected k")
            _number(payload.get("selected_mean_score"), "selected mean score")
            _list(payload.get("candidates"), "candidate summaries")
        elif packet.output_schema == "run-summary-v4":
            _list(payload.get("cases"), "run summary cases")
            _string(payload.get("run_graph_id"), "run summary graph id")
            if payload.get("run_id") != packet.run_id:
                raise IntegrityError("run summary is bound to another run")
        else:
            raise IntegrityError(f"unknown output schema: {packet.output_schema}")

    @staticmethod
    def _validate_result_budget(packet: StagePacket, result: StageResult) -> None:
        for key, usage_key, limit in (
            ("model_calls", "completed_calls", packet.budget.model_calls),
            ("tokens", "tokens", packet.budget.tokens),
        ):
            value = result.usage.get(usage_key)
            if value is None and usage_key == "completed_calls":
                value = result.usage.get("model_calls")
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

    def _budget_ledger(
        self,
        stage: StageKind,
        *,
        concurrency: int = 1,
        model_call_multiplier: int = 1,
    ) -> BudgetLedger:
        budget: RoleBudget = self.workspace.repository.workspace.stages[stage.value].budget
        return BudgetLedger(
            BudgetLimits(
                model_calls=budget.model_calls * model_call_multiplier,
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

    def _persisted_fit_packets(self, run_id: str, *, phase: str) -> tuple[StagePacket, ...]:
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
        lifecycle_state = {
            "paused": RunLifecycleState.PAUSED,
            "awaiting_execution": RunLifecycleState.AWAITING_HARNESS,
            "execution_failed": RunLifecycleState.FAILED,
            "judge_validation_failed": RunLifecycleState.FAILED,
            "report_failed": RunLifecycleState.FAILED,
            "completed": RunLifecycleState.COMPLETED,
            "pilot_completed": RunLifecycleState.COMPLETED,
        }.get(status)
        if lifecycle_state is not None:
            self.lifecycle.append(
                run_id,
                lifecycle_state,
                reason=status,
                details={
                    "stage": None if stage is None else stage.value,
                    **(
                        {"error_kind": result.get("error_kind")}
                        if isinstance(result, Mapping) and result.get("error_kind") is not None
                        else {}
                    ),
                },
                if_changed=True,
            )
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
                    item.result is not None and item.result.provenance.get("cache_hit") is True
                ),
                "source_result_id": (
                    None if item.result is None else item.result.provenance.get("source_result_id")
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
        if lifecycle_state is not None:
            terminal_recording = lifecycle_state in {
                RunLifecycleState.COMPLETED,
                RunLifecycleState.FAILED,
                RunLifecycleState.CANCELLED,
            }
            self._recording_checkpoint(
                run_id,
                force=True,
                flush=terminal_recording,
                stage=None if stage is None else stage.value,
                boundary_id=status,
            )
        return progress

    def _recording_checkpoint(
        self,
        run_id: str,
        *,
        force: bool = False,
        flush: bool = False,
        stage: str | None = None,
        boundary_id: str | None = None,
    ) -> Mapping[str, object] | None:
        repository = (
            self.workspace
            if isinstance(self.workspace, WorkspaceRepository)
            else self.workspace.repository
        )
        recording = repository.workspace.recording
        if not recording.enabled:
            return None
        if flush:
            queued = self.recorder_service.enqueue_finalization(run_id)
            status = self.recorder_service.status(run_id)
            self._emit(
                "recording_status",
                run_id,
                _string(
                    self.ledger.load_run(run_id).get("experiment_id"),
                    "run experiment_id",
                ),
                details={**dict(status), "enqueue": dict(queued)},
            )
            return status
        if force:
            return self.recorder_service.enqueue_stage_boundary(
                run_id,
                stage=stage or "run",
                boundary_id=boundary_id or "forced-checkpoint",
            )
        return self.recorder_service.request_sync(run_id)

    def _validate_judge_validation_split(
        self, lock: ExperimentLock, *, pilot: bool = False
    ) -> None:
        if lock.configuration.mode is not ExperimentMode.OPTIMIZE and not pilot:
            return
        cases = {case.id: case for case in self.workspace.case_bank.cases}
        held_out = sorted(
            fixture.id
            for fixture in self.workspace.judge_validation.fixtures
            if self.workspace.splits.split_for(cases[fixture.case_id]) in {"test", "challenge"}
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


def _upgrade_frozen_workspace_payload(
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Upgrade an immutable historical run for the current parser.

    This is deliberately not used by ``WorkspaceRepository.from_file``:
    user-authored legacy workspaces remain rejected. It exists only so a run
    frozen by an earlier framework version can honor its immutable contract.
    """

    version = payload.get("schema_version")
    if version == WORKSPACE_SCHEMA_VERSION:
        return json.loads(canonical_json_bytes(payload))
    if WORKSPACE_SCHEMA_VERSION != 17 or version not in {13, 14, 15, 16}:
        raise IntegrityError(
            f"frozen workspace schema_version {version!r} cannot be rehydrated by "
            f"schema {WORKSPACE_SCHEMA_VERSION}"
        )

    upgraded = json.loads(canonical_json_bytes(payload))
    if version == 16:
        return _upgrade_frozen_copro_search_spaces(upgraded)
    if version == 15:
        return _upgrade_frozen_copro_search_spaces(
            _upgrade_frozen_recording_payload(upgraded)
        )
    if version == 14:
        experiments = _mapping(upgraded.get("experiments"), "frozen workspace experiments")
        upgraded_experiments: dict[str, object] = {}
        for experiment_id, raw_experiment in experiments.items():
            experiment = dict(_mapping(raw_experiment, f"frozen experiment {experiment_id}"))
            cross_validation = experiment.pop("cross_validation", None)
            if experiment.get("mode") == "optimize":
                legacy_validation = _mapping(
                    cross_validation,
                    f"frozen experiment {experiment_id} cross_validation",
                )
                optimizer = dict(
                    _mapping(
                        experiment.get("optimizer"),
                        f"frozen experiment {experiment_id} optimizer",
                    )
                )
                experiment["optimizer"] = optimizer
                experiment["validation"] = {
                    "strategy": "holdout",
                    "fraction": 0.2,
                    "seed": legacy_validation.get("seed", 0),
                }
            upgraded_experiments[experiment_id] = experiment
        upgraded["experiments"] = upgraded_experiments
        return _upgrade_frozen_copro_search_spaces(
            _upgrade_frozen_recording_payload(upgraded)
        )

    stages = _mapping(upgraded.get("stages"), "frozen workspace stages")
    models = _mapping(upgraded.get("models"), "frozen workspace models")
    experiments = _mapping(upgraded.get("experiments"), "frozen workspace experiments")
    executors = _mapping(upgraded.get("executors"), "frozen workspace executors")

    model_executors: dict[str, str] = {}
    for stage_id, raw_stage in stages.items():
        stage = _mapping(raw_stage, f"frozen stage {stage_id}")
        model_id = stage.get("model")
        if model_id is None:
            continue
        model_id = _string(model_id, f"frozen stage {stage_id} model")
        executor_id = _string(stage.get("executor"), f"frozen stage {stage_id} executor")
        previous = model_executors.setdefault(model_id, executor_id)
        if previous != executor_id:
            raise IntegrityError(f"frozen model {model_id!r} was bound to multiple executors")

    deployments: dict[str, object] = {}
    upgraded_models: dict[str, object] = {}
    deployment_by_model: dict[str, str] = {}
    for model_id, raw_model in models.items():
        model = _mapping(raw_model, f"frozen model {model_id}")
        executor_id = model_executors.get(model_id)
        if executor_id is None:
            raise IntegrityError(f"frozen model {model_id!r} has no stage executor binding")
        deployment_id = f"frozen-{model_id}"
        deployment_by_model[model_id] = deployment_id
        deployments[deployment_id] = {
            "executor": executor_id,
            "model": _string(model.get("name"), f"frozen model {model_id} name"),
            "revision": _string(
                model.get("deployment_revision"),
                f"frozen model {model_id} deployment_revision",
            ),
        }
        upgraded_model = {
            "deployment": deployment_id,
            "parameters": dict(
                _mapping(model.get("parameters", {}), f"frozen model {model_id} parameters")
            ),
        }
        for field in ("reasoning_effort", "configured_cost_per_call"):
            if model.get(field) is not None:
                upgraded_model[field] = model[field]
        upgraded_models[model_id] = upgraded_model

    upgraded_executors: dict[str, object] = {}
    for executor_id, raw_executor in executors.items():
        executor = dict(_mapping(raw_executor, f"frozen executor {executor_id}"))
        raw_endpoints = executor.get("endpoints")
        if raw_endpoints is not None:
            routed_deployments = [
                (deployment_by_model[model_id], models[model_id])
                for model_id, bound_executor in model_executors.items()
                if bound_executor == executor_id
            ]
            endpoints = []
            for index, raw_endpoint in enumerate(
                _list(raw_endpoints, f"frozen executor {executor_id} endpoints")
            ):
                endpoint = dict(
                    _mapping(raw_endpoint, f"frozen executor {executor_id} endpoint {index}")
                )
                endpoint["routes"] = [
                    {
                        "deployment": deployment_id,
                        "model": _string(
                            _mapping(raw_model, "frozen routed model").get("name"),
                            "frozen routed model name",
                        ),
                    }
                    for deployment_id, raw_model in sorted(routed_deployments)
                ]
                endpoints.append(endpoint)
            executor["endpoints"] = endpoints
        upgraded_executors[executor_id] = executor

    upgraded_stages: dict[str, object] = {}
    experiment_model_stages = {"validate_judge", "optimize", "solve", "judge"}
    for stage_id, raw_stage in stages.items():
        stage = dict(_mapping(raw_stage, f"frozen stage {stage_id}"))
        if stage_id in experiment_model_stages:
            stage.pop("executor", None)
            stage.pop("model", None)
        upgraded_stages[stage_id] = stage

    solver_profile = _string(
        _mapping(stages.get("solve"), "frozen solve stage").get("model"),
        "frozen solve model",
    )
    judge_profile = _string(
        _mapping(stages.get("judge"), "frozen judge stage").get("model"),
        "frozen judge model",
    )
    optimizer_profile = _string(
        _mapping(stages.get("optimize"), "frozen optimize stage").get("model"),
        "frozen optimize model",
    )
    upgraded_experiments: dict[str, object] = {}
    for experiment_id, raw_experiment in experiments.items():
        experiment = dict(_mapping(raw_experiment, f"frozen experiment {experiment_id}"))
        roles: dict[str, object] = {
            "solver": solver_profile,
            "judge": judge_profile,
        }
        optimizer = experiment.get("optimizer")
        if isinstance(optimizer, Mapping) and optimizer.get("strategy") == "copro":
            roles["optimizer"] = optimizer_profile
        if experiment.get("mode") == "optimize":
            cross_validation = _mapping(
                experiment.pop("cross_validation"),
                f"frozen experiment {experiment_id} cross_validation",
            )
            experiment["validation"] = {
                "strategy": "holdout",
                "fraction": 0.2,
                "seed": cross_validation.get("seed", 0),
            }
        experiment["roles"] = roles
        upgraded_experiments[experiment_id] = experiment

    upgraded["schema_version"] = WORKSPACE_SCHEMA_VERSION
    upgraded["executors"] = upgraded_executors
    upgraded["deployments"] = deployments
    upgraded["models"] = upgraded_models
    upgraded["stages"] = upgraded_stages
    upgraded["experiments"] = upgraded_experiments
    upgraded.setdefault("comparisons", {})
    return _upgrade_frozen_copro_search_spaces(
        _upgrade_frozen_recording_payload(upgraded)
    )


def _upgrade_frozen_copro_search_spaces(upgraded: dict[str, object]) -> dict[str, object]:
    """Project retired COPRO targets into the explicit frozen search-space union.

    This runs only for immutable historical definitions. Active workspace files
    never pass through this adapter and must use schema 17 directly.
    """

    experiments = _mapping(upgraded.get("experiments"), "frozen workspace experiments")
    projected: dict[str, object] = {}
    for experiment_id, raw_experiment in experiments.items():
        experiment = dict(_mapping(raw_experiment, f"frozen experiment {experiment_id}"))
        raw_optimizer = experiment.get("optimizer")
        if not isinstance(raw_optimizer, Mapping) or raw_optimizer.get("strategy") != "copro":
            projected[experiment_id] = experiment
            continue
        optimizer = dict(raw_optimizer)
        raw_targets = optimizer.pop("targets", None)
        if raw_targets is not None:
            targets = _mapping(raw_targets, f"frozen experiment {experiment_id} targets")
            raw_skills = targets.get("skills")
            has_skills = raw_skills == "all" or (
                isinstance(raw_skills, Sequence)
                and not isinstance(raw_skills, (str, bytes, bytearray))
                and bool(raw_skills)
            )
            if has_skills:
                optimizer["search_space"] = {
                    "type": "skills",
                    "include": raw_skills,
                }
            elif targets.get("prompt") is True:
                optimizer["search_space"] = {
                    "type": "global_instructions",
                    "files": "all",
                }
            else:
                raise IntegrityError(
                    f"frozen COPRO experiment {experiment_id!r} has no usable target"
                )
        experiment["optimizer"] = optimizer
        projected[experiment_id] = experiment
    upgraded["experiments"] = projected
    upgraded["schema_version"] = WORKSPACE_SCHEMA_VERSION
    return upgraded


def _upgrade_frozen_recording_payload(upgraded: dict[str, object]) -> dict[str, object]:
    """Add the queue-backed recorder contract to a historical frozen workspace."""

    raw_recording = upgraded.get("recording")
    if isinstance(raw_recording, Mapping):
        recording = dict(raw_recording)
        recording.pop("mode", None)
        recording.update(
            {
                "queue": "local-recorder-queue",
                "wait_on_run_exit": False,
                "projection": {
                    "live": {"enabled": True, "minimum_interval_seconds": 15},
                    "stage_boundary": {"tables": "incremental", "summaries": True},
                    "finalization": {"reports": True, "plotly": True, "evidence": True},
                    "traces": {
                        "optimization": {
                            "policy": "winners_failures_and_sample",
                            "sample_rate": 0.05,
                        },
                        "validation": {"policy": "full"},
                        "test": {"policy": "full"},
                    },
                },
            }
        )
        upgraded["recording"] = recording
    upgraded["recorder_queues"] = {
        "local-recorder-queue": {
            "type": "filesystem",
            "path": "operations/recorder-queue",
            "live_coalescing": True,
            "lease_seconds": 300,
            "max_attempts": 12,
        }
    }
    upgraded["schema_version"] = WORKSPACE_SCHEMA_VERSION
    return upgraded


def _rehydrate_historical_experiment_lock(
    stored: Mapping[str, object],
    generated: ExperimentLock,
    *,
    expected_revision: str,
    judge_repetitions: int,
) -> ExperimentLock:
    """Bind current runtime routing to a verified schema-7 experiment identity."""

    if stored.get("schema_version") != 7:
        raise IntegrityError(
            "frozen experiment lock does not reproduce its revision and has no supported "
            "historical identity reader"
        )
    if stored.get("content_hash") != expected_revision:
        raise IntegrityError("frozen experiment lock content hash differs from its run")

    stable_fields = (
        "id",
        "workspace_id",
        "mode",
        "source_snapshot_hash",
        "source_commit",
        "case_bank_hash",
        "split_hash",
        "judge_validation_hash",
        "authoring_instructions_hash",
        "capability_catalog_hash",
        "solver_program_hash",
        "judge_program_hash",
        "cases",
    )
    for field in stable_fields:
        if stored.get(field) != json_value(getattr(generated, field)):
            raise IntegrityError(f"historical experiment lock changed frozen field {field!r}")

    stored_configuration = dict(
        _mapping(stored.get("configuration"), "historical experiment configuration")
    )
    generated_configuration = dict(json_value(generated.configuration))
    generated_configuration.pop("roles", None)
    if stored_configuration != generated_configuration:
        raise IntegrityError("historical experiment configuration changed during rehydration")

    raw_models = _mapping(stored.get("models"), "historical experiment models")
    if set(raw_models) != set(generated.models):
        raise IntegrityError("historical experiment model roles changed during rehydration")
    models: dict[str, ResolvedRoleModel] = {}
    for role, current in generated.models.items():
        raw = _mapping(raw_models[role], f"historical {role} model")
        expected = {
            "role": current.role,
            "executor_id": current.executor_id,
            "provider": current.provider,
            "model": current.model,
            "deployment_revision": current.deployment_revision,
            "base_url": current.base_url,
            "api_key_env": current.api_key_env,
            "reasoning_effort": current.reasoning_effort,
            "parameters": dict(current.parameters),
            "configured_cost_per_call": current.configured_cost_per_call,
        }
        for field, value in expected.items():
            if raw.get(field) != value:
                raise IntegrityError(f"historical {role} model changed frozen field {field!r}")
        models[role] = ResolvedRoleModel(
            role=current.role,
            profile_id=current.profile_id,
            deployment_id=current.deployment_id,
            executor_id=current.executor_id,
            provider=current.provider,
            model=current.model,
            deployment_revision=current.deployment_revision,
            base_url=current.base_url,
            api_key_env=current.api_key_env,
            reasoning_effort=current.reasoning_effort,
            parameters=current.parameters,
            configured_cost_per_call=current.configured_cost_per_call,
            content_hash=_string(raw.get("content_hash"), f"historical {role} model hash"),
        )
    frozen_models = MappingProxyType(models)
    comparison_group_id = content_hash(
        {
            "schema_version": 1,
            "benchmark_revision_id": generated.benchmark_revision_id,
            "judge_model": frozen_models["judge"].content_hash,
            "judge_program_hash": generated.judge_program_hash,
            "judge_repetitions": judge_repetitions,
        }
    )
    arm_id = content_hash(
        {
            "schema_version": 1,
            "comparison_group_id": comparison_group_id,
            "roles": generated.configuration.roles,
            "models": frozen_models,
            "mode": generated.configuration.mode.value,
            "optimizer": generated.configuration.optimizer,
            "solver_repetitions": generated.configuration.repetitions,
        }
    )
    return replace(
        generated,
        schema_version=7,
        models=frozen_models,
        comparison_group_id=comparison_group_id,
        arm_id=arm_id,
        content_hash=expected_revision,
    )


def _expected_response_token_band(token_count: int) -> str:
    lower = 1
    for upper in _EXPECTED_RESPONSE_TOKEN_BANDS:
        if token_count <= upper:
            return f"{lower:03d}-{upper}"
        lower = upper + 1
    return f"{lower}+"


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
        exhausted = next(
            (
                int(call.parameters["max_tokens"])
                for call in result.calls
                if isinstance(call.parameters.get("max_tokens"), int)
                and not isinstance(call.parameters.get("max_tokens"), bool)
                and call.usage.get("completion_tokens")
                == call.parameters.get("max_tokens")
            ),
            None,
        )
        summary = (
            f"{role} response reached max_tokens={exhausted} before completing "
            "valid structured output"
            if exhausted is not None
            else f"{role} returned invalid structured output{detail}"
        )
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


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise IntegrityError(f"{field} must be a string")
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
