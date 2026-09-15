from __future__ import annotations

import fcntl
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from .errors import ConfigurationError, IntegrityError, ResolutionError
from .hashing import content_hash, json_value
from .storage import FilesystemArtifactStore


class RunLifecycleState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_HARNESS = "awaiting_harness"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    REPORT_PUBLISHED = "report_published"


@dataclass(frozen=True)
class RunLifecycleEvent:
    schema_version: int
    sequence: int
    event_id: str
    run_id: str
    state: RunLifecycleState
    reason: str | None
    at: str
    details: Mapping[str, object]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> RunLifecycleEvent:
        if payload.get("schema_version") != 1:
            raise IntegrityError("run lifecycle event schema_version must be 1")
        sequence = payload.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise IntegrityError("run lifecycle sequence must be positive")
        try:
            state = RunLifecycleState(payload.get("state"))
        except (TypeError, ValueError) as error:
            raise IntegrityError("run lifecycle event has an unknown state") from error
        details = payload.get("details", {})
        if not isinstance(details, Mapping):
            raise IntegrityError("run lifecycle details must be a mapping")
        reason = payload.get("reason")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise IntegrityError("run lifecycle reason must be a non-empty string")
        event = cls(
            schema_version=1,
            sequence=sequence,
            event_id=_required_string(payload.get("event_id"), "lifecycle event_id"),
            run_id=_required_string(payload.get("run_id"), "lifecycle run_id"),
            state=state,
            reason=None if reason is None else reason.strip(),
            at=_required_string(payload.get("at"), "lifecycle timestamp"),
            details=MappingProxyType({str(key): value for key, value in details.items()}),
        )
        identity = event.identity()
        if event.event_id != content_hash(identity):
            raise IntegrityError("run lifecycle event identity hash is invalid")
        return event

    def identity(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "state": self.state.value,
            "reason": self.reason,
            "at": self.at,
            "details": dict(self.details),
        }

    def as_dict(self) -> Mapping[str, object]:
        value = json_value(self)
        assert isinstance(value, dict)
        value["state"] = self.state.value
        return value


class RunLifecycleLedger:
    """Append-only canonical lifecycle for runs outside stage transitions."""

    def __init__(
        self,
        store: FilesystemArtifactStore,
        *,
        timestamp: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.timestamp = timestamp or (lambda: datetime.now(UTC).isoformat())
        self._lock_path = store.operations_root / "run-lifecycle.lock"

    def append(
        self,
        run_id: str,
        state: RunLifecycleState,
        *,
        reason: str | None = None,
        details: Mapping[str, object] | None = None,
        if_changed: bool = False,
    ) -> RunLifecycleEvent:
        run_id = _required_string(run_id, "lifecycle run_id")
        self._require_run(run_id)
        normalized_reason = None if reason is None else _required_string(reason, "reason")
        normalized_details = dict(details or {})
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                existing = self.list(run_id)
                if if_changed and existing:
                    latest = existing[-1]
                    if (
                        latest.state is state
                        and latest.reason == normalized_reason
                        and dict(latest.details) == normalized_details
                    ):
                        return latest
                sequence = len(existing) + 1
                identity = {
                    "schema_version": 1,
                    "sequence": sequence,
                    "run_id": run_id,
                    "state": state.value,
                    "reason": normalized_reason,
                    "at": self.timestamp(),
                    "details": normalized_details,
                }
                event = RunLifecycleEvent(
                    schema_version=1,
                    sequence=sequence,
                    event_id=content_hash(identity),
                    run_id=run_id,
                    state=state,
                    reason=normalized_reason,
                    at=str(identity["at"]),
                    details=MappingProxyType(normalized_details),
                )
                digest = event.event_id.removeprefix("sha256:")
                self.store.put_manifest(
                    f"runs/{run_id}/lifecycle/{sequence:08d}-{digest}",
                    event.as_dict(),
                )
                return event
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def list(self, run_id: str) -> tuple[RunLifecycleEvent, ...]:
        root = self.store.manifest_root / "runs" / run_id / "lifecycle"
        if not root.is_dir():
            return ()
        events = tuple(
            RunLifecycleEvent.from_mapping(_load_mapping(path, "run lifecycle event"))
            for path in sorted(root.glob("*.json"))
        )
        for expected, event in enumerate(events, start=1):
            if event.sequence != expected or event.run_id != run_id:
                raise IntegrityError("run lifecycle sequence is incomplete or inconsistent")
        return events

    def current(self, run_id: str) -> RunLifecycleEvent | None:
        events = self.list(run_id)
        return events[-1] if events else None

    def latest_sequence(self, run_id: str) -> int:
        """Return the current lifecycle high-water mark."""

        current = self.current(run_id)
        return 0 if current is None else current.sequence

    def _require_run(self, run_id: str) -> None:
        path = self.store.manifest_root / "runs" / run_id / "run.json"
        if not path.is_file() or path.is_symlink():
            raise ResolutionError(f"run does not exist: {run_id}")


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


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty string")
    return value.strip()
