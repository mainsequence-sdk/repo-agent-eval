from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from .errors import ConfigurationError, IntegrityError, RecordingError, ResolutionError
from .hashing import canonical_json_bytes, content_hash, json_value
from .lifecycle import RunLifecycleState
from .models import ArtifactReference
from .recorder_queue import (
    ClaimedProjectionIntent,
    FilesystemRecorderQueue,
    ProjectionIntent,
    ProjectionIntentKind,
)
from .storage import FilesystemArtifactStore
from .workspace import (
    RecorderConfiguration,
    RecorderQueueConfiguration,
    RecordingConfiguration,
)


RECORDING_PLAN_SCHEMA_VERSION = 1


class RecordingStatus(str, Enum):
    DISABLED = "disabled"
    QUEUED = "queued"
    PROJECTING = "projecting"
    PUBLISHED = "published"
    FAILED = "failed"


@dataclass(frozen=True)
class RecorderCapabilities:
    nested_runs: bool = False
    metrics: bool = False
    tables: bool = False
    artifacts: bool = False
    traces: bool = False
    trace_links: bool = False


@dataclass(frozen=True)
class ExperimentDescriptor:
    name: str
    tags: Mapping[str, object]


@dataclass(frozen=True)
class RunDescriptor:
    local_run_id: str
    name: str
    status: str
    start_time_ms: int
    end_time_ms: int | None
    tags: Mapping[str, object]
    parameters: Mapping[str, object]


@dataclass(frozen=True)
class ScopeDescriptor:
    id: str
    name: str
    parent_id: str | None
    tags: Mapping[str, object]


@dataclass(frozen=True)
class MetricPoint:
    id: str
    key: str
    value: float
    step: int
    timestamp_ms: int
    scope_id: str | None = None


@dataclass(frozen=True)
class TableArtifact:
    id: str
    name: str
    rows: tuple[Mapping[str, object], ...]
    scope_id: str | None = None


@dataclass(frozen=True)
class ArtifactDescriptor:
    id: str
    path: str
    reference: ArtifactReference
    scope_id: str | None = None


@dataclass(frozen=True)
class SpanDescriptor:
    id: str
    name: str
    type: str
    inputs: object
    outputs: object
    attributes: Mapping[str, object]


