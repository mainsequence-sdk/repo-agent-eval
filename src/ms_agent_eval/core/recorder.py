from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Protocol

from .errors import ConfigurationError, IntegrityError, RecordingError, ResolutionError
from .hashing import canonical_json_bytes, content_hash, json_value
from .lifecycle import RunLifecycleState
from .models import ArtifactReference
from .storage import FilesystemArtifactStore
from .workspace import (
    RecorderConfiguration,
    RecordingConfiguration,
)


RECORDING_PLAN_SCHEMA_VERSION = 1


class RecordingStatus(str, Enum):
    DISABLED = "disabled"
    PENDING = "pending"
    SYNCING = "syncing"
    SYNCED = "synced"
    PARTIALLY_SYNCED = "partially_synced"
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

    def sync(self, plan: RecordingPlan, binding: ExternalBinding) -> Mapping[str, object]: ...

    def finalize(
        self,
        binding: ExternalBinding,
        status: RunLifecycleState,
        end_time_ms: int,
    ) -> Mapping[str, object]: ...


RecorderFactory = Callable[[RecorderConfiguration, Mapping[str, str]], ExperimentRecorder]


class RecorderService:
    """Project canonical evidence through one retryable recorder destination."""

    def __init__(
        self,
        store: FilesystemArtifactStore,
        recording: RecordingConfiguration,
        configuration: RecorderConfiguration,
        environment: Mapping[str, str],
        *,
        adapter_factory: RecorderFactory | None = None,
    ) -> None:
        self.store = store
        self.recording = recording
        self.configuration = configuration
        self.environment = environment
        self.adapter_factory = adapter_factory or _default_recorder_factory
        self.root = (
            store.operations_root
            / "recorders"
            / configuration.id
            / configuration.configuration_hash.removeprefix("sha256:")
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self._executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="ms-agent-eval-recorder")
            if recording.enabled
            else None
        )
        self._state_lock = Lock()
        self._dirty: set[str] = set()
        self._futures: dict[str, Future[Mapping[str, object]]] = {}

    def configure_environment(self, environment: Mapping[str, str]) -> None:
        """Set process-local recorder credentials before scheduling delivery."""

        with self._state_lock:
            if any(not future.done() for future in self._futures.values()):
                raise RecordingError("recorder environment cannot change while delivery is active")
            self.environment = environment

    def request_sync(self, run_id: str) -> None:
        """Coalesce live checkpoints onto one bounded recorder worker."""

        if not self.recording.enabled or self._executor is None:
            return
        with self._state_lock:
            self._dirty.add(run_id)
            future = self._futures.get(run_id)
            if future is None or future.done():
                self._futures[run_id] = self._executor.submit(self._drain, run_id)

    def flush(self, run_id: str) -> Mapping[str, object]:
        if not self.recording.enabled:
            return self.status(run_id)
        self.request_sync(run_id)
        while True:
            with self._state_lock:
                future = self._futures.get(run_id)
            if future is None:
                return self.status(run_id)
            future.result()
            with self._state_lock:
                if run_id not in self._dirty:
                    return self.status(run_id)

    def _drain(self, run_id: str) -> Mapping[str, object]:
        latest: Mapping[str, object] = {}
        while True:
            with self._state_lock:
                if run_id not in self._dirty:
                    self._futures.pop(run_id, None)
                    return latest
                self._dirty.remove(run_id)
            latest = self.sync_run(run_id)

    def sync_run(
        self,
        run_id: str,
        *,
        raise_on_error: bool = False,
    ) -> Mapping[str, object]:
        from .recording_projection import RecorderProjector

        run_root = self._run_root(run_id)
        run_root.mkdir(parents=True, exist_ok=True)
        lock_path = run_root / "sync.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            binding: ExternalBinding | None = None
            adapter: ExperimentRecorder | None = None
            event_id = content_hash(
                {
                    "run_id": run_id,
                    "recorder_config_hash": self.configuration.configuration_hash,
                    "operation": "project",
                }
            )
            try:
                try:
                    projector = RecorderProjector(
                        self.store,
                        recorder=self.configuration,
                        recording=self.recording,
                    )
                    plan = projector.build(run_id)
                    event_id = plan.plan_id
                    event_path = (
                        run_root / "events" / f"{plan.plan_id.removeprefix('sha256:')}.json"
                    )
                    receipt_path = run_root / "receipts" / event_path.name
                    self._write_event(event_path, plan)
                    if receipt_path.is_file():
                        return self.status(run_id)

                    binding = self._load_binding(run_id)
                    assert binding is not None
                    adapter = self.adapter_factory(self.configuration, self.environment)
                    bind_store = getattr(adapter, "bind_artifact_store", None)
                    if callable(bind_store):
                        bind_store(self.store)
                    binding.external_experiment_id = adapter.ensure_experiment(
                        plan.experiment, binding
                    )
                    self._write_binding(run_id, binding)
                    binding.external_run_id = adapter.ensure_run(plan.run, binding)
                    self._write_binding(run_id, binding)
                    terminal_state = (
                        RunLifecycleState(plan.run.status)
                        if plan.run.status
                        in {
                            RunLifecycleState.COMPLETED.value,
                            RunLifecycleState.FAILED.value,
                            RunLifecycleState.CANCELLED.value,
                        }
                        else None
                    )
                    try:
                        details = adapter.sync(plan, binding)
                    except Exception:
                        # Terminal lifecycle state is canonical and must reach the
                        # external recorder even if a table, trace, or artifact
                        # fails later in the same projection attempt.
                        if terminal_state is not None:
                            try:
                                adapter.finalize(
                                    binding,
                                    terminal_state,
                                    plan.run.end_time_ms or plan.run.start_time_ms,
                                )
                            except Exception:
                                pass
                        raise
                    if terminal_state is not None:
                        terminal = adapter.finalize(
                            binding,
                            terminal_state,
                            plan.run.end_time_ms or plan.run.start_time_ms,
                        )
                        details = {**dict(details), "finalization": dict(terminal)}
                    comparison_plan = projector.build_comparison(run_id)
                    if comparison_plan is not None:
                        details = {
                            **dict(details),
                            "model_comparison": self._sync_comparison(
                                adapter,
                                comparison_plan,
                            ),
                        }
                    self._write_binding(run_id, binding)
                    receipt = {
                        "schema_version": 1,
                        "event_id": plan.plan_id,
                        "run_id": run_id,
                        "recorder_id": self.configuration.id,
                        "recorder_config_hash": self.configuration.configuration_hash,
                        "status": "synced",
                        "recorded_at": _timestamp(),
                        "external_experiment_id": binding.external_experiment_id,
                        "external_run_id": binding.external_run_id,
                        "details": dict(details),
                    }
                    _atomic_json(receipt_path, receipt)
                    self._supersede_older_events(run_root, plan.plan_id)
                    return self.status(run_id)
                except Exception as error:
                    if binding is not None:
                        self._write_binding(run_id, binding)
                    self._write_error(run_root, event_id, error)
                    if raise_on_error:
                        if isinstance(error, RecordingError):
                            raise
                        raise RecordingError(
                            f"recorder {self.configuration.id!r} sync failed: {error}"
                        ) from error
                    return self.status(run_id)
            finally:
                del adapter
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

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
        if not self.recording.enabled and not self._run_root(run_id).exists():
            return {
                "schema_version": 1,
                "run_id": run_id,
                "recorder": self.configuration.id,
                "recorder_config_hash": self.configuration.configuration_hash,
                "status": RecordingStatus.DISABLED.value,
                "pending_events": 0,
                "synced_events": 0,
            }
        root = self._run_root(run_id)
        events = tuple(sorted((root / "events").glob("*.json"))) if root.exists() else ()
        receipts = (
            {path.name for path in (root / "receipts").glob("*.json")}
            if (root / "receipts").is_dir()
            else set()
        )
        pending = [path for path in events if path.name not in receipts]
        errors = tuple(sorted((root / "errors").glob("*.json"))) if root.exists() else ()
        binding = self._load_binding(run_id, required=False)
        latest_error: Mapping[str, object] | None = None
        if errors:
            try:
                latest_error = _load_mapping(errors[-1], "recorder error")
            except (IntegrityError, ResolutionError):
                latest_error = {"message": "recorder error record is unreadable"}
        if pending and receipts:
            state = RecordingStatus.PARTIALLY_SYNCED
        elif pending and latest_error is not None:
            state = RecordingStatus.FAILED
        elif pending:
            state = RecordingStatus.PENDING
        elif latest_error is not None and not events:
            state = RecordingStatus.FAILED
        elif events:
            state = RecordingStatus.SYNCED
        else:
            state = RecordingStatus.PENDING
        return {
            "schema_version": 1,
            "run_id": run_id,
            "recorder": self.configuration.id,
            "recorder_config_hash": self.configuration.configuration_hash,
            "status": state.value,
            "pending_events": len(pending),
            "synced_events": len(receipts),
            "external_experiment_id": (None if binding is None else binding.external_experiment_id),
            "external_run_id": None if binding is None else binding.external_run_id,
            "last_error": None if latest_error is None else dict(latest_error),
        }

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
        details = adapter.sync(plan, binding)
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

    def _write_error(self, root: Path, event_id: str, error: Exception) -> None:
        errors = root / "errors"
        errors.mkdir(parents=True, exist_ok=True)
        message = str(error)
        for name, value in self.environment.items():
            if (
                any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
                and value
            ):
                message = message.replace(value, "[REDACTED]")
        identity = {
            "schema_version": 1,
            "event_id": event_id,
            "kind": type(error).__name__.lower(),
            "message": message,
            "at": _timestamp(),
        }
        digest = content_hash(identity).removeprefix("sha256:")
        _atomic_json(errors / f"{digest}.json", identity, replace=False)

    @staticmethod
    def _supersede_older_events(root: Path, current_event_id: str) -> None:
        events = root / "events"
        receipts = root / "receipts"
        receipts.mkdir(parents=True, exist_ok=True)
        for path in sorted(events.glob("*.json")):
            event = _load_mapping(path, "recorder event")
            event_id = event.get("event_id")
            if event_id == current_event_id or (receipts / path.name).exists():
                continue
            _atomic_json(
                receipts / path.name,
                {
                    "schema_version": 1,
                    "event_id": event_id,
                    "status": "superseded",
                    "superseded_by": current_event_id,
                    "recorded_at": _timestamp(),
                },
                replace=False,
            )


def load_recording_plan_event(path: Path) -> RecordingPlan:
    event = _load_mapping(path, "recorder event")
    plan = RecordingPlan.from_mapping(_mapping(event.get("plan"), "recorder event plan"))
    if event.get("event_id") != plan.plan_id:
        raise IntegrityError("recorder event and plan identities disagree")
    return plan


def recorder_service(
    store: FilesystemArtifactStore,
    *,
    recording: RecordingConfiguration,
    recorders: Mapping[str, RecorderConfiguration],
    environment: Mapping[str, str],
    adapter_factory: RecorderFactory | None = None,
) -> RecorderService:
    try:
        configuration = recorders[recording.recorder]
    except KeyError as error:
        raise ConfigurationError(
            f"recording references unknown recorder {recording.recorder!r}"
        ) from error
    return RecorderService(
        store,
        recording,
        configuration,
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
