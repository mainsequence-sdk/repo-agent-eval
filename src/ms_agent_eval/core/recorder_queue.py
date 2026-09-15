from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from uuid import uuid4

from .errors import ConfigurationError, IntegrityError, ResolutionError
from .hashing import canonical_json_bytes, content_hash, json_value


RECORDER_QUEUE_SCHEMA_VERSION = 1


class ProjectionIntentKind(str, Enum):
    LIVE_PROGRESS = "live_progress"
    STAGE_BOUNDARY = "stage_boundary"
    FINALIZATION = "finalization"
    RECONCILE = "reconcile"
    BACKFILL = "backfill"


_REQUIRED_KINDS = {
    ProjectionIntentKind.STAGE_BOUNDARY,
    ProjectionIntentKind.FINALIZATION,
    ProjectionIntentKind.RECONCILE,
    ProjectionIntentKind.BACKFILL,
}


@dataclass(frozen=True)
class ProjectionIntent:
    schema_version: int
    intent_id: str
    recorder_id: str
    recorder_config_hash: str
    workspace_id: str
    run_id: str
    experiment_revision_id: str
    kind: ProjectionIntentKind
    target_sequence: int
    priority: int
    created_at: str
    stage: str | None = None
    boundary_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        recorder_id: str,
        recorder_config_hash: str,
        workspace_id: str,
        run_id: str,
        experiment_revision_id: str,
        kind: ProjectionIntentKind,
        target_sequence: int,
        priority: int,
        stage: str | None = None,
        boundary_id: str | None = None,
        created_at: str | None = None,
    ) -> ProjectionIntent:
        if target_sequence < 0:
            raise ConfigurationError("projection target_sequence must be non-negative")
        if not 0 <= priority <= 100:
            raise ConfigurationError("projection priority must be between 0 and 100")
        timestamp = created_at or datetime.now(UTC).isoformat()
        identity = {
            "schema_version": RECORDER_QUEUE_SCHEMA_VERSION,
            "recorder_id": recorder_id,
            "recorder_config_hash": recorder_config_hash,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "experiment_revision_id": experiment_revision_id,
            "kind": kind.value,
            "target_sequence": target_sequence,
            "priority": priority,
            "stage": stage,
            "boundary_id": boundary_id,
        }
        return cls(
            RECORDER_QUEUE_SCHEMA_VERSION,
            content_hash(identity),
            recorder_id,
            recorder_config_hash,
            workspace_id,
            run_id,
            experiment_revision_id,
            kind,
            target_sequence,
            priority,
            timestamp,
            stage,
            boundary_id,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ProjectionIntent:
        if payload.get("schema_version") != RECORDER_QUEUE_SCHEMA_VERSION:
            raise IntegrityError(
                f"projection intent schema_version must be {RECORDER_QUEUE_SCHEMA_VERSION}"
            )
        try:
            kind = ProjectionIntentKind(payload.get("kind"))
        except (TypeError, ValueError) as error:
            raise IntegrityError("projection intent kind is invalid") from error
        intent = cls.create(
            recorder_id=_string(payload.get("recorder_id"), "projection recorder_id"),
            recorder_config_hash=_string(
                payload.get("recorder_config_hash"), "projection recorder_config_hash"
            ),
            workspace_id=_string(payload.get("workspace_id"), "projection workspace_id"),
            run_id=_string(payload.get("run_id"), "projection run_id"),
            experiment_revision_id=_string(
                payload.get("experiment_revision_id"),
                "projection experiment_revision_id",
            ),
            kind=kind,
            target_sequence=_integer(
                payload.get("target_sequence"), "projection target_sequence", minimum=0
            ),
            priority=_integer(payload.get("priority"), "projection priority", minimum=0),
            stage=_optional_string(payload.get("stage")),
            boundary_id=_optional_string(payload.get("boundary_id")),
            created_at=_string(payload.get("created_at"), "projection created_at"),
        )
        if intent.priority > 100:
            raise IntegrityError("projection priority must be between 0 and 100")
        if payload.get("intent_id") != intent.intent_id:
            raise IntegrityError("projection intent identity hash is invalid")
        return intent

    @property
    def required(self) -> bool:
        return self.kind in _REQUIRED_KINDS

    def as_dict(self) -> Mapping[str, object]:
        value = json_value(self)
        assert isinstance(value, dict)
        value["kind"] = self.kind.value
        return value


@dataclass(frozen=True)
class ClaimedProjectionIntent:
    intent: ProjectionIntent
    worker_id: str
    lease_id: str
    claimed_at: str
    expires_at_epoch: float
    attempts: int
    path: Path


@dataclass(frozen=True)
class ProjectionCursor:
    schema_version: int
    recorder_config_hash: str
    run_id: str
    projected_through: int
    finalized_through: int | None
    updated_at: str

    @classmethod
    def initial(cls, recorder_config_hash: str, run_id: str) -> ProjectionCursor:
        return cls(1, recorder_config_hash, run_id, 0, None, datetime.now(UTC).isoformat())

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ProjectionCursor:
        if payload.get("schema_version") != 1:
            raise IntegrityError("projection cursor schema_version must be 1")
        finalized = payload.get("finalized_through")
        return cls(
            1,
            _string(payload.get("recorder_config_hash"), "cursor recorder_config_hash"),
            _string(payload.get("run_id"), "cursor run_id"),
            _integer(payload.get("projected_through"), "cursor projected_through", minimum=0),
            (
                None
                if finalized is None
                else _integer(finalized, "cursor finalized_through", minimum=0)
            ),
            _string(payload.get("updated_at"), "cursor updated_at"),
        )

    def advance(self, intent: ProjectionIntent) -> ProjectionCursor:
        return ProjectionCursor(
            1,
            self.recorder_config_hash,
            self.run_id,
            max(self.projected_through, intent.target_sequence),
            (
                max(self.finalized_through or 0, intent.target_sequence)
                if intent.kind is ProjectionIntentKind.FINALIZATION
                else self.finalized_through
            ),
            datetime.now(UTC).isoformat(),
        )

    def as_dict(self) -> Mapping[str, object]:
        value = json_value(self)
        assert isinstance(value, dict)
        return value


class FilesystemRecorderQueue:
    """Durable queue of pointer-only recorder projection intents."""

    def __init__(
        self,
        root: Path,
        *,
        live_coalescing: bool = True,
        lease_seconds: int = 300,
        max_attempts: int = 12,
    ) -> None:
        if lease_seconds < 1:
            raise ConfigurationError("recorder queue lease_seconds must be positive")
        if max_attempts < 1:
            raise ConfigurationError("recorder queue max_attempts must be positive")
        self.root = root
        self.live_coalescing = live_coalescing
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.pending_root = root / "pending"
        self.leased_root = root / "leased"
        self.acknowledged_root = root / "acknowledged"
        self.dead_letter_root = root / "dead-letter"
        self.state_root = root / "state"
        self.lock_path = root / "queue.lock"
        for path in (
            self.pending_root,
            self.leased_root,
            self.acknowledged_root,
            self.dead_letter_root,
            self.state_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def enqueue(
        self,
        intent: ProjectionIntent,
        *,
        minimum_interval_seconds: int = 0,
    ) -> Mapping[str, object]:
        with self._locked():
            path = self.pending_root / self._filename(intent)
            if intent.kind is ProjectionIntentKind.LIVE_PROGRESS and self.live_coalescing:
                existing = self._load_envelope(path, required=False)
                throttle_path = self._live_throttle_path(intent)
                now = time.time()
                if existing is None and throttle_path.is_file() and minimum_interval_seconds > 0:
                    throttle = self._load_mapping(throttle_path, "live projection throttle")
                    last_enqueued = float(throttle.get("last_enqueued_at_epoch", 0.0))
                    if now - last_enqueued < minimum_interval_seconds:
                        return {
                            "status": "throttled",
                            "intent_id": intent.intent_id,
                            "target_sequence": intent.target_sequence,
                        }
                if existing is not None:
                    previous = ProjectionIntent.from_mapping(
                        _mapping(existing.get("intent"), "queued projection intent")
                    )
                    if previous.target_sequence >= intent.target_sequence:
                        return {
                            "status": "coalesced",
                            "intent_id": previous.intent_id,
                            "target_sequence": previous.target_sequence,
                        }
                self._write_envelope(path, intent, attempts=0, replace=True)
                _atomic_json(
                    throttle_path,
                    {
                        "schema_version": 1,
                        "last_enqueued_at_epoch": now,
                        "target_sequence": intent.target_sequence,
                    },
                    replace=True,
                )
                return {
                    "status": "queued",
                    "intent_id": intent.intent_id,
                    "target_sequence": intent.target_sequence,
                }
            if path.exists():
                existing = self._load_envelope(path)
                queued = ProjectionIntent.from_mapping(
                    _mapping(existing.get("intent"), "queued projection intent")
                )
                if queued.intent_id != intent.intent_id:
                    raise IntegrityError("recorder queue key contains different intent data")
                return {
                    "status": "already_queued",
                    "intent_id": intent.intent_id,
                    "target_sequence": intent.target_sequence,
                }
            acknowledged = self.acknowledged_root / path.name
            if acknowledged.exists():
                return {
                    "status": "acknowledged",
                    "intent_id": intent.intent_id,
                    "target_sequence": intent.target_sequence,
                }
            self._write_envelope(path, intent, attempts=0, replace=False)
            return {
                "status": "queued",
                "intent_id": intent.intent_id,
                "target_sequence": intent.target_sequence,
            }

    def claim(
        self,
        *,
        worker_id: str,
        run_id: str | None = None,
        now: float | None = None,
    ) -> ClaimedProjectionIntent | None:
        worker = _safe_component(worker_id, "recorder worker id")
        current = time.time() if now is None else now
        with self._locked():
            self._recover_expired_locked(current)
            candidates: list[tuple[int, str, Path, Mapping[str, object], ProjectionIntent]] = []
            for path in self.pending_root.glob("*.json"):
                envelope = self._load_envelope(path)
                intent = ProjectionIntent.from_mapping(
                    _mapping(envelope.get("intent"), "queued projection intent")
                )
                if run_id is not None and intent.run_id != run_id:
                    continue
                available_at = float(envelope.get("available_at_epoch", 0.0))
                if available_at > current:
                    continue
                candidates.append((-intent.priority, intent.created_at, path, envelope, intent))
            if not candidates:
                return None
            _, _, path, envelope, intent = min(candidates)
            lease_id = str(uuid4())
            claimed_at = datetime.now(UTC).isoformat()
            attempts = _integer(envelope.get("attempts", 0), "queue attempts", minimum=0) + 1
            leased_path = self.leased_root / path.name
            leased = {
                **dict(envelope),
                "attempts": attempts,
                "lease": {
                    "worker_id": worker,
                    "lease_id": lease_id,
                    "claimed_at": claimed_at,
                    "expires_at_epoch": current + self.lease_seconds,
                },
            }
            _atomic_json(leased_path, leased, replace=True)
            path.unlink()
            return ClaimedProjectionIntent(
                intent,
                worker,
                lease_id,
                claimed_at,
                current + self.lease_seconds,
                attempts,
                leased_path,
            )

    def acknowledge(
        self,
        claim: ClaimedProjectionIntent,
        receipt: Mapping[str, object],
    ) -> None:
        with self._locked():
            envelope = self._validate_claim(claim)
            destination = self.acknowledged_root / claim.path.name
            _atomic_json(
                destination,
                {
                    **dict(envelope),
                    "acknowledged_at": datetime.now(UTC).isoformat(),
                    "receipt": dict(receipt),
                },
                replace=True,
            )
            claim.path.unlink()

    def retry(
        self,
        claim: ClaimedProjectionIntent,
        *,
        error_kind: str,
        message: str,
        delay_seconds: float,
    ) -> str:
        with self._locked():
            envelope = self._validate_claim(claim)
            failed = {
                **dict(envelope),
                "lease": None,
                "available_at_epoch": time.time() + max(0.0, delay_seconds),
                "last_error": {
                    "kind": error_kind,
                    "message": message,
                    "at": datetime.now(UTC).isoformat(),
                },
            }
            if claim.attempts >= self.max_attempts:
                destination = self.dead_letter_root / claim.path.name
                state = "dead_letter"
            else:
                destination = self.pending_root / claim.path.name
                state = "retrying"
            _atomic_json(destination, failed, replace=True)
            claim.path.unlink()
            return state

    def status(
        self,
        *,
        recorder_config_hash: str,
        run_id: str,
    ) -> Mapping[str, object]:
        items: dict[str, list[Mapping[str, object]]] = {
            "pending": [],
            "leased": [],
            "acknowledged": [],
            "dead_letter": [],
        }
        roots = {
            "pending": self.pending_root,
            "leased": self.leased_root,
            "acknowledged": self.acknowledged_root,
            "dead_letter": self.dead_letter_root,
        }
        for state, root in roots.items():
            for path in root.glob("*.json"):
                envelope = self._load_envelope(path)
                intent = ProjectionIntent.from_mapping(
                    _mapping(envelope.get("intent"), "queued projection intent")
                )
                if intent.recorder_config_hash == recorder_config_hash and intent.run_id == run_id:
                    items[state].append(envelope)
        pending_dates = [
            str(item.get("enqueued_at"))
            for item in (*items["pending"], *items["leased"])
            if item.get("enqueued_at") is not None
        ]
        return {
            "pending": len(items["pending"]),
            "leased": len(items["leased"]),
            "acknowledged": len(items["acknowledged"]),
            "dead_letter": len(items["dead_letter"]),
            "oldest_pending_at": min(pending_dates) if pending_dates else None,
        }

    def cursor(self, recorder_config_hash: str, run_id: str) -> ProjectionCursor:
        path = self._cursor_path(recorder_config_hash, run_id)
        if not path.is_file():
            return ProjectionCursor.initial(recorder_config_hash, run_id)
        cursor = ProjectionCursor.from_mapping(self._load_mapping(path, "projection cursor"))
        if cursor.recorder_config_hash != recorder_config_hash or cursor.run_id != run_id:
            raise IntegrityError("projection cursor does not match its storage location")
        return cursor

    def write_cursor(self, cursor: ProjectionCursor) -> None:
        _atomic_json(
            self._cursor_path(cursor.recorder_config_hash, cursor.run_id),
            cursor.as_dict(),
            replace=True,
        )

    def _cursor_path(self, recorder_config_hash: str, run_id: str) -> Path:
        digest = recorder_config_hash.removeprefix("sha256:")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ConfigurationError("recorder configuration hash must be a SHA-256 content id")
        return self.state_root / digest / _safe_component(run_id, "run id") / "cursor.json"

    def _filename(self, intent: ProjectionIntent) -> str:
        if intent.kind is ProjectionIntentKind.LIVE_PROGRESS and self.live_coalescing:
            key = content_hash(
                {
                    "recorder_config_hash": intent.recorder_config_hash,
                    "run_id": intent.run_id,
                    "kind": intent.kind.value,
                }
            ).removeprefix("sha256:")
        else:
            key = intent.intent_id.removeprefix("sha256:")
        return f"{key}.json"

    def _live_throttle_path(self, intent: ProjectionIntent) -> Path:
        key = content_hash(
            {
                "recorder_config_hash": intent.recorder_config_hash,
                "run_id": intent.run_id,
                "kind": ProjectionIntentKind.LIVE_PROGRESS.value,
            }
        ).removeprefix("sha256:")
        return self.state_root / "live-throttle" / f"{key}.json"

    def _write_envelope(
        self,
        path: Path,
        intent: ProjectionIntent,
        *,
        attempts: int,
        replace: bool,
    ) -> None:
        _atomic_json(
            path,
            {
                "schema_version": 1,
                "intent": intent.as_dict(),
                "enqueued_at": datetime.now(UTC).isoformat(),
                "attempts": attempts,
                "available_at_epoch": 0.0,
                "lease": None,
                "last_error": None,
            },
            replace=replace,
        )

    def _validate_claim(self, claim: ClaimedProjectionIntent) -> Mapping[str, object]:
        envelope = self._load_envelope(claim.path)
        lease = _mapping(envelope.get("lease"), "projection queue lease")
        if lease.get("lease_id") != claim.lease_id or lease.get("worker_id") != claim.worker_id:
            raise IntegrityError("projection queue lease does not match the claim")
        return envelope

    def _recover_expired_locked(self, now: float) -> None:
        for path in self.leased_root.glob("*.json"):
            envelope = self._load_envelope(path)
            lease = envelope.get("lease")
            if not isinstance(lease, Mapping):
                continue
            expires = float(lease.get("expires_at_epoch", 0.0))
            if expires > now:
                continue
            _atomic_json(
                self.pending_root / path.name,
                {**dict(envelope), "lease": None, "available_at_epoch": now},
                replace=True,
            )
            path.unlink()

    def _load_envelope(self, path: Path, *, required: bool = True) -> Mapping[str, object] | None:
        if not path.is_file() or path.is_symlink():
            if required:
                raise ResolutionError(f"projection queue item does not exist: {path}")
            return None
        return self._load_mapping(path, "projection queue item")

    @staticmethod
    def _load_mapping(path: Path, field: str) -> Mapping[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IntegrityError(f"{field} is invalid: {path}") from error
        if not isinstance(payload, Mapping):
            raise IntegrityError(f"{field} must contain an object: {path}")
        return {str(key): value for key, value in payload.items()}

    def _locked(self):  # type: ignore[no-untyped-def]
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

        class _LockContext:
            def __enter__(self_nonlocal):  # type: ignore[no-untyped-def]
                return handle

            def __exit__(self_nonlocal, *_args):  # type: ignore[no-untyped-def]
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

        return _LockContext()


def _atomic_json(path: Path, payload: Mapping[str, object], *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(payload)
    if path.exists() and not replace:
        existing = path.read_bytes()
        if existing != data:
            raise IntegrityError(f"immutable queue file already contains other data: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix="queue-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IntegrityError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntegrityError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value, "optional string")


def _integer(value: object, field: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise IntegrityError(f"{field} must be an integer >= {minimum}")
    return value


def _safe_component(value: object, field: str) -> str:
    component = _string(value, field)
    if component in {".", ".."} or "/" in component or "\\" in component:
        raise ConfigurationError(f"{field} must be a safe path component")
    return component