@dataclass(frozen=True)
class TraceDescriptor:
    id: str
    name: str
    scope_id: str
    status: str
    inputs: object
    outputs: object
    attributes: Mapping[str, object]
    spans: tuple[SpanDescriptor, ...]
    links: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class RecordingPlan:
    schema_version: int
    plan_id: str
    recorder_config_hash: str
    source_sequence: int
    experiment: ExperimentDescriptor
    run: RunDescriptor
    scopes: tuple[ScopeDescriptor, ...]
    metrics: tuple[MetricPoint, ...]
    tables: tuple[TableArtifact, ...]
    artifacts: tuple[ArtifactDescriptor, ...]
    traces: tuple[TraceDescriptor, ...]
    omissions: tuple[Mapping[str, object], ...]

    @classmethod
    def create(
        cls,
        *,
        recorder_config_hash: str,
        source_sequence: int,
        experiment: ExperimentDescriptor,
        run: RunDescriptor,
        scopes: Sequence[ScopeDescriptor] = (),
        metrics: Sequence[MetricPoint] = (),
        tables: Sequence[TableArtifact] = (),
        artifacts: Sequence[ArtifactDescriptor] = (),
        traces: Sequence[TraceDescriptor] = (),
        omissions: Sequence[Mapping[str, object]] = (),
    ) -> RecordingPlan:
        identity = {
            "schema_version": RECORDING_PLAN_SCHEMA_VERSION,
            "recorder_config_hash": recorder_config_hash,
            "source_sequence": source_sequence,
            "experiment": experiment,
            "run": run,
            "scopes": tuple(scopes),
            "metrics": tuple(metrics),
            "tables": tuple(tables),
            "artifacts": tuple(artifacts),
            "traces": tuple(traces),
            "omissions": tuple(dict(item) for item in omissions),
        }
        return cls(
            schema_version=RECORDING_PLAN_SCHEMA_VERSION,
            plan_id=content_hash(identity),
            recorder_config_hash=recorder_config_hash,
            source_sequence=source_sequence,
            experiment=experiment,
            run=run,
            scopes=tuple(scopes),
            metrics=tuple(metrics),
            tables=tuple(tables),
            artifacts=tuple(artifacts),
            traces=tuple(traces),
            omissions=tuple(dict(item) for item in omissions),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> RecordingPlan:
        if payload.get("schema_version") != RECORDING_PLAN_SCHEMA_VERSION:
            raise IntegrityError(
                f"recording plan schema_version must be {RECORDING_PLAN_SCHEMA_VERSION}"
            )
        experiment_payload = _mapping(payload.get("experiment"), "recording experiment")
        run_payload = _mapping(payload.get("run"), "recording run")
        experiment = ExperimentDescriptor(
            _string(experiment_payload.get("name"), "recording experiment name"),
            _mapping(experiment_payload.get("tags", {}), "recording experiment tags"),
        )
        run = RunDescriptor(
            _string(run_payload.get("local_run_id"), "recording local_run_id"),
            _string(run_payload.get("name"), "recording run name"),
            _string(run_payload.get("status"), "recording run status"),
            _integer(run_payload.get("start_time_ms"), "recording start_time_ms"),
            (
                None
                if run_payload.get("end_time_ms") is None
                else _integer(run_payload.get("end_time_ms"), "recording end_time_ms")
            ),
            _mapping(run_payload.get("tags", {}), "recording run tags"),
            _mapping(run_payload.get("parameters", {}), "recording run parameters"),
        )
        scopes = tuple(
            ScopeDescriptor(
                _string(item.get("id"), "recording scope id"),
                _string(item.get("name"), "recording scope name"),
                (
                    None
                    if item.get("parent_id") is None
                    else _string(item.get("parent_id"), "recording scope parent_id")
                ),
                _mapping(item.get("tags", {}), "recording scope tags"),
            )
            for item in _mapping_sequence(payload.get("scopes", []), "recording scopes")
        )
        metrics = tuple(
            MetricPoint(
                _string(item.get("id"), "recording metric id"),
                _string(item.get("key"), "recording metric key"),
                _number(item.get("value"), "recording metric value"),
                _integer(item.get("step"), "recording metric step"),
                _integer(item.get("timestamp_ms"), "recording metric timestamp_ms"),
                (
                    None
                    if item.get("scope_id") is None
                    else _string(item.get("scope_id"), "recording metric scope_id")
                ),
            )
            for item in _mapping_sequence(payload.get("metrics", []), "recording metrics")
        )
        tables = tuple(
            TableArtifact(
                _string(item.get("id"), "recording table id"),
                _string(item.get("name"), "recording table name"),
                tuple(
                    _mapping(row, "recording table row")
                    for row in _sequence(item.get("rows", []), "recording table rows")
                ),
                (
                    None
                    if item.get("scope_id") is None
                    else _string(item.get("scope_id"), "recording table scope_id")
                ),
            )
            for item in _mapping_sequence(payload.get("tables", []), "recording tables")
        )
        artifacts = tuple(
            ArtifactDescriptor(
                _string(item.get("id"), "recording artifact id"),
                _safe_path(item.get("path"), "recording artifact path"),
                _artifact_reference(item.get("reference")),
                (
                    None
                    if item.get("scope_id") is None
                    else _string(item.get("scope_id"), "recording artifact scope_id")
                ),
            )
            for item in _mapping_sequence(payload.get("artifacts", []), "recording artifacts")
        )
        traces = tuple(
            _trace_from_mapping(item)
            for item in _mapping_sequence(payload.get("traces", []), "recording traces")
        )
        omissions = tuple(
            _mapping(item, "recording omission")
            for item in _sequence(payload.get("omissions", []), "recording omissions")
        )
        plan = cls.create(
            recorder_config_hash=_string(
                payload.get("recorder_config_hash"), "recording recorder_config_hash"
            ),
            source_sequence=_integer(payload.get("source_sequence"), "recording source_sequence"),
            experiment=experiment,
            run=run,
            scopes=scopes,
            metrics=metrics,
            tables=tables,
            artifacts=artifacts,
            traces=traces,
            omissions=omissions,
        )
        if payload.get("plan_id") != plan.plan_id:
            raise IntegrityError("recording plan identity hash is invalid")
        return plan

    def as_dict(self) -> Mapping[str, object]:
        value = json_value(self)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True)
