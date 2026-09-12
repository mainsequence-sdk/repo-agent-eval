from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from .errors import ConfigurationError, IntegrityError, ResolutionError
from .hashing import canonical_json_bytes, content_hash, json_value
from .models import ArtifactReference
from .storage import FilesystemArtifactStore


_CONTENT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class StageKind(str, Enum):
    AUTHOR = "author"
    CALIBRATE_JUDGE = "calibrate_judge"
    SOLVE = "solve"
    JUDGE = "judge"
    OPTIMIZE = "optimize"
    FINAL_EVALUATE = "final_evaluate"
    AGGREGATE = "aggregate"


class StageState(str, Enum):
    PREPARED = "prepared"
    CLAIMED = "claimed"
    SUBMITTED = "submitted"
    VALIDATED = "validated"
    PERSISTED = "persisted"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class StageArtifact:
    id: str
    content_id: str
    media_type: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> StageArtifact:
        artifact_id = _required_string(payload.get("id"), "stage artifact id")
        if _IDENTIFIER.fullmatch(artifact_id) is None:
            raise ConfigurationError("stage artifact id must be a lowercase identifier")
        return cls(
            artifact_id,
            _content_id(payload.get("content_id"), "stage artifact content_id"),
            _required_string(payload.get("media_type"), "stage artifact media_type"),
        )


@dataclass(frozen=True)
class StageExecutorIdentity:
    id: str
    adapter: str
    configuration_hash: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> StageExecutorIdentity:
        return cls(
            _identifier(payload.get("id"), "stage executor id"),
            _identifier(payload.get("adapter"), "stage executor adapter"),
            _content_id(
                payload.get("configuration_hash"), "stage executor configuration_hash"
            ),
        )


@dataclass(frozen=True)
class StageModelIdentity:
    id: str | None
    configuration_hash: str | None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> StageModelIdentity:
        value = payload.get("id")
        raw_hash = payload.get("configuration_hash")
        if (value is None) != (raw_hash is None):
            raise ConfigurationError(
                "stage model id and configuration_hash must either both be set or both be null"
            )
        return cls(
            None if value is None else _required_string(value, "stage model id"),
            None
            if raw_hash is None
            else _content_id(raw_hash, "stage model configuration_hash"),
        )


@dataclass(frozen=True)
class StageBudget:
    model_calls: int
    tokens: int
    wall_seconds: float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> StageBudget:
        calls = payload.get("model_calls")
        tokens = payload.get("tokens")
        wall = payload.get("wall_seconds")
        if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
            raise ConfigurationError("stage budget model_calls must be non-negative")
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            raise ConfigurationError("stage budget tokens must be non-negative")
        if not isinstance(wall, (int, float)) or isinstance(wall, bool) or wall <= 0:
            raise ConfigurationError("stage budget wall_seconds must be positive")
        return cls(calls, tokens, float(wall))


