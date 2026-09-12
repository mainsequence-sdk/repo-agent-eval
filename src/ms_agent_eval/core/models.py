from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlparse

from .errors import ConfigurationError
from .hashing import content_hash


_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TAG = re.compile(r"^(?!-)(?!.*(?:\.\.|@\{|//))[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty string")
    return value.strip()


def _identifier(value: object, field: str) -> str:
    result = _string(value, field)
    if _ID.fullmatch(result) is None:
        raise ConfigurationError(f"{field} must be a lowercase identifier")
    return result


def _repository_path(value: object, field: str) -> str:
    text = _string(value, field).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "\x00" in text or ":" in path.parts[0]:
        raise ConfigurationError(f"{field} must be a safe repository-relative POSIX path")
    if path.as_posix() in {"", "."}:
        raise ConfigurationError(f"{field} must identify a repository path")
    return path.as_posix()


class SourceRefKind(str, Enum):
    TAG = "tag"
    COMMIT = "commit"


@dataclass(frozen=True)
class SourceRef:
    type: SourceRefKind
    value: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> SourceRef:
        try:
            kind = SourceRefKind(payload.get("type"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError("source ref type must be tag or commit") from error
        value = _string(payload.get("value"), "source.ref.value")
        if kind is SourceRefKind.COMMIT and _COMMIT.fullmatch(value) is None:
            raise ConfigurationError("commit refs require a full lowercase 40-character SHA")
        if kind is SourceRefKind.TAG and (
            _TAG.fullmatch(value) is None or value.endswith(("/", ".", ".lock"))
        ):
            raise ConfigurationError("tag ref contains invalid or unsafe characters")
        return cls(kind, value)


@dataclass(frozen=True)
class GitSource:
    repository_url: str
    ref: SourceRef
    submodules: bool = False
    git_lfs: bool = False

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> GitSource:
        if payload.get("type") != "github":
            raise ConfigurationError("repository source type must be github")
        repository_url = _string(payload.get("repository_url"), "source.repository_url")
        parsed = urlparse(repository_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError("repository URL must be an HTTPS github.com URL")
        parts = [part for part in PurePosixPath(parsed.path).parts if part != "/"]
        if len(parts) != 2 or not all(
            re.fullmatch(r"[A-Za-z0-9_.-]+", part.removesuffix(".git")) for part in parts
        ):
            raise ConfigurationError("repository URL must identify one owner/repository")
        fetch = _mapping(payload.get("fetch", {}), "source.fetch")
        return cls(
            repository_url.rstrip("/"),
            SourceRef.from_mapping(_mapping(payload.get("ref"), "source.ref")),
            bool(fetch.get("submodules", False)),
            bool(fetch.get("git_lfs", False)),
        )


@dataclass(frozen=True)
class ContextFileSpecification:
    id: str
    source_path: str
    required: bool


@dataclass(frozen=True)
class ExplicitUnitEntry:
    id: str
    source_path: str


@dataclass(frozen=True)
class UnitSourceSpecification:
    id: str
    type: Literal["directory", "explicit"]
    root: str | None
    entries: tuple[ExplicitUnitEntry, ...]


@dataclass(frozen=True)
class InstructionBundleSpecification:
    id: str
    display_name: str
    global_context: tuple[ContextFileSpecification, ...]
    unit_sources: tuple[UnitSourceSpecification, ...]


@dataclass(frozen=True)
class TargetSpecification:
    schema_version: int
    id: str
    display_name: str
    source: GitSource
    instruction_bundles: tuple[InstructionBundleSpecification, ...]

    @property
    def specification_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True)
class LockedFile:
    source_path: str
    snapshot_path: str
    content_hash: str
    size_bytes: int

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> LockedFile:
        size = payload.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ConfigurationError("snapshot file size_bytes must be non-negative")
        return cls(
            _repository_path(payload.get("source_path"), "snapshot file source_path"),
            _repository_path(payload.get("snapshot_path"), "snapshot file snapshot_path"),
            _string(payload.get("content_hash"), "snapshot file content_hash"),
            size,
        )


@dataclass(frozen=True)
class LockedInstructionUnit:
    bundle_id: str
    source_id: str
    unit_id: str
    source_path: str
    snapshot_path: str
    content_hash: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> LockedInstructionUnit:
        return cls(
            _identifier(payload.get("bundle_id"), "unit bundle_id"),
            _identifier(payload.get("source_id"), "unit source_id"),
            _string(payload.get("unit_id"), "unit id"),
            _repository_path(payload.get("source_path"), "unit source_path"),
            _repository_path(payload.get("snapshot_path"), "unit snapshot path"),
            _string(payload.get("content_hash"), "unit content_hash"),
        )


@dataclass(frozen=True)
class SnapshotLock:
    schema_version: int
    id: str
    target_id: str
    repository_url: str
    requested_ref: SourceRef
    resolved_commit: str
    target_specification_hash: str
    extraction_configuration_hash: str
    inventory_hash: str
    files: tuple[LockedFile, ...]
    units: tuple[LockedInstructionUnit, ...]
    content_hash: str

    @classmethod
    def create(
        cls,
        *,
        id: str,
        target_id: str,
        repository_url: str,
        requested_ref: SourceRef,
        resolved_commit: str,
        target_specification_hash: str,
        extraction_configuration_hash: str,
        files: Sequence[LockedFile],
        units: Sequence[LockedInstructionUnit],
    ) -> SnapshotLock:
        if _COMMIT.fullmatch(resolved_commit) is None:
            raise ConfigurationError("resolved commit must be a full lowercase SHA")
        inventory_hash = content_hash({"files": tuple(files), "units": tuple(units)})
        identity = {
            "schema_version": 1,
            "id": _identifier(id, "snapshot id"),
            "target_id": _identifier(target_id, "snapshot target id"),
            "repository_url": _string(repository_url, "snapshot repository URL"),
            "requested_ref": requested_ref,
            "resolved_commit": resolved_commit,
            "target_specification_hash": _string(
                target_specification_hash, "target specification hash"
            ),
            "extraction_configuration_hash": _string(
                extraction_configuration_hash, "extraction configuration hash"
            ),
            "inventory_hash": inventory_hash,
            "files": tuple(files),
            "units": tuple(units),
        }
        return cls(**identity, content_hash=content_hash(identity))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> SnapshotLock:
        if payload.get("schema_version") != 1:
            raise ConfigurationError("internal snapshot schema_version must be 1")
        raw_files = payload.get("files")
        raw_units = payload.get("units")
        if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes)):
            raise ConfigurationError("snapshot files must be a list")
        if not isinstance(raw_units, Sequence) or isinstance(raw_units, (str, bytes)):
            raise ConfigurationError("snapshot units must be a list")
        lock = cls.create(
            id=_identifier(payload.get("id"), "snapshot id"),
            target_id=_identifier(payload.get("target_id"), "snapshot target id"),
            repository_url=_string(payload.get("repository_url"), "snapshot repository URL"),
            requested_ref=SourceRef.from_mapping(
                _mapping(payload.get("requested_ref"), "snapshot requested ref")
            ),
            resolved_commit=_string(payload.get("resolved_commit"), "resolved commit"),
            target_specification_hash=_string(
                payload.get("target_specification_hash"), "target specification hash"
            ),
            extraction_configuration_hash=_string(
                payload.get("extraction_configuration_hash"),
                "extraction configuration hash",
            ),
            files=tuple(
                LockedFile.from_mapping(_mapping(item, "snapshot files[]")) for item in raw_files
            ),
            units=tuple(
                LockedInstructionUnit.from_mapping(_mapping(item, "snapshot units[]"))
                for item in raw_units
            ),
        )
        if payload.get("inventory_hash") != lock.inventory_hash:
            raise ConfigurationError("snapshot inventory hash is invalid")
        if payload.get("content_hash") != lock.content_hash:
            raise ConfigurationError("snapshot content hash is invalid")
        return lock


@dataclass(frozen=True)
class ArtifactReference:
    content_id: str
    media_type: str
    size_bytes: int
    relative_path: str


@dataclass(frozen=True)
class ModelCallRecord:
    call_id: str
    role: Literal["solver", "judge"]
    provider_id: str
    model: str
    parameters: Mapping[str, object]
    rendered_messages: tuple[Mapping[str, object], ...]
    request_artifact: ArtifactReference
    response_artifact: ArtifactReference
    usage: Mapping[str, object]
    status: Literal["completed", "failed"]
    error_kind: str | None
    latency_seconds: float
    configured_cost: float


@dataclass(frozen=True)
class ProgramResult:
    outputs: Mapping[str, object]
    primary_response: str | None
    calls: tuple[ModelCallRecord, ...]
    trace_artifact: ArtifactReference | None
    status: Literal["completed", "failed"]
    error_kind: str | None