class ProjectionBatch:
    schema_version: int
    batch_id: str
    plan_id: str
    source_sequence: int
    recorder_config_hash: str
    from_sequence_exclusive: int
    through_sequence_inclusive: int
    phase: str
    experiment: ExperimentDescriptor
    run: RunDescriptor
    scopes: tuple[ScopeDescriptor, ...]
    metrics: tuple[MetricPoint, ...]
    tables: tuple[TableArtifact, ...]
    artifacts: tuple[ArtifactDescriptor, ...]
    traces: tuple[TraceDescriptor, ...]
    omissions: tuple[Mapping[str, object], ...]

    @classmethod
    def from_plan(
        cls,
        plan: RecordingPlan,
        *,
        from_sequence_exclusive: int,
        through_sequence_inclusive: int,
        phase: str,
        delivered_items: set[str],
    ) -> ProjectionBatch:
        metrics = tuple(item for item in plan.metrics if item.id not in delivered_items)
        tables = tuple(item for item in plan.tables if item.id not in delivered_items)
        artifacts = tuple(item for item in plan.artifacts if item.id not in delivered_items)
        traces = tuple(item for item in plan.traces if item.id not in delivered_items)
        identity = {
            "schema_version": 1,
            "plan_id": plan.plan_id,
            "from_sequence_exclusive": from_sequence_exclusive,
            "through_sequence_inclusive": through_sequence_inclusive,
            "phase": phase,
            "metrics": tuple(item.id for item in metrics),
            "tables": tuple(item.id for item in tables),
            "artifacts": tuple(item.id for item in artifacts),
            "traces": tuple(item.id for item in traces),
        }
        return cls(
            1,
            content_hash(identity),
            plan.plan_id,
            plan.source_sequence,
            plan.recorder_config_hash,
            from_sequence_exclusive,
            through_sequence_inclusive,
            phase,
            plan.experiment,
            plan.run,
            plan.scopes,
            metrics,
            tables,
            artifacts,
            traces,
            plan.omissions,
        )


@dataclass
class ExternalBinding:
    schema_version: int
    recorder_id: str
    recorder_config_hash: str
    local_run_id: str
    external_experiment_id: str | None = None
    external_run_id: str | None = None
    scopes: dict[str, str] = field(default_factory=dict)
    traces: dict[str, str] = field(default_factory=dict)
    trace_spans: dict[str, str] = field(default_factory=dict)
    delivered_items: set[str] = field(default_factory=set)

    @classmethod
    def create(
        cls,
        *,
        recorder_id: str,
        recorder_config_hash: str,
        local_run_id: str,
    ) -> ExternalBinding:
        return cls(1, recorder_id, recorder_config_hash, local_run_id)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ExternalBinding:
        if payload.get("schema_version") != 1:
            raise IntegrityError("external recorder binding schema_version must be 1")
        return cls(
            schema_version=1,
            recorder_id=_string(payload.get("recorder_id"), "binding recorder_id"),
            recorder_config_hash=_string(
                payload.get("recorder_config_hash"), "binding recorder_config_hash"
            ),
            local_run_id=_string(payload.get("local_run_id"), "binding local_run_id"),
            external_experiment_id=_optional_string(payload.get("external_experiment_id")),
            external_run_id=_optional_string(payload.get("external_run_id")),
            scopes={
                str(key): _string(value, "binding scope id")
                for key, value in _mapping(payload.get("scopes", {}), "binding scopes").items()
            },
            traces={
                str(key): _string(value, "binding trace id")
                for key, value in _mapping(payload.get("traces", {}), "binding traces").items()
            },
            trace_spans={
                str(key): _string(value, "binding trace root span id")
                for key, value in _mapping(
                    payload.get("trace_spans", {}), "binding trace_spans"
                ).items()
            },
            delivered_items={
                _string(value, "binding delivered item")
                for value in _sequence(
                    payload.get("delivered_items", []), "binding delivered_items"
                )
            },
        )

    def as_dict(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "recorder_id": self.recorder_id,
            "recorder_config_hash": self.recorder_config_hash,
            "local_run_id": self.local_run_id,
            "external_experiment_id": self.external_experiment_id,
            "external_run_id": self.external_run_id,
            "scopes": dict(sorted(self.scopes.items())),
            "traces": dict(sorted(self.traces.items())),
            "trace_spans": dict(sorted(self.trace_spans.items())),
            "delivered_items": sorted(self.delivered_items),
        }


class ExperimentRecorder(Protocol):
    @property
    def capabilities(self) -> RecorderCapabilities: ...

    def healthcheck(self, *, write_test: bool = False) -> Mapping[str, object]: ...

    def ensure_experiment(
        self,
        descriptor: ExperimentDescriptor,
        binding: ExternalBinding,
    ) -> str: ...

    def ensure_run(
        self,
        descriptor: RunDescriptor,
        binding: ExternalBinding,
    ) -> str: ...

    def apply(self, batch: ProjectionBatch, binding: ExternalBinding) -> Mapping[str, object]: ...

    def finalize(
        self,
        binding: ExternalBinding,
        status: RunLifecycleState,
        end_time_ms: int,
    ) -> Mapping[str, object]: ...