@dataclass(frozen=True)
class StagePacket:
    schema_version: int
    packet_id: str
    run_id: str
    experiment_id: str
    stage: StageKind
    attempt: int
    dependencies: tuple[str, ...]
    inputs: tuple[StageArtifact, ...]
    instructions: StageArtifact
    output_schema: str
    executor: StageExecutorIdentity
    model: StageModelIdentity
    budget: StageBudget

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        experiment_id: str,
        stage: StageKind,
        attempt: int,
        dependencies: Sequence[str],
        inputs: Sequence[StageArtifact],
        instructions: StageArtifact,
        output_schema: str,
        executor: StageExecutorIdentity,
        model: StageModelIdentity,
        budget: StageBudget,
    ) -> StagePacket:
        if attempt < 1:
            raise ConfigurationError("stage packet attempt must be positive")
        normalized_budget = StageBudget(
            budget.model_calls, budget.tokens, float(budget.wall_seconds)
        )
        dependencies_tuple = tuple(dependencies)
        if len(dependencies_tuple) != len(set(dependencies_tuple)):
            raise ConfigurationError("stage packet dependencies must be unique")
        for dependency in dependencies_tuple:
            _content_id(dependency, "stage packet dependency")
        inputs_tuple = tuple(inputs)
        input_ids = [item.id for item in inputs_tuple]
        if len(input_ids) != len(set(input_ids)):
            raise ConfigurationError("stage packet input ids must be unique")
        identity = {
            "schema_version": 1,
            "run_id": _required_string(run_id, "stage packet run_id"),
            "experiment_id": _identifier(experiment_id, "stage packet experiment_id"),
            "stage": stage.value,
            "attempt": attempt,
            "dependencies": dependencies_tuple,
            "inputs": inputs_tuple,
            "instructions": instructions,
            "output_contract": {"schema_id": _identifier(output_schema, "output schema")},
            "executor": executor,
            "model": model,
            "budget": normalized_budget,
        }
        return cls(
            schema_version=1,
            packet_id=content_hash(identity),
            run_id=identity["run_id"],  # type: ignore[arg-type]
            experiment_id=identity["experiment_id"],  # type: ignore[arg-type]
            stage=stage,
            attempt=attempt,
            dependencies=dependencies_tuple,
            inputs=inputs_tuple,
            instructions=instructions,
            output_schema=identity["output_contract"]["schema_id"],  # type: ignore[index]
            executor=executor,
            model=model,
            budget=normalized_budget,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> StagePacket:
        if payload.get("schema_version") != 1:
            raise ConfigurationError("stage packet schema_version must be 1")
        raw_inputs = _sequence(payload.get("inputs"), "stage packet inputs")
        raw_dependencies = _sequence(
            payload.get("dependencies", []), "stage packet dependencies"
        )
        try:
            stage = StageKind(payload.get("stage"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError("unknown stage packet kind") from error
        output_contract = _mapping(payload.get("output_contract"), "output_contract")
        packet = cls.create(
            run_id=_required_string(payload.get("run_id"), "stage packet run_id"),
            experiment_id=_identifier(
                payload.get("experiment_id"), "stage packet experiment_id"
            ),
            stage=stage,
            attempt=_positive_int(payload.get("attempt"), "stage packet attempt"),
            dependencies=tuple(
                _content_id(item, "stage packet dependency") for item in raw_dependencies
            ),
            inputs=tuple(
                StageArtifact.from_mapping(_mapping(item, "stage packet input"))
                for item in raw_inputs
            ),
            instructions=StageArtifact.from_mapping(
                _mapping(payload.get("instructions"), "stage packet instructions")
            ),
            output_schema=_identifier(output_contract.get("schema_id"), "output schema"),
            executor=StageExecutorIdentity.from_mapping(
                _mapping(payload.get("executor"), "stage packet executor")
            ),
            model=StageModelIdentity.from_mapping(
                _mapping(payload.get("model"), "stage packet model")
            ),
            budget=StageBudget.from_mapping(
                _mapping(payload.get("budget"), "stage packet budget")
            ),
        )
        if payload.get("packet_id") != packet.packet_id:
            raise IntegrityError("stage packet identity hash is invalid")
        return packet

    def as_dict(self) -> Mapping[str, object]:
        payload = json_value(self)
        assert isinstance(payload, dict)
        payload["stage"] = self.stage.value
        payload["output_contract"] = {"schema_id": payload.pop("output_schema")}
        return payload


@dataclass(frozen=True)
class StageError:
    kind: str
    message: str


@dataclass(frozen=True)
class StageResult:
    schema_version: int
    result_id: str
    packet_id: str
    attempt: int
    status: str
    executor: Mapping[str, object]
    model: Mapping[str, object]
    outputs: tuple[StageArtifact, ...]
    usage: Mapping[str, object]
    provenance: Mapping[str, object]
    timing: Mapping[str, object]
    error: StageError | None

    @classmethod
    def create(
        cls,
        *,
        packet: StagePacket,
        status: str,
        executor_version: str | None,
        worker_id: str,
        outputs: Sequence[StageArtifact] = (),
        usage: Mapping[str, object] | None = None,
        provenance: Mapping[str, object] | None = None,
        started_at: str,
        completed_at: str,
        observed_model: str | None = None,
        error: StageError | None = None,
    ) -> StageResult:
        if status not in {"completed", "failed"}:
            raise ConfigurationError("stage result status must be completed or failed")
        if status == "completed" and error is not None:
            raise ConfigurationError("completed stage result cannot contain an error")
        if status == "failed" and error is None:
            raise ConfigurationError("failed stage result requires an error")
        output_tuple = tuple(outputs)
        output_ids = [item.id for item in output_tuple]
        if len(output_ids) != len(set(output_ids)):
            raise ConfigurationError("stage result output ids must be unique")
        executor = {
            "id": packet.executor.id,
            "adapter": packet.executor.adapter,
            "version": executor_version,
            "worker_id": _required_string(worker_id, "stage result worker_id"),
        }
        model = {
            "id": packet.model.id,
            "observed_id": observed_model,
        }
        timing = {"started_at": started_at, "completed_at": completed_at}
        identity = {
            "schema_version": 2,
            "packet_id": packet.packet_id,
            "attempt": packet.attempt,
            "status": status,
            "executor": executor,
            "model": model,
            "outputs": output_tuple,
            "usage": dict(usage or {}),
            "provenance": dict(provenance or {}),
            "timing": timing,
            "error": error,
        }
        return cls(
            schema_version=2,
            result_id=content_hash(identity),
            packet_id=packet.packet_id,
            attempt=packet.attempt,
            status=status,
            executor=MappingProxyType(executor),
            model=MappingProxyType(model),
            outputs=output_tuple,
            usage=MappingProxyType(dict(usage or {})),
            provenance=MappingProxyType(dict(provenance or {})),
            timing=MappingProxyType(timing),
            error=error,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> StageResult:
        if payload.get("schema_version") != 2:
            raise ConfigurationError("stage result schema_version must be 2")
        packet_id = _content_id(payload.get("packet_id"), "stage result packet_id")
        attempt = _positive_int(payload.get("attempt"), "stage result attempt")
        status = _required_string(payload.get("status"), "stage result status")
        executor = _mapping(payload.get("executor"), "stage result executor")
        model = _mapping(payload.get("model"), "stage result model")
        raw_error = payload.get("error")
        error = None
        if raw_error is not None:
            error_payload = _mapping(raw_error, "stage result error")
            error = StageError(
                _identifier(error_payload.get("kind"), "stage result error kind"),
                _required_string(error_payload.get("message"), "stage result error message"),
            )
        synthetic = StagePacket.create(
            run_id="validation-only",
            experiment_id="validation-only",
            stage=StageKind.AGGREGATE,
            attempt=attempt,
            dependencies=(),
            inputs=(),
            instructions=StageArtifact(
                "instructions", f"sha256:{'0' * 64}", "application/json"
            ),
            output_schema="validation-only",
            executor=StageExecutorIdentity(
                _identifier(executor.get("id"), "stage result executor id"),
                _identifier(executor.get("adapter"), "stage result executor adapter"),
                f"sha256:{'0' * 64}",
            ),
            model=StageModelIdentity(
                None
                if model.get("id") is None
                else _required_string(model.get("id"), "stage result model id"),
                None if model.get("id") is None else f"sha256:{'0' * 64}",
            ),
            budget=StageBudget(0, 0, 1),
        )
        synthetic = replace(synthetic, packet_id=packet_id)
        result = cls.create(
            packet=synthetic,
            status=status,
            executor_version=(
                None
                if executor.get("version") is None
                else _required_string(executor.get("version"), "executor version")
            ),
            worker_id=_required_string(executor.get("worker_id"), "executor worker_id"),
            outputs=tuple(
                StageArtifact.from_mapping(_mapping(item, "stage result output"))
                for item in _sequence(payload.get("outputs", []), "stage result outputs")
            ),
            usage=_mapping(payload.get("usage", {}), "stage result usage"),
            provenance=_mapping(
                payload.get("provenance", {}), "stage result provenance"
            ),
            started_at=_required_string(
                _mapping(payload.get("timing"), "stage result timing").get("started_at"),
                "stage result started_at",
            ),
            completed_at=_required_string(
                _mapping(payload.get("timing"), "stage result timing").get("completed_at"),
                "stage result completed_at",
            ),
            observed_model=(
                None
                if model.get("observed_id") is None
                else _required_string(model.get("observed_id"), "observed model id")
            ),
            error=error,
        )
        if payload.get("result_id") != result.result_id:
            raise IntegrityError("stage result identity hash is invalid")
        return result

    def as_dict(self) -> Mapping[str, object]:
        payload = json_value(self)
        assert isinstance(payload, dict)
        return payload


@dataclass(frozen=True)
class StageClaim:
    packet_id: str
    executor_id: str
    worker_id: str
    lease_id: str
    claimed_at: str
    expires_at_epoch: float


@dataclass(frozen=True)
class StageStatus:
    packet: StagePacket
    state: StageState
    claim: StageClaim | None
    result: StageResult | None
    history: tuple[Mapping[str, object], ...]


class StageLedger:
    """Append-only packet/result ledger stored below the external data root."""

    def __init__(
        self,
        store: FilesystemArtifactStore,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.clock = clock
        self._lock_path = store.data_root / "stage-ledger.lock"

    def create_run(self, run_id: str, document: Mapping[str, object]) -> ArtifactReference:
        return self.store.put_manifest(f"runs/{run_id}/run", document)

    def load_run(self, run_id: str) -> Mapping[str, object]:
        path = self.store.manifest_root / "runs" / run_id / "run.json"
        return self._load_mapping(path, "run")

    def prepare(self, packet: StagePacket) -> StagePacket:
        self._validate_dependencies(packet)
        self.store.put_manifest(
            f"stage-packets/{packet.packet_id.removeprefix('sha256:')}", packet.as_dict()
        )
        current = self.status_or_none(packet.packet_id)
        if current is None:
            self._append_transition(packet, StageState.PREPARED, {})
        elif current.packet.as_dict() != packet.as_dict():
            raise IntegrityError("packet id already identifies different packet bytes")
        return packet

    def load_packet(self, packet_id: str) -> StagePacket:
        digest = _content_id(packet_id, "packet id").removeprefix("sha256:")
        path = self.store.manifest_root / "stage-packets" / f"{digest}.json"
        return StagePacket.from_mapping(self._load_mapping(path, "stage packet"))

    def list(self, run_id: str) -> tuple[StageStatus, ...]:
        root = self.store.manifest_root / "runs" / run_id / "transitions"
        if not root.is_dir():
            return ()
        packet_ids = {
            str(self._load_mapping(path, "stage transition")["packet_id"])
            for path in root.glob("*.json")
        }
        return tuple(
            sorted(
                (self.status(packet_id) for packet_id in packet_ids),
                key=lambda item: (
                    list(StageKind).index(item.packet.stage),
                    item.packet.attempt,
                    item.packet.packet_id,
                ),
            )
        )

    def claim(
        self,
        packet_id: str,
        *,
        executor_id: str,
        worker_id: str,
        lease_seconds: float = 600,
    ) -> StageClaim:
        if lease_seconds <= 0:
            raise ConfigurationError("claim lease_seconds must be positive")
        packet = self.load_packet(packet_id)
        if executor_id != packet.executor.id:
            raise IntegrityError("claim executor does not match the packet executor")
        status = self.status(packet_id)
        now = self.clock()
        if status.state is StageState.CLAIMED and status.claim is not None:
            if status.claim.expires_at_epoch > now:
                raise IntegrityError("stage packet already has an active claim")
            self._append_transition(
                packet,
                StageState.PREPARED,
                {"reason": "claim_expired", "lease_id": status.claim.lease_id},
            )
        elif status.state is not StageState.PREPARED:
            raise IntegrityError(f"cannot claim a packet in state {status.state.value}")
        claim = StageClaim(
            packet.packet_id,
            executor_id,
            _required_string(worker_id, "claim worker_id"),
            str(uuid4()),
            _timestamp(),
            now + lease_seconds,
        )
        self._append_transition(packet, StageState.CLAIMED, {"claim": json_value(claim)})
        return claim

    def submit(self, result: StageResult, *, lease_id: str) -> StageResult:
        packet = self.load_packet(result.packet_id)
        if result.attempt != packet.attempt:
            raise IntegrityError("stage result attempt does not match its packet")
        if result.executor.get("id") != packet.executor.id:
            raise IntegrityError("stage result executor does not match its packet")
        if result.model.get("id") != packet.model.id:
            raise IntegrityError("stage result model does not match its packet")
        current = self.status(packet.packet_id)
        if current.result is not None:
            if current.result.result_id == result.result_id:
                return current.result
            raise IntegrityError("conflicting result submitted for the same packet attempt")
        if current.state is not StageState.CLAIMED or current.claim is None:
            raise IntegrityError("stage packet must be claimed before submission")
        if current.claim.lease_id != lease_id:
            raise IntegrityError("submission lease does not match the active claim")
        if result.executor.get("worker_id") != current.claim.worker_id:
            raise IntegrityError("submission worker does not match the active claim")
        if current.claim.expires_at_epoch <= self.clock():
            raise IntegrityError("submission claim lease has expired")
        for output in result.outputs:
            self.store.reference(output.content_id, output.media_type)
        self.store.put_manifest(
            f"stage-results/{result.result_id.removeprefix('sha256:')}", result.as_dict()
        )
        state = StageState.SUBMITTED if result.status == "completed" else StageState.FAILED
        self._append_transition(
            packet,
            state,
            {"result_id": result.result_id, "lease_id": lease_id},
        )
        return result

    def validate(
        self,
        packet_id: str,
        validator: Callable[[StagePacket, StageResult], None],
    ) -> StageResult:
        status = self.status(packet_id)
        if status.state is StageState.VALIDATED and status.result is not None:
            return status.result
        if status.state is not StageState.SUBMITTED or status.result is None:
            raise IntegrityError("only submitted stage results can be validated")
        try:
            validator(status.packet, status.result)
        except Exception as error:
            self._append_transition(
                status.packet,
                StageState.REJECTED,
                {
                    "result_id": status.result.result_id,
                    "reason": "validation_failed",
                    "error_kind": type(error).__name__.lower(),
                },
            )
            raise
        self._append_transition(
            status.packet,
            StageState.VALIDATED,
            {"result_id": status.result.result_id},
        )
        return status.result

    def persist(self, packet_id: str) -> StageResult:
        status = self.status(packet_id)
        if status.state is StageState.PERSISTED and status.result is not None:
            return status.result
        if status.state is not StageState.VALIDATED or status.result is None:
            raise IntegrityError("only validated stage results can be persisted")
        self._append_transition(
            status.packet,
            StageState.PERSISTED,
            {"result_id": status.result.result_id},
        )
        return status.result

    def retry(self, packet_id: str) -> StagePacket:
        original = self.load_packet(packet_id)
        current = self.status(packet_id)
        if current.state not in {StageState.FAILED, StageState.REJECTED}:
            raise IntegrityError("only failed or rejected packets can be retried")
        retry = StagePacket.create(
            run_id=original.run_id,
            experiment_id=original.experiment_id,
            stage=original.stage,
            attempt=original.attempt + 1,
            dependencies=original.dependencies,
            inputs=original.inputs,
            instructions=original.instructions,
            output_schema=original.output_schema,
            executor=original.executor,
            model=original.model,
            budget=original.budget,
        )
        return self.prepare(retry)

    def status_or_none(self, packet_id: str) -> StageStatus | None:
        packet = self.load_packet(packet_id)
        history = self._history(packet)
        if not history:
            return None
        return self._status(packet, history)

    def status(self, packet_id: str) -> StageStatus:
        packet = self.load_packet(packet_id)
        history = self._history(packet)
        if not history:
            raise ResolutionError(f"stage packet has no run transition: {packet_id}")
        return self._status(packet, history)

    def read_output(self, result: StageResult, output_id: str = "result") -> object:
        output = next((item for item in result.outputs if item.id == output_id), None)
        if output is None:
            raise IntegrityError(f"stage result has no {output_id!r} output")
        reference = self.store.reference(output.content_id, output.media_type)
        with self.store.get_blob(reference) as handle:
            return json.load(handle)

    def put_json(self, artifact_id: str, value: object) -> StageArtifact:
        reference = self.store.put_blob(
            BytesIO(canonical_json_bytes(value)), "application/json"
        )
        return StageArtifact(artifact_id, reference.content_id, reference.media_type)

    def _validate_dependencies(self, packet: StagePacket) -> None:
        for dependency in packet.dependencies:
            status = self.status(dependency)
            if status.packet.run_id != packet.run_id:
                raise IntegrityError("stage dependency belongs to another run")
            if status.state is not StageState.PERSISTED:
                raise IntegrityError("stage dependency is not persisted")

    def _history(self, packet: StagePacket) -> tuple[Mapping[str, object], ...]:
        root = self.store.manifest_root / "runs" / packet.run_id / "transitions"
        if not root.is_dir():
            return ()
        history = []
        for path in root.glob("*.json"):
            payload = self._load_mapping(path, "stage transition")
            if payload.get("packet_id") == packet.packet_id:
                history.append(payload)
        return tuple(sorted(history, key=lambda item: int(item["sequence"])))

    def _status(
        self,
        packet: StagePacket,
        history: tuple[Mapping[str, object], ...],
    ) -> StageStatus:
        latest = history[-1]
        state = StageState(str(latest["state"]))
        claim = None
        for event in reversed(history):
            raw_claim = event.get("claim")
            if raw_claim is None:
                continue
            claim_payload = _mapping(raw_claim, "claim")
            claim = StageClaim(
                packet.packet_id,
                _required_string(claim_payload.get("executor_id"), "claim executor_id"),
                _required_string(claim_payload.get("worker_id"), "claim worker_id"),
                _required_string(claim_payload.get("lease_id"), "claim lease_id"),
                _required_string(claim_payload.get("claimed_at"), "claim claimed_at"),
                float(claim_payload.get("expires_at_epoch", 0)),
            )
            break
        result = None
        for event in reversed(history):
            raw_result_id = event.get("result_id")
            if raw_result_id is None:
                continue
            result_id = _content_id(raw_result_id, "transition result_id")
            path = (
                self.store.manifest_root
                / "stage-results"
                / f"{result_id.removeprefix('sha256:')}.json"
            )
            result = StageResult.from_mapping(self._load_mapping(path, "stage result"))
            break
        return StageStatus(packet, state, claim, result, history)

    def _append_transition(
        self,
        packet: StagePacket,
        state: StageState,
        details: Mapping[str, object],
    ) -> None:
        import fcntl

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            root = self.store.manifest_root / "runs" / packet.run_id / "transitions"
            root.mkdir(parents=True, exist_ok=True)
            sequence = len(tuple(root.glob("*.json"))) + 1
            payload = {
                "schema_version": 1,
                "sequence": sequence,
                "transition_id": str(uuid4()),
                "packet_id": packet.packet_id,
                "stage": packet.stage.value,
                "attempt": packet.attempt,
                "state": state.value,
                "at": _timestamp(),
                **dict(details),
            }
            self.store.put_manifest(
                f"runs/{packet.run_id}/transitions/{sequence:08d}-{payload['transition_id']}",
                payload,
            )
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _load_mapping(path: Path, label: str) -> Mapping[str, object]:
        if not path.is_file() or path.is_symlink():
            raise ResolutionError(f"{label} does not exist: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise IntegrityError(f"{label} must contain a JSON object")
        return {str(key): value for key, value in payload.items()}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ConfigurationError(f"{field} must be a list")
    return value


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty string")
    return value.strip()


def _identifier(value: object, field: str) -> str:
    text = _required_string(value, field)
    if _IDENTIFIER.fullmatch(text) is None:
        raise ConfigurationError(f"{field} must be a lowercase identifier")
    return text


def _content_id(value: object, field: str) -> str:
    text = _required_string(value, field)
    if _CONTENT_ID.fullmatch(text) is None:
        raise ConfigurationError(f"{field} must be a sha256 content id")
    return text


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigurationError(f"{field} must be a positive integer")
    return value