RecorderFactory = Callable[[RecorderConfiguration, Mapping[str, str]], ExperimentRecorder]


class RecorderWorkerService:
    """Queue and project recorder intents from managed or standalone consumers."""

    def __init__(
        self,
        store: FilesystemArtifactStore,
        recording: RecordingConfiguration,
        configuration: RecorderConfiguration,
        queue_configuration: RecorderQueueConfiguration,
        environment: Mapping[str, str],
        *,
        adapter_factory: RecorderFactory | None = None,
    ) -> None:
        self.store = store
        self.recording = recording
        self.configuration = configuration
        self.queue_configuration = queue_configuration
        self.environment = environment
        self.adapter_factory = adapter_factory or _default_recorder_factory
        self.root = (
            store.operations_root
            / "recorders"
            / configuration.id
            / configuration.configuration_hash.removeprefix("sha256:")
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.queue = FilesystemRecorderQueue(
            store.data_root / queue_configuration.path / queue_configuration.id,
            live_coalescing=queue_configuration.live_coalescing,
            lease_seconds=queue_configuration.lease_seconds,
            max_attempts=queue_configuration.max_attempts,
        )

    def request_sync(self, run_id: str) -> Mapping[str, object] | None:
        """Enqueue a coalescible live intent without building a recording plan."""

        if not self.recording.enabled:
            return None
        if not self.recording.projection.live.enabled:
            return None
        return self.enqueue(
            run_id,
            kind=ProjectionIntentKind.LIVE_PROGRESS,
            minimum_interval_seconds=self.recording.projection.live.minimum_interval_seconds,
        )

    def enqueue_stage_boundary(
        self, run_id: str, stage: str, boundary_id: str
    ) -> Mapping[str, object]:
        return self.enqueue(
            run_id,
            kind=ProjectionIntentKind.STAGE_BOUNDARY,
            stage=stage,
            boundary_id=boundary_id,
        )

    def enqueue_finalization(self, run_id: str) -> Mapping[str, object]:
        return self.enqueue(run_id, kind=ProjectionIntentKind.FINALIZATION)

    def enqueue(
        self,
        run_id: str,
        *,
        kind: ProjectionIntentKind,
        stage: str | None = None,
        boundary_id: str | None = None,
        minimum_interval_seconds: int = 0,
    ) -> Mapping[str, object]:
        from .lifecycle import RunLifecycleLedger
        from .stages import StageLedger

        if not self.recording.enabled:
            return self.status(run_id)
        ledger = StageLedger(self.store)
        run = ledger.load_run(run_id)
        target_sequence = ledger.latest_transition_sequence(run_id) + RunLifecycleLedger(
            self.store
        ).latest_sequence(run_id)
        priority = {
            ProjectionIntentKind.LIVE_PROGRESS: 10,
            ProjectionIntentKind.STAGE_BOUNDARY: 50,
            ProjectionIntentKind.BACKFILL: 60,
            ProjectionIntentKind.RECONCILE: 80,
            ProjectionIntentKind.FINALIZATION: 100,
        }[kind]
        intent = ProjectionIntent.create(
            recorder_id=self.configuration.id,
            recorder_config_hash=self.configuration.configuration_hash,
            workspace_id=_string(run.get("workspace_id"), "run workspace_id"),
            run_id=run_id,
            experiment_revision_id=_string(
                run.get("experiment_revision_id"), "run experiment_revision_id"
            ),
            kind=kind,
            target_sequence=target_sequence,
            priority=priority,
            stage=stage,
            boundary_id=boundary_id,
        )
        return {
            **dict(
                self.queue.enqueue(
                    intent,
                    minimum_interval_seconds=minimum_interval_seconds,
                )
            ),
            "kind": kind.value,
            "recorder": self.configuration.id,
        }

    def project_run(
        self,
        run_id: str,
        *,
        worker_id: str = "one-shot-projector",
        raise_on_error: bool = True,
    ) -> Mapping[str, object]:
        from .lifecycle import RunLifecycleLedger, RunLifecycleState

        current = RunLifecycleLedger(self.store).current(run_id)
        kind = (
            ProjectionIntentKind.FINALIZATION
            if current is not None
            and current.state
            in {
                RunLifecycleState.COMPLETED,
                RunLifecycleState.FAILED,
                RunLifecycleState.CANCELLED,
            }
            else ProjectionIntentKind.BACKFILL
        )
        self.enqueue(run_id, kind=kind)
        while True:
            result = self.work_once(
                worker_id=worker_id,
                run_id=run_id,
                raise_on_error=raise_on_error,
            )
            if result is None:
                break
        return self.status(run_id)

    def work_once(
        self,
        *,
        worker_id: str,
        run_id: str | None = None,
        raise_on_error: bool = False,
    ) -> Mapping[str, object] | None:
        claim = self.queue.claim(worker_id=worker_id, run_id=run_id)
        if claim is None:
            return None
        try:
            receipt = self._project_claim(claim)
            self.queue.acknowledge(claim, receipt)
            return receipt
        except Exception as error:
            message = self._redacted_error(error)
            state = self.queue.retry(
                claim,
                error_kind=type(error).__name__.lower(),
                message=message,
                delay_seconds=min(60.0, float(2 ** min(claim.attempts - 1, 6))),
            )
            if raise_on_error:
                if isinstance(error, RecordingError):
                    raise
                raise RecordingError(
                    f"recorder {self.configuration.id!r} projection failed: {error}"
                ) from error
            return {
                "status": state,
                "run_id": claim.intent.run_id,
                "intent_id": claim.intent.intent_id,
                "error_kind": type(error).__name__.lower(),
                "message": message,
            }

    def work_until_idle(
        self,
        *,
        worker_id: str,
        run_id: str | None = None,
        maximum_items: int | None = None,
        raise_on_error: bool = False,
    ) -> tuple[Mapping[str, object], ...]:
        results = []
        while maximum_items is None or len(results) < maximum_items:
            result = self.work_once(
                worker_id=worker_id,
                run_id=run_id,
                raise_on_error=raise_on_error,
            )
            if result is None:
                break
            results.append(result)
        return tuple(results)

    def reconcile_queue(self) -> Mapping[str, object]:
        """Repair evidence committed before its projection intent was published."""

        from .lifecycle import RunLifecycleLedger, RunLifecycleState
        from .reporting import list_run_ids
        from .stages import StageLedger

        if not self.recording.enabled:
            return {"status": "disabled", "enqueued": 0}
        ledger = StageLedger(self.store)
        lifecycle = RunLifecycleLedger(self.store)
        enqueued = []
        for run_id in list_run_ids(ledger):
            high_water = ledger.latest_transition_sequence(run_id) + lifecycle.latest_sequence(
                run_id
            )
            cursor = self.queue.cursor(self.configuration.configuration_hash, run_id)
            queue_status = self.queue.status(
                recorder_config_hash=self.configuration.configuration_hash,
                run_id=run_id,
            )
            current = lifecycle.current(run_id)
            terminal = current is not None and current.state in {
                RunLifecycleState.COMPLETED,
                RunLifecycleState.FAILED,
                RunLifecycleState.CANCELLED,
            }
            final_missing = terminal and (
                cursor.finalized_through is None or cursor.finalized_through < high_water
            )
            delta_missing = cursor.projected_through < high_water
            if not (final_missing or delta_missing):
                continue
            if int(queue_status["pending"]) or int(queue_status["leased"]):
                continue
            kind = (
                ProjectionIntentKind.FINALIZATION
                if terminal
                else ProjectionIntentKind.RECONCILE
            )
            enqueued.append(self.enqueue(run_id, kind=kind))
        return {"status": "reconciled", "enqueued": len(enqueued), "intents": enqueued}

    def _project_claim(self, claim: ClaimedProjectionIntent) -> Mapping[str, object]:
        from .lifecycle import RunLifecycleState
        from .recording_projection import RecorderProjector

        intent = claim.intent
        cursor = self.queue.cursor(intent.recorder_config_hash, intent.run_id)
        finalized = (
            intent.kind is ProjectionIntentKind.FINALIZATION
            and cursor.finalized_through is not None
            and cursor.finalized_through >= intent.target_sequence
        )
        if cursor.projected_through >= intent.target_sequence and (
            intent.kind is not ProjectionIntentKind.FINALIZATION or finalized
        ):
            return {
                "schema_version": 1,
                "status": "already_projected",
                "intent_id": intent.intent_id,
                "run_id": intent.run_id,
                "projected_through": cursor.projected_through,
            }

        projector = RecorderProjector(
            self.store,
            recorder=self.configuration,
            recording=self.recording,
        )
        plan = projector.build(intent.run_id, phase=intent.kind.value)
        binding = self._load_binding(intent.run_id)
        assert binding is not None
        adapter = self.adapter_factory(self.configuration, self.environment)
        bind_store = getattr(adapter, "bind_artifact_store", None)
        if callable(bind_store):
            bind_store(self.store)
        binding.external_experiment_id = adapter.ensure_experiment(plan.experiment, binding)
        self._write_binding(intent.run_id, binding)
        binding.external_run_id = adapter.ensure_run(plan.run, binding)
        self._write_binding(intent.run_id, binding)
        batch = ProjectionBatch.from_plan(
            plan,
            from_sequence_exclusive=cursor.projected_through,
            through_sequence_inclusive=intent.target_sequence,
            phase=intent.kind.value,
            delivered_items=binding.delivered_items,
        )
        details = dict(adapter.apply(batch, binding))
        if intent.kind is ProjectionIntentKind.FINALIZATION:
            terminal_state = RunLifecycleState(plan.run.status)
            if terminal_state not in {
                RunLifecycleState.COMPLETED,
                RunLifecycleState.FAILED,
                RunLifecycleState.CANCELLED,
            }:
                raise RecordingError("finalization intent references a non-terminal run")
            details["finalization"] = dict(
                adapter.finalize(
                    binding,
                    terminal_state,
                    plan.run.end_time_ms or plan.run.start_time_ms,
                )
            )
            comparison_plan = projector.build_comparison(intent.run_id)
            if comparison_plan is not None:
                details["model_comparison"] = self._sync_comparison(adapter, comparison_plan)
        self._write_binding(intent.run_id, binding)
        advanced = cursor.advance(intent)
        self.queue.write_cursor(advanced)
        return {
            "schema_version": 1,
            "status": "projected",
            "intent_id": intent.intent_id,
            "kind": intent.kind.value,
            "run_id": intent.run_id,
            "plan_id": plan.plan_id,
            "batch_id": batch.batch_id,
            "from_sequence_exclusive": cursor.projected_through,
            "through_sequence_inclusive": advanced.projected_through,
            "external_experiment_id": binding.external_experiment_id,
            "external_run_id": binding.external_run_id,
            "details": details,
        }

    def doctor(self, *, write_test: bool = True) -> Mapping[str, object]:
        try:
            adapter = self.adapter_factory(self.configuration, self.environment)
            details = adapter.healthcheck(write_test=write_test)
        except Exception as error:
            if isinstance(error, RecordingError):
                raise
            raise RecordingError(
                f"recorder {self.configuration.id!r} healthcheck failed: {error}"
            ) from error
        return {
            "schema_version": 1,
            "status": "ready",
            "recorder": self.configuration.id,
            "type": self.configuration.type,
            "tracking_uri": self.configuration.tracking_uri,
            "configuration_hash": self.configuration.configuration_hash,
            "capabilities": json_value(adapter.capabilities),
            "details": dict(details),
        }

    def status(self, run_id: str) -> Mapping[str, object]:
        if not self.recording.enabled:
            return {
                "schema_version": 1,
                "run_id": run_id,
                "recorder": self.configuration.id,
                "recorder_config_hash": self.configuration.configuration_hash,
                "status": RecordingStatus.DISABLED.value,
                "queue": {
                    "pending": 0,
                    "leased": 0,
                    "acknowledged": 0,
                    "dead_letter": 0,
                },
            }
        from .lifecycle import RunLifecycleLedger
        from .stages import StageLedger

        ledger = StageLedger(self.store)
        ledger.load_run(run_id)
        high_water = ledger.latest_transition_sequence(run_id) + RunLifecycleLedger(
            self.store
        ).latest_sequence(run_id)
        queue_status = self.queue.status(
            recorder_config_hash=self.configuration.configuration_hash,
            run_id=run_id,
        )
        cursor = self.queue.cursor(self.configuration.configuration_hash, run_id)
        binding = self._load_binding(run_id, required=False)
        if int(queue_status["dead_letter"]) > 0:
            state = RecordingStatus.FAILED
        elif int(queue_status["leased"]) > 0:
            state = RecordingStatus.PROJECTING
        elif int(queue_status["pending"]) > 0 or cursor.projected_through < high_water:
            state = RecordingStatus.QUEUED
        elif cursor.finalized_through is not None:
            state = RecordingStatus.PUBLISHED
        else:
            state = RecordingStatus.QUEUED
        return {
            "schema_version": 1,
            "run_id": run_id,
            "recorder": self.configuration.id,
            "recorder_config_hash": self.configuration.configuration_hash,
            "status": state.value,
            "canonical_high_water": high_water,
            "projected_through": cursor.projected_through,
            "finalized_through": cursor.finalized_through,
            "projection_lag": max(0, high_water - cursor.projected_through),
            "queue": dict(queue_status),
            "external_experiment_id": (None if binding is None else binding.external_experiment_id),
            "external_run_id": None if binding is None else binding.external_run_id,
        }

    def _redacted_error(self, error: Exception) -> str:
        message = str(error)
        for name, value in self.environment.items():
            if (
                any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
                and value
            ):
                message = message.replace(value, "[REDACTED]")
        return message

    def _run_root(self, run_id: str) -> Path:
        return self.root / "runs" / _safe_component(run_id, "run id")

    def _sync_comparison(
        self,
        adapter: ExperimentRecorder,
        plan: RecordingPlan,
    ) -> Mapping[str, object]:
        """Publish one immutable derived comparison run idempotently."""

        comparison_id = plan.run.local_run_id
        root = self.root / "comparisons" / _safe_component(comparison_id, "comparison id")
        event_path = root / "events" / f"{plan.plan_id.removeprefix('sha256:')}.json"
        receipt_path = root / "receipts" / event_path.name
        root.mkdir(parents=True, exist_ok=True)
        self._write_event(event_path, plan)
        if receipt_path.is_file():
            receipt = _load_mapping(receipt_path, "comparison recorder receipt")
            return {
                "status": "synced",
                "local_run_id": comparison_id,
                "external_run_id": receipt.get("external_run_id"),
                "plan_id": plan.plan_id,
            }
        binding_path = root / "binding.json"
        if binding_path.is_file():
            binding = ExternalBinding.from_mapping(
                _load_mapping(binding_path, "comparison recorder binding")
            )
            if (
                binding.recorder_id != self.configuration.id
                or binding.recorder_config_hash != self.configuration.configuration_hash
                or binding.local_run_id != comparison_id
            ):
                raise IntegrityError("comparison recorder binding does not match its location")
        else:
            binding = ExternalBinding.create(
                recorder_id=self.configuration.id,
                recorder_config_hash=self.configuration.configuration_hash,
                local_run_id=comparison_id,
            )
        binding.external_experiment_id = adapter.ensure_experiment(plan.experiment, binding)
        _atomic_json(binding_path, binding.as_dict())
        binding.external_run_id = adapter.ensure_run(plan.run, binding)
        _atomic_json(binding_path, binding.as_dict())
        batch = ProjectionBatch.from_plan(
            plan,
            from_sequence_exclusive=0,
            through_sequence_inclusive=plan.source_sequence,
            phase=ProjectionIntentKind.FINALIZATION.value,
            delivered_items=binding.delivered_items,
        )
        details = adapter.apply(batch, binding)
        terminal = adapter.finalize(
            binding,
            RunLifecycleState.COMPLETED,
            plan.run.end_time_ms or plan.run.start_time_ms,
        )
        _atomic_json(binding_path, binding.as_dict())
        receipt = {
            "schema_version": 1,
            "event_id": plan.plan_id,
            "run_id": comparison_id,
            "recorder_id": self.configuration.id,
            "recorder_config_hash": self.configuration.configuration_hash,
            "status": "synced",
            "recorded_at": _timestamp(),
            "external_experiment_id": binding.external_experiment_id,
            "external_run_id": binding.external_run_id,
            "details": {**dict(details), "finalization": dict(terminal)},
        }
        _atomic_json(receipt_path, receipt, replace=False)
        return {
            "status": "synced",
            "local_run_id": comparison_id,
            "external_run_id": binding.external_run_id,
            "plan_id": plan.plan_id,
        }

    def _load_binding(
        self,
        run_id: str,
        *,
        required: bool = True,
    ) -> ExternalBinding | None:
        path = self._run_root(run_id) / "binding.json"
        if not path.is_file():
            if not required:
                return None
            return ExternalBinding.create(
                recorder_id=self.configuration.id,
                recorder_config_hash=self.configuration.configuration_hash,
                local_run_id=run_id,
            )
        binding = ExternalBinding.from_mapping(_load_mapping(path, "recorder binding"))
        if (
            binding.recorder_id != self.configuration.id
            or binding.recorder_config_hash != self.configuration.configuration_hash
            or binding.local_run_id != run_id
        ):
            raise IntegrityError("recorder binding does not match its storage location")
        return binding

    def _write_binding(self, run_id: str, binding: ExternalBinding) -> None:
        _atomic_json(self._run_root(run_id) / "binding.json", binding.as_dict())

    @staticmethod
    def _write_event(path: Path, plan: RecordingPlan) -> None:
        payload = {
            "schema_version": 1,
            "event_id": plan.plan_id,
            "enqueued_at": _timestamp(),
            "plan": plan.as_dict(),
        }
        if path.exists():
            existing = _load_mapping(path, "recorder event")
            if existing.get("event_id") != plan.plan_id or existing.get("plan") != plan.as_dict():
                raise IntegrityError("recorder event id already contains different data")
            return
        _atomic_json(path, payload, replace=False)


def recorder_worker_service(
    store: FilesystemArtifactStore,
    *,
    recording: RecordingConfiguration,
    recorders: Mapping[str, RecorderConfiguration],
    recorder_queues: Mapping[str, RecorderQueueConfiguration],
    environment: Mapping[str, str],
    adapter_factory: RecorderFactory | None = None,
) -> RecorderWorkerService:
    try:
        configuration = recorders[recording.recorder]
    except KeyError as error:
        raise ConfigurationError(
            f"recording references unknown recorder {recording.recorder!r}"
        ) from error
    try:
        queue_configuration = recorder_queues[recording.queue]
    except KeyError as error:
        raise ConfigurationError(
            f"recording references unknown recorder queue {recording.queue!r}"
        ) from error
    return RecorderWorkerService(
        store,
        recording,
        configuration,
        queue_configuration,
        environment,
        adapter_factory=adapter_factory,
    )


def _default_recorder_factory(
    configuration: RecorderConfiguration,
    environment: Mapping[str, str],
) -> ExperimentRecorder:
    if configuration.type == "mlflow":
        from ms_agent_eval.adapters.mlflow import MlflowRecorder

        return MlflowRecorder(configuration, environment)
    raise ConfigurationError(f"unsupported recorder type: {configuration.type}")


def _trace_from_mapping(payload: Mapping[str, object]) -> TraceDescriptor:
    return TraceDescriptor(
        id=_string(payload.get("id"), "recording trace id"),
        name=_string(payload.get("name"), "recording trace name"),
        scope_id=_string(payload.get("scope_id"), "recording trace scope_id"),
        status=_string(payload.get("status"), "recording trace status"),
        inputs=payload.get("inputs"),
        outputs=payload.get("outputs"),
        attributes=_mapping(payload.get("attributes", {}), "recording trace attributes"),
        spans=tuple(
            SpanDescriptor(
                id=_string(item.get("id"), "recording span id"),
                name=_string(item.get("name"), "recording span name"),
                type=_string(item.get("type"), "recording span type"),
                inputs=item.get("inputs"),
                outputs=item.get("outputs"),
                attributes=_mapping(item.get("attributes", {}), "recording span attributes"),
            )
            for item in _mapping_sequence(payload.get("spans", []), "recording spans")
        ),
        links=tuple(
            _mapping(item, "recording trace link")
            for item in _sequence(payload.get("links", []), "recording trace links")
        ),
    )


def _artifact_reference(value: object) -> ArtifactReference:
    payload = _mapping(value, "artifact reference")
    size = payload.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise IntegrityError("artifact reference size_bytes must be non-negative")
    return ArtifactReference(
        _string(payload.get("content_id"), "artifact content_id"),
        _string(payload.get("media_type"), "artifact media_type"),
        size,
        _string(payload.get("relative_path"), "artifact relative_path"),
    )


def _atomic_json(
    path: Path,
    value: Mapping[str, object],
    *,
    replace: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(value)
    if path.exists() and not replace:
        if path.read_bytes() != payload:
            raise IntegrityError(f"immutable recorder file already contains other data: {path}")
        return
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_mapping(path: Path, label: str) -> Mapping[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ResolutionError(f"{label} does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IntegrityError(f"{label} is invalid: {path}") from error
    if not isinstance(payload, Mapping):
        raise IntegrityError(f"{label} must contain a JSON object")
    return {str(key): value for key, value in payload.items()}


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IntegrityError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise IntegrityError(f"{field} must be a list")
    return value


def _mapping_sequence(value: object, field: str) -> tuple[Mapping[str, object], ...]:
    return tuple(_mapping(item, f"{field}[]") for item in _sequence(value, field))


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntegrityError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value, "optional string")


def _integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise IntegrityError(f"{field} must be an integer")
    return value


def _number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise IntegrityError(f"{field} must be a number")
    return float(value)


def _safe_component(value: object, field: str) -> str:
    text = _string(value, field)
    if text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text:
        raise ConfigurationError(f"{field} must be a safe path component")
    return text


def _safe_path(value: object, field: str) -> str:
    from pathlib import PurePosixPath

    text = _string(value, field)
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "\x00" in text:
        raise IntegrityError(f"{field} must be a safe relative path")
    return path.as_posix()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
