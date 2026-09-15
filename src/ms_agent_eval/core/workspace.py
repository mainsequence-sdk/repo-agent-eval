from __future__ import annotations

import os
import posixpath
import re
from collections import Counter
from string import Formatter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from urllib.parse import urlparse

import yaml

from .errors import ConfigurationError, ResolutionError
from .hashing import content_hash
from .models import (
    ContextFileSpecification,
    ExplicitUnitEntry,
    GitSource,
    InstructionBundleSpecification,
    SourceRef,
    SourceRefKind,
    TargetSpecification,
    UnitSourceSpecification,
)


WORKSPACE_SCHEMA_VERSION = 16
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ENVIRONMENT = re.compile(r"^[A-Z][A-Z0-9_]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_PINNED_IMAGE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
_DOCKER_NETWORK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SPLITS = {"train", "test", "challenge"}
REASONING_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
_REASONING_EFFORTS = frozenset(REASONING_EFFORTS)
_PROGRAM_FIELDS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "solver": {
        "inputs": ("global_context", "skill_context", "task"),
        "outputs": ("response",),
    },
    "judge": {
        "inputs": (
            "task",
            "skill_context",
            "rubric",
            "expected_response",
            "expected_artifacts",
            "candidate_response",
        ),
        "outputs": ("criterion_scores", "hard_failures", "feedback"),
    },
}
_PROGRAM_TYPES: Mapping[str, Mapping[str, Mapping[str, str]]] = {
    "solver": {
        "inputs": {
            "global_context": "str",
            "skill_context": "str",
            "task": "str",
        },
        "outputs": {"response": "str"},
    },
    "judge": {
        "inputs": {
            "task": "str",
            "skill_context": "str",
            "rubric": "str",
            "expected_response": "str",
            "expected_artifacts": "str",
            "candidate_response": "str",
        },
        "outputs": {
            "criterion_scores": "dict[str, float]",
            "hard_failures": "list[str]",
            "feedback": "str",
        },
    },
}


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ConfigurationError(f"{field} must be a list")
    return value


def _reject_unknown(payload: Mapping[str, object], allowed: set[str], field: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ConfigurationError(f"{field} contains unknown fields: {unknown}")


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty string")
    return value.strip()


def _identifier(value: object, field: str) -> str:
    result = _string(value, field)
    if _IDENTIFIER.fullmatch(result) is None:
        raise ConfigurationError(f"{field} must be a lowercase identifier")
    return result


def _environment(value: object, field: str) -> str:
    result = _string(value, field)
    if _ENVIRONMENT.fullmatch(result) is None:
        raise ConfigurationError(f"{field} must be an uppercase environment name")
    return result


def _api_base_url(value: object, field: str) -> str:
    result = _string(value, field).rstrip("/")
    parsed = urlparse(result)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(f"{field} must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(f"{field} must not contain credentials, a query, or a fragment")
    return result


def _relative_path(value: object, field: str) -> str:
    result = _string(value, field)
    path = PurePosixPath(result)
    if path.is_absolute() or ".." in path.parts or "\x00" in result:
        raise ConfigurationError(f"{field} must be a safe relative POSIX path")
    return path.as_posix()


def _source_glob(value: object, field: str) -> str:
    result = _string(value, field).replace("\\", "/")
    path = PurePosixPath(result)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\x00" in result
        or ":" in path.parts[0]
        or path.as_posix() in {"", "."}
    ):
        raise ConfigurationError(f"{field} must be a safe repository-relative glob")
    return path.as_posix()


def _string_list(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ConfigurationError(f"{field} must be a list")
    result = tuple(_string(item, f"{field}[]") for item in value)
    if not result and not allow_empty:
        raise ConfigurationError(f"{field} must not be empty")
    if len(set(result)) != len(result):
        raise ConfigurationError(f"{field} must not contain duplicates")
    return result


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigurationError(f"{field} must be a positive integer")
    return value


def _non_negative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigurationError(f"{field} must be a non-negative integer")
    return value


def _positive_number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
        raise ConfigurationError(f"{field} must be a positive number")
    return float(value)


def load_yaml(path: Path) -> Mapping[str, object]:
    if not path.is_file():
        raise ResolutionError(f"configuration file does not exist: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConfigurationError(f"invalid YAML: {error}", path=path) from error
    if not isinstance(value, Mapping):
        raise ConfigurationError("configuration file must contain a mapping", path=path)
    return {str(key): item for key, item in value.items()}


def validate_external_data_root(candidate: Path, workspace_root: Path) -> Path:
    """Resolve a data root and reject any location inside the workspace's Git repo."""

    root = candidate.expanduser().resolve()
    workspace = workspace_root.resolve()
    if root == workspace or workspace in root.parents:
        raise ConfigurationError("workspace.data_root must be outside the workspace")
    git_root = next(
        (ancestor for ancestor in (workspace, *workspace.parents) if (ancestor / ".git").exists()),
        None,
    )
    if git_root is not None and (root == git_root or git_root in root.parents):
        raise ConfigurationError("workspace.data_root must be outside the Git repository")
    return root


class ExperimentMode(str, Enum):
    EVALUATE = "evaluate"
    OPTIMIZE = "optimize"


class RecordingFailurePolicy(str, Enum):
    CONTINUE = "continue"


class RecordingArtifactPolicy(str, Enum):
    INDEX_ONLY = "index_only"
    FULL = "full"


class RecordingContentPolicy(str, Enum):
    HASHES_ONLY = "hashes_only"
    SUMMARY = "summary"
    FULL = "full"


class OptimizationTracePolicy(str, Enum):
    WINNERS_FAILURES_AND_SAMPLE = "winners_failures_and_sample"
    FULL = "full"
    METADATA_ONLY = "metadata_only"


@dataclass(frozen=True)
class LiveProjectionConfiguration:
    enabled: bool
    minimum_interval_seconds: int

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> LiveProjectionConfiguration:
        _reject_unknown(payload, {"enabled", "minimum_interval_seconds"}, "recording.projection.live")
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ConfigurationError("recording.projection.live.enabled must be a boolean")
        return cls(
            enabled,
            _positive_int(
                payload.get("minimum_interval_seconds"),
                "recording.projection.live.minimum_interval_seconds",
            ),
        )


@dataclass(frozen=True)
class StageBoundaryProjectionConfiguration:
    tables: str
    summaries: bool

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, object]
    ) -> StageBoundaryProjectionConfiguration:
        _reject_unknown(payload, {"tables", "summaries"}, "recording.projection.stage_boundary")
        tables = _identifier(
            payload.get("tables"), "recording.projection.stage_boundary.tables"
        )
        if tables not in {"incremental", "disabled"}:
            raise ConfigurationError(
                "recording.projection.stage_boundary.tables must be incremental or disabled"
            )
        summaries = payload.get("summaries")
        if not isinstance(summaries, bool):
            raise ConfigurationError(
                "recording.projection.stage_boundary.summaries must be a boolean"
            )
        return cls(tables, summaries)


@dataclass(frozen=True)
class FinalProjectionConfiguration:
    reports: bool
    plotly: bool
    evidence: bool

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> FinalProjectionConfiguration:
        _reject_unknown(
            payload,
            {"reports", "plotly", "evidence"},
            "recording.projection.finalization",
        )
        values = []
        for key in ("reports", "plotly", "evidence"):
            value = payload.get(key)
            if not isinstance(value, bool):
                raise ConfigurationError(
                    f"recording.projection.finalization.{key} must be a boolean"
                )
            values.append(value)
        return cls(*values)


@dataclass(frozen=True)
class TraceProjectionConfiguration:
    optimization_policy: OptimizationTracePolicy
    optimization_sample_rate: float
    validation_policy: str
    test_policy: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> TraceProjectionConfiguration:
        _reject_unknown(payload, {"optimization", "validation", "test"}, "recording.projection.traces")
        optimization = _mapping(
            payload.get("optimization"), "recording.projection.traces.optimization"
        )
        _reject_unknown(
            optimization,
            {"policy", "sample_rate"},
            "recording.projection.traces.optimization",
        )
        try:
            optimization_policy = OptimizationTracePolicy(optimization.get("policy"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                "recording.projection.traces.optimization.policy is invalid"
            ) from error
        sample_rate = optimization.get("sample_rate", 0.0)
        if (
            not isinstance(sample_rate, (int, float))
            or isinstance(sample_rate, bool)
            or not 0.0 <= float(sample_rate) <= 1.0
        ):
            raise ConfigurationError(
                "recording.projection.traces.optimization.sample_rate must be between 0 and 1"
            )
        policies = []
        for key in ("validation", "test"):
            item = _mapping(payload.get(key), f"recording.projection.traces.{key}")
            _reject_unknown(item, {"policy"}, f"recording.projection.traces.{key}")
            policy = _identifier(item.get("policy"), f"recording.projection.traces.{key}.policy")
            if policy != "full":
                raise ConfigurationError(
                    f"recording.projection.traces.{key}.policy must be full"
                )
            policies.append(policy)
        return cls(optimization_policy, float(sample_rate), policies[0], policies[1])


@dataclass(frozen=True)
class RecordingProjectionConfiguration:
    live: LiveProjectionConfiguration
    stage_boundary: StageBoundaryProjectionConfiguration
    finalization: FinalProjectionConfiguration
    traces: TraceProjectionConfiguration

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, object]
    ) -> RecordingProjectionConfiguration:
        _reject_unknown(
            payload,
            {"live", "stage_boundary", "finalization", "traces"},
            "recording.projection",
        )
        return cls(
            LiveProjectionConfiguration.from_mapping(
                _mapping(payload.get("live"), "recording.projection.live")
            ),
            StageBoundaryProjectionConfiguration.from_mapping(
                _mapping(
                    payload.get("stage_boundary"),
                    "recording.projection.stage_boundary",
                )
            ),
            FinalProjectionConfiguration.from_mapping(
                _mapping(payload.get("finalization"), "recording.projection.finalization")
            ),
            TraceProjectionConfiguration.from_mapping(
                _mapping(payload.get("traces"), "recording.projection.traces")
            ),
        )


@dataclass(frozen=True)
class RecorderQueueConfiguration:
    id: str
    type: str
    path: str
    live_coalescing: bool
    lease_seconds: int
    max_attempts: int

    @classmethod
    def from_mapping(
        cls, queue_id: str, payload: Mapping[str, object]
    ) -> RecorderQueueConfiguration:
        field = f"recorder_queues.{queue_id}"
        _reject_unknown(
            payload,
            {"type", "path", "live_coalescing", "lease_seconds", "max_attempts"},
            field,
        )
        queue_type = _identifier(payload.get("type"), f"{field}.type")
        if queue_type != "filesystem":
            raise ConfigurationError(f"{field}.type must be filesystem")
        live_coalescing = payload.get("live_coalescing")
        if not isinstance(live_coalescing, bool):
            raise ConfigurationError(f"{field}.live_coalescing must be a boolean")
        return cls(
            _identifier(queue_id, "recorder queue id"),
            queue_type,
            _relative_path(payload.get("path"), f"{field}.path"),
            live_coalescing,
            _positive_int(payload.get("lease_seconds"), f"{field}.lease_seconds"),
            _positive_int(payload.get("max_attempts"), f"{field}.max_attempts"),
        )


@dataclass(frozen=True)
class RecorderAuthenticationConfiguration:
    type: str
    username_env: str | None
    password_env: str | None
    token_env: str | None

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        field: str,
    ) -> RecorderAuthenticationConfiguration:
        _reject_unknown(
            payload,
            {"type", "username_env", "password_env", "token_env"},
            field,
        )
        authentication_type = _identifier(payload.get("type"), f"{field}.type")
        if authentication_type not in {"none", "basic", "bearer"}:
            raise ConfigurationError(f"{field}.type must be none, basic, or bearer")
        username = payload.get("username_env")
        password = payload.get("password_env")
        token = payload.get("token_env")
        if authentication_type == "none":
            if any(value is not None for value in (username, password, token)):
                raise ConfigurationError(f"{field} type none cannot declare credential references")
            return cls("none", None, None, None)
        if authentication_type == "basic":
            if username is None or password is None or token is not None:
                raise ConfigurationError(
                    f"{field} type basic requires username_env and password_env only"
                )
            return cls(
                "basic",
                _environment(username, f"{field}.username_env"),
                _environment(password, f"{field}.password_env"),
                None,
            )
        if token is None or username is not None or password is not None:
            raise ConfigurationError(f"{field} type bearer requires token_env only")
        return cls(
            "bearer",
            None,
            None,
            _environment(token, f"{field}.token_env"),
        )


@dataclass(frozen=True)
class RecorderConfiguration:
    id: str
    type: str
    tracking_uri: str
    experiment_name: str
    authentication: RecorderAuthenticationConfiguration

    @classmethod
    def from_mapping(
        cls,
        recorder_id: str,
        payload: Mapping[str, object],
    ) -> RecorderConfiguration:
        field = f"recorders.{recorder_id}"
        _reject_unknown(
            payload,
            {"type", "tracking_uri", "experiment_name", "authentication"},
            field,
        )
        recorder_type = _identifier(payload.get("type"), f"{field}.type")
        if recorder_type != "mlflow":
            raise ConfigurationError(f"{field}.type must be mlflow")
        experiment_name = _string(payload.get("experiment_name"), f"{field}.experiment_name")
        try:
            parsed_fields = tuple(Formatter().parse(experiment_name))
        except ValueError as error:
            raise ConfigurationError(f"{field}.experiment_name has invalid placeholders") from error
        for _, placeholder, format_spec, conversion in parsed_fields:
            if placeholder is None:
                continue
            if placeholder not in {"workspace_id", "experiment_id"}:
                raise ConfigurationError(
                    f"{field}.experiment_name supports only workspace_id and "
                    "experiment_id placeholders"
                )
            if format_spec or conversion:
                raise ConfigurationError(
                    f"{field}.experiment_name placeholders cannot use conversions or formats"
                )
        return cls(
            _identifier(recorder_id, "recorder id"),
            recorder_type,
            _api_base_url(payload.get("tracking_uri"), f"{field}.tracking_uri"),
            experiment_name,
            RecorderAuthenticationConfiguration.from_mapping(
                _mapping(payload.get("authentication"), f"{field}.authentication"),
                field=f"{field}.authentication",
            ),
        )

    @property
    def configuration_hash(self) -> str:
        """Hash operational destination configuration without resolved secrets."""

        return content_hash(self)

    def resolved_experiment_name(self, workspace_id: str, experiment_id: str) -> str:
        name = self.experiment_name.format(
            workspace_id=workspace_id,
            experiment_id=experiment_id,
        ).strip()
        if not name:
            raise ConfigurationError("resolved recorder experiment name must not be empty")
        return name


@dataclass(frozen=True)
class RecordingConfiguration:
    enabled: bool
    recorder: str
    queue: str
    failure_policy: RecordingFailurePolicy
    wait_on_run_exit: bool
    artifact_policy: RecordingArtifactPolicy
    content_policy: RecordingContentPolicy
    projection: RecordingProjectionConfiguration

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> RecordingConfiguration:
        _reject_unknown(
            payload,
            {
                "enabled",
                "recorder",
                "queue",
                "failure_policy",
                "wait_on_run_exit",
                "artifact_policy",
                "content_policy",
                "projection",
            },
            "recording",
        )
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ConfigurationError("recording.enabled must be a boolean")
        wait_on_run_exit = payload.get("wait_on_run_exit")
        if not isinstance(wait_on_run_exit, bool):
            raise ConfigurationError("recording.wait_on_run_exit must be a boolean")
        try:
            failure_policy = RecordingFailurePolicy(payload.get("failure_policy"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError("recording.failure_policy must be continue") from error
        try:
            artifact_policy = RecordingArtifactPolicy(payload.get("artifact_policy"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                "recording.artifact_policy must be index_only or full"
            ) from error
        try:
            content_policy = RecordingContentPolicy(payload.get("content_policy"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                "recording.content_policy must be hashes_only, summary, or full"
            ) from error
        return cls(
            enabled,
            _identifier(payload.get("recorder"), "recording.recorder"),
            _identifier(payload.get("queue"), "recording.queue"),
            failure_policy,
            wait_on_run_exit,
            artifact_policy,
            content_policy,
            RecordingProjectionConfiguration.from_mapping(
                _mapping(payload.get("projection"), "recording.projection")
            ),
        )


@dataclass(frozen=True)
class RepositoryConfiguration:
    url: str
    ref: str

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        field: str = "evaluation.repository",
    ) -> RepositoryConfiguration:
        _reject_unknown(payload, {"url", "ref"}, field)
        url = _string(payload.get("url"), f"{field}.url")
        ref = _string(payload.get("ref"), f"{field}.ref")
        kind = SourceRefKind.COMMIT if _COMMIT.fullmatch(ref) else SourceRefKind.TAG
        source = GitSource(url, SourceRef(kind, ref))
        # Reuse the strict GitHub URL/ref validation used by snapshot acquisition.
        validated = GitSource.from_mapping(
            {
                "type": "github",
                "repository_url": source.repository_url,
                "ref": {"type": source.ref.type.value, "value": source.ref.value},
            }
        )
        return cls(validated.repository_url, validated.ref.value)

    def to_git_source(self) -> GitSource:
        kind = SourceRefKind.COMMIT if _COMMIT.fullmatch(self.ref) else SourceRefKind.TAG
        return GitSource(self.url, SourceRef(kind, self.ref))


@dataclass(frozen=True)
class AuthoringSourceConfiguration:
    id: str
    role: str
    repository: RepositoryConfiguration
    include: tuple[str, ...]

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        index: int,
    ) -> AuthoringSourceConfiguration:
        field = f"evaluation.case_authoring.sources[{index}]"
        _reject_unknown(payload, {"id", "role", "repository", "include"}, field)
        include = tuple(
            _source_glob(item, f"{field}.include[]")
            for item in _string_list(payload.get("include"), f"{field}.include")
        )
        return cls(
            _identifier(payload.get("id"), f"{field}.id"),
            _identifier(payload.get("role"), f"{field}.role"),
            RepositoryConfiguration.from_mapping(
                _mapping(payload.get("repository"), f"{field}.repository"),
                field=f"{field}.repository",
            ),
            include,
        )


@dataclass(frozen=True)
class SkillSelection:
    directory: str | None
    files: tuple[str, ...]

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        field: str = "evaluation.instructions.skills",
    ) -> SkillSelection:
        _reject_unknown(payload, {"directory", "files"}, field)
        raw_directory = payload.get("directory")
        raw_files = payload.get("files")
        if (raw_directory is None) == (raw_files is None):
            raise ConfigurationError(f"{field} requires exactly one of directory or files")
        if raw_directory is not None:
            return cls(_relative_path(raw_directory, f"{field}.directory"), ())
        files = tuple(
            _relative_path(item, f"{field}.files[]")
            for item in _string_list(raw_files, f"{field}.files")
        )
        if any(PurePosixPath(item).name != "SKILL.md" for item in files):
            raise ConfigurationError(f"every explicit file in {field} must be named SKILL.md")
        return cls(None, files)

    def explicit_entries(self) -> tuple[ExplicitUnitEntry, ...]:
        if not self.files:
            return ()
        parents = [PurePosixPath(item).parent.as_posix() for item in self.files]
        common = PurePosixPath(posixpath.commonpath(parents))
        entries: list[ExplicitUnitEntry] = []
        for item in self.files:
            parent = PurePosixPath(item).parent
            relative = parent.relative_to(common)
            unit_id = relative.as_posix()
            if unit_id == ".":
                unit_id = parent.name
            entries.append(ExplicitUnitEntry(unit_id, item))
        return tuple(entries)


@dataclass(frozen=True)
class SkillImportConfiguration:
    id: str
    source: str
    note: str
    manifest: str
    skills: SkillSelection

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        index: int,
    ) -> SkillImportConfiguration:
        field = f"evaluation.instructions.imports[{index}]"
        _reject_unknown(payload, {"id", "source", "note", "manifest", "skills"}, field)
        return cls(
            _identifier(payload.get("id"), f"{field}.id"),
            _identifier(payload.get("source"), f"{field}.source"),
            _string(payload.get("note"), f"{field}.note"),
            _relative_path(payload.get("manifest"), f"{field}.manifest"),
            SkillSelection.from_mapping(
                _mapping(payload.get("skills"), f"{field}.skills"),
                field=f"{field}.skills",
            ),
        )


@dataclass(frozen=True)
class InstructionConfiguration:
    global_paths: tuple[str, ...]
    skills: SkillSelection
    imports: tuple[SkillImportConfiguration, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> InstructionConfiguration:
        _reject_unknown(payload, {"global", "skills", "imports"}, "evaluation.instructions")
        global_paths = tuple(
            _relative_path(item, "evaluation.instructions.global[]")
            for item in _string_list(payload.get("global", []), "evaluation.instructions.global")
        )
        imports = tuple(
            SkillImportConfiguration.from_mapping(
                _mapping(item, f"evaluation.instructions.imports[{index}]"),
                index=index,
            )
            for index, item in enumerate(
                _sequence(payload.get("imports", []), "evaluation.instructions.imports")
            )
        )
        import_ids = [item.id for item in imports]
        if len(import_ids) != len(set(import_ids)):
            raise ConfigurationError("evaluation.instructions.imports ids must be unique")
        return cls(
            global_paths,
            SkillSelection.from_mapping(
                _mapping(payload.get("skills"), "evaluation.instructions.skills"),
                field="evaluation.instructions.skills",
            ),
            imports,
        )


@dataclass(frozen=True)
class DspyProgramConfiguration:
    module: str
    inputs: Mapping[str, str]
    outputs: Mapping[str, str]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, field: str) -> DspyProgramConfiguration:
        _reject_unknown(payload, {"module", "signature"}, field)
        module = _string(payload.get("module"), f"{field}.module")
        if module != "Predict":
            raise ConfigurationError(f"{field}.module must be Predict")
        signature = _mapping(payload.get("signature"), f"{field}.signature")
        _reject_unknown(signature, {"inputs", "outputs"}, f"{field}.signature")
        inputs = {
            key: _string(value, f"{field}.signature.inputs.{key}")
            for key, value in _mapping(signature.get("inputs"), f"{field}.signature.inputs").items()
        }
        outputs = {
            key: _string(value, f"{field}.signature.outputs.{key}")
            for key, value in _mapping(
                signature.get("outputs"), f"{field}.signature.outputs"
            ).items()
        }
        if not inputs or not outputs:
            raise ConfigurationError(f"{field}.signature inputs and outputs must not be empty")
        return cls(module, MappingProxyType(inputs), MappingProxyType(outputs))

    def validate_role(self, role: str, *, field: str) -> None:
        expected = _PROGRAM_FIELDS[role]
        expected_types = _PROGRAM_TYPES[role]
        # Signature fields are named. YAML/JSON key order is not semantic and
        # canonical revision serialization deliberately sorts mapping keys.
        if (
            dict(self.inputs) != expected_types["inputs"]
            or dict(self.outputs) != expected_types["outputs"]
        ):
            raise ConfigurationError(
                f"{field} must use the canonical {role} DSPy signature; "
                f"inputs={list(expected['inputs'])}, outputs={list(expected['outputs'])}"
            )

    @property
    def content_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True)
class ModelConfiguration:
    deployment: str
    reasoning_effort: str | None
    parameters: Mapping[str, object]
    configured_cost_per_call: float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, field: str) -> ModelConfiguration:
        _reject_unknown(
            payload,
            {
                "deployment",
                "reasoning_effort",
                "parameters",
                "configured_cost_per_call",
            },
            field,
        )
        parameters = MappingProxyType(
            dict(_mapping(payload.get("parameters", {}), f"{field}.parameters"))
        )
        if "reasoning_effort" in parameters:
            raise ConfigurationError(
                f"{field}.reasoning_effort is a first-class model field; remove it from parameters"
            )
        raw_reasoning_effort = payload.get("reasoning_effort")
        reasoning_effort = (
            None
            if raw_reasoning_effort is None
            else _string(raw_reasoning_effort, f"{field}.reasoning_effort")
        )
        if reasoning_effort is not None and reasoning_effort not in _REASONING_EFFORTS:
            raise ConfigurationError(
                f"{field}.reasoning_effort must be one of {sorted(_REASONING_EFFORTS)}"
            )
        cost = payload.get("configured_cost_per_call", 0.0)
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or float(cost) < 0:
            raise ConfigurationError(f"{field}.configured_cost_per_call must be non-negative")
        return cls(
            _identifier(payload.get("deployment"), f"{field}.deployment"),
            reasoning_effort,
            parameters,
            float(cost),
        )


class ExecutorType(str, Enum):
    ENDPOINT = "endpoint"
    HARNESS = "harness"
    PROCESS = "process"


@dataclass(frozen=True)
class CodexControlPlaneConfiguration:
    docker_network: str
    https_proxy: str
    policy_id: str

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, object], *, field: str
    ) -> CodexControlPlaneConfiguration:
        _reject_unknown(
            payload,
            {"docker_network", "https_proxy", "policy_id"},
            field,
        )
        network = _string(payload.get("docker_network"), f"{field}.docker_network")
        if _DOCKER_NETWORK.fullmatch(network) is None or network in {
            "bridge",
            "default",
            "host",
            "none",
        }:
            raise ConfigurationError(
                f"{field}.docker_network must name a dedicated internal proxy network"
            )
        return cls(
            network,
            _api_base_url(payload.get("https_proxy"), f"{field}.https_proxy"),
            _identifier(payload.get("policy_id"), f"{field}.policy_id"),
        )


@dataclass(frozen=True)
class CodexResourceConfiguration:
    cpus: float
    memory_mb: int
    pids: int
    maximum_output_bytes: int

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, object], *, field: str
    ) -> CodexResourceConfiguration:
        _reject_unknown(
            payload,
            {"cpus", "memory_mb", "pids", "maximum_output_bytes"},
            field,
        )
        return cls(
            _positive_number(payload.get("cpus"), f"{field}.cpus"),
            _positive_int(payload.get("memory_mb"), f"{field}.memory_mb"),
            _positive_int(payload.get("pids"), f"{field}.pids"),
            _positive_int(
                payload.get("maximum_output_bytes"),
                f"{field}.maximum_output_bytes",
            ),
        )


@dataclass(frozen=True)
class CodexCliConfiguration:
    image: str
    authentication: str
    permission_profile: str
    instruction_policy: str
    control_plane: CodexControlPlaneConfiguration
    resources: CodexResourceConfiguration

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, field: str) -> CodexCliConfiguration:
        _reject_unknown(
            payload,
            {
                "image",
                "authentication",
                "permission_profile",
                "instruction_policy",
                "control_plane",
                "resources",
            },
            field,
        )
        image = _string(payload.get("image"), f"{field}.image")
        if _DIGEST_PINNED_IMAGE.fullmatch(image) is None:
            raise ConfigurationError(f"{field}.image must be pinned by sha256 digest")
        authentication = _identifier(payload.get("authentication"), f"{field}.authentication")
        if authentication != "chatgpt_subscription":
            raise ConfigurationError(
                f"{field}.authentication must be chatgpt_subscription; "
                "API-key consumption is not supported by codex_cli"
            )
        instruction_policy = _identifier(
            payload.get("instruction_policy"), f"{field}.instruction_policy"
        )
        if instruction_policy not in {"controlled", "none"}:
            raise ConfigurationError(
                f"{field}.instruction_policy must be controlled or none for response-only stages"
            )
        return cls(
            image,
            authentication,
            _identifier(payload.get("permission_profile"), f"{field}.permission_profile"),
            instruction_policy,
            CodexControlPlaneConfiguration.from_mapping(
                _mapping(payload.get("control_plane"), f"{field}.control_plane"),
                field=f"{field}.control_plane",
            ),
            CodexResourceConfiguration.from_mapping(
                _mapping(payload.get("resources"), f"{field}.resources"),
                field=f"{field}.resources",
            ),
        )


@dataclass(frozen=True)
class EndpointRouteConfiguration:
    deployment: str
    model: str

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, object], *, field: str
    ) -> EndpointRouteConfiguration:
        _reject_unknown(payload, {"deployment", "model"}, field)
        return cls(
            _identifier(payload.get("deployment"), f"{field}.deployment"),
            _string(payload.get("model"), f"{field}.model"),
        )


@dataclass(frozen=True)
class EndpointConfiguration:
    id: str
    base_url: str
    max_concurrent_requests: int
    api_key_env: str | None
    routes: tuple[EndpointRouteConfiguration, ...]

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        field: str,
    ) -> EndpointConfiguration:
        _reject_unknown(
            payload,
            {"id", "base_url", "max_concurrent_requests", "api_key_env", "routes"},
            field,
        )
        raw_key = payload.get("api_key_env")
        routes = tuple(
            EndpointRouteConfiguration.from_mapping(
                _mapping(item, f"{field}.routes[{index}]"),
                field=f"{field}.routes[{index}]",
            )
            for index, item in enumerate(_sequence(payload.get("routes"), f"{field}.routes"))
        )
        if not routes:
            raise ConfigurationError(f"{field}.routes must not be empty")
        deployment_ids = [route.deployment for route in routes]
        if len(deployment_ids) != len(set(deployment_ids)):
            raise ConfigurationError(f"{field}.routes deployments must be unique")
        return cls(
            _identifier(payload.get("id"), f"{field}.id"),
            _api_base_url(payload.get("base_url"), f"{field}.base_url"),
            _positive_int(
                payload.get("max_concurrent_requests"),
                f"{field}.max_concurrent_requests",
            ),
            (_environment(raw_key, f"{field}.api_key_env") if raw_key is not None else None),
            routes,
        )

    def route(self, deployment_id: str) -> EndpointRouteConfiguration | None:
        return next((item for item in self.routes if item.deployment == deployment_id), None)

    def api_key(self, environment: Mapping[str, str]) -> str | None:
        if self.api_key_env is None:
            return None
        value = environment.get(self.api_key_env, "").strip()
        if not value:
            raise ResolutionError(
                f"endpoint {self.id!r} requires an API credential in {self.api_key_env}"
            )
        return value

    @property
    def configuration_hash(self) -> str:
        """Hash non-secret endpoint topology and capacity for attempt provenance."""

        return content_hash(self)


@dataclass(frozen=True)
class ExecutorConfiguration:
    id: str
    type: ExecutorType
    adapter: str
    endpoints: tuple[EndpointConfiguration, ...]
    version: str | None
    options: Mapping[str, object]
    codex_cli: CodexCliConfiguration | None

    @classmethod
    def from_mapping(
        cls,
        executor_id: str,
        payload: Mapping[str, object],
    ) -> ExecutorConfiguration:
        field = f"executors.{executor_id}"
        _reject_unknown(
            payload,
            {"type", "adapter", "endpoints", "version", "options"},
            field,
        )
        try:
            executor_type = ExecutorType(payload.get("type"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                f"{field}.type must be endpoint, harness, or process"
            ) from error
        adapter = _identifier(payload.get("adapter"), f"{field}.adapter")
        allowed = {
            ExecutorType.ENDPOINT: {"openai_compatible"},
            ExecutorType.HARNESS: {"artifact_exchange", "codex_cli"},
            ExecutorType.PROCESS: {"local"},
        }
        if adapter not in allowed[executor_type]:
            raise ConfigurationError(
                f"{field}.adapter {adapter!r} is invalid for {executor_type.value} executors"
            )
        raw_endpoints = payload.get("endpoints")
        if executor_type is ExecutorType.ENDPOINT:
            if raw_endpoints is None:
                raise ConfigurationError(f"{field}.endpoints is required for endpoint executors")
            endpoints = tuple(
                EndpointConfiguration.from_mapping(
                    _mapping(item, f"{field}.endpoints[{index}]"),
                    field=f"{field}.endpoints[{index}]",
                )
                for index, item in enumerate(_sequence(raw_endpoints, f"{field}.endpoints"))
            )
            if not endpoints:
                raise ConfigurationError(f"{field}.endpoints must not be empty")
            endpoint_ids = [endpoint.id for endpoint in endpoints]
            if len(endpoint_ids) != len(set(endpoint_ids)):
                raise ConfigurationError(f"{field}.endpoints ids must be unique")
            endpoint_urls = [endpoint.base_url for endpoint in endpoints]
            if len(endpoint_urls) != len(set(endpoint_urls)):
                raise ConfigurationError(f"{field}.endpoints base URLs must be unique")
        else:
            if raw_endpoints is not None:
                raise ConfigurationError(f"{field}.endpoints is only valid for endpoint executors")
            endpoints = ()
        raw_version = payload.get("version")
        version = _string(raw_version, f"{field}.version") if raw_version is not None else None
        options = _mapping(payload.get("options", {}), f"{field}.options")
        codex_cli = None
        if adapter == "codex_cli":
            if version is None:
                raise ConfigurationError(f"{field}.version is required for codex_cli")
            codex_cli = CodexCliConfiguration.from_mapping(options, field=f"{field}.options")
            options = {}
        elif options:
            raise ConfigurationError(f"{field}.options are not supported by adapter {adapter!r}")
        return cls(
            _identifier(executor_id, "executor id"),
            executor_type,
            adapter,
            endpoints,
            version,
            MappingProxyType(dict(options)),
            codex_cli,
        )

    @property
    def total_concurrency(self) -> int:
        return sum(endpoint.max_concurrent_requests for endpoint in self.endpoints)

    @property
    def pool_hash(self) -> str:
        """Hash physical endpoint topology without including credential values."""

        return content_hash(
            {
                "adapter": self.adapter,
                "endpoints": self.endpoints,
            }
        )

    @property
    def execution_hash(self) -> str:
        """Hash output-affecting execution behavior, excluding labels and secrets."""

        identity: dict[str, object] = {
            "type": self.type,
            "adapter": self.adapter,
            "version": self.version,
            "options": self.options,
            "codex_cli": self.codex_cli,
        }
        # Endpoint replicas and capacities are attempt-level infrastructure.
        # Model deployment_revision is the explicit semantic-equivalence attestation.
        return content_hash(identity)


@dataclass(frozen=True)
class DeploymentConfiguration:
    id: str
    executor: str
    model: str
    revision: str

    @classmethod
    def from_mapping(
        cls, deployment_id: str, payload: Mapping[str, object]
    ) -> DeploymentConfiguration:
        field = f"deployments.{deployment_id}"
        _reject_unknown(payload, {"executor", "model", "revision"}, field)
        return cls(
            _identifier(deployment_id, "deployment id"),
            _identifier(payload.get("executor"), f"{field}.executor"),
            _string(payload.get("model"), f"{field}.model"),
            _string(payload.get("revision"), f"{field}.revision"),
        )


_MODEL_STAGES = {"author", "validate_judge", "optimize", "solve", "judge"}
_EXPERIMENT_MODEL_STAGES = {"validate_judge", "optimize", "solve", "judge"}
_CANONICAL_STAGES = _MODEL_STAGES | {"fit", "select", "aggregate"}


class ReusePolicy(str, Enum):
    EXACT = "exact"
    REFRESH = "refresh"
    REPLICATE = "replicate"


@dataclass(frozen=True)
class StageReuseConfiguration:
    policy: ReusePolicy
    samples: int

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        field: str,
        stage_id: str,
    ) -> StageReuseConfiguration:
        _reject_unknown(payload, {"policy", "samples"}, field)
        try:
            policy = ReusePolicy(payload.get("policy"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                f"{field}.policy must be exact, refresh, or replicate"
            ) from error
        raw_samples = payload.get("samples", 1)
        samples = _positive_int(raw_samples, f"{field}.samples")
        if policy is ReusePolicy.REPLICATE:
            if stage_id != "solve":
                raise ConfigurationError(f"{field}.policy replicate is supported only for solve")
            if samples < 2:
                raise ConfigurationError(f"{field}.samples must be at least 2 for replicate")
        elif samples != 1:
            raise ConfigurationError(f"{field}.samples is valid only when policy is replicate")
        return cls(policy, samples)


@dataclass(frozen=True)
class StageConfiguration:
    id: str
    executor: str | None
    model: str | None
    reuse: StageReuseConfiguration
    budget: RoleBudget

    @classmethod
    def from_mapping(
        cls,
        stage_id: str,
        payload: Mapping[str, object],
    ) -> StageConfiguration:
        field = f"stages.{stage_id}"
        if stage_id not in _CANONICAL_STAGES:
            raise ConfigurationError(f"unknown canonical stage: {stage_id}")
        _reject_unknown(payload, {"executor", "model", "reuse", "budget"}, field)
        raw_executor = payload.get("executor")
        executor = (
            _identifier(raw_executor, f"{field}.executor") if raw_executor is not None else None
        )
        raw_model = payload.get("model")
        model = _identifier(raw_model, f"{field}.model") if raw_model is not None else None
        if stage_id == "author" and (model is None or executor is None):
            raise ConfigurationError(f"{field}.executor and model are required")
        if stage_id in _EXPERIMENT_MODEL_STAGES and (model is not None or executor is not None):
            raise ConfigurationError(f"{field} resolves executor and model from experiment.roles")
        if stage_id not in _MODEL_STAGES and model is not None:
            raise ConfigurationError(f"{field}.model is not valid for a deterministic stage")
        if stage_id not in _MODEL_STAGES and executor is None:
            raise ConfigurationError(f"{field}.executor is required")
        return cls(
            stage_id,
            executor,
            model,
            StageReuseConfiguration.from_mapping(
                _mapping(payload.get("reuse"), f"{field}.reuse"),
                field=f"{field}.reuse",
                stage_id=stage_id,
            ),
            RoleBudget.from_mapping(
                _mapping(payload.get("budget"), f"{field}.budget"),
                field=f"{field}.budget",
            ),
        )


@dataclass(frozen=True)
class RoleBudget:
    model_calls: int
    tokens: int
    wall_seconds: float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, field: str) -> RoleBudget:
        _reject_unknown(payload, {"model_calls", "tokens", "wall_seconds"}, field)
        return cls(
            _non_negative_int(payload.get("model_calls"), f"{field}.model_calls"),
            _non_negative_int(payload.get("tokens"), f"{field}.tokens"),
            _positive_number(payload.get("wall_seconds", 1800), f"{field}.wall_seconds"),
        )


@dataclass(frozen=True)
class CaseAuthoringConfiguration:
    instructions: str
    sources: tuple[AuthoringSourceConfiguration, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> CaseAuthoringConfiguration:
        _reject_unknown(
            payload,
            {"instructions", "sources"},
            "evaluation.case_authoring",
        )
        raw_sources = payload.get("sources", [])
        if not isinstance(raw_sources, Sequence) or isinstance(
            raw_sources, (str, bytes, bytearray)
        ):
            raise ConfigurationError("evaluation.case_authoring.sources must be a list")
        sources = tuple(
            AuthoringSourceConfiguration.from_mapping(
                _mapping(item, f"evaluation.case_authoring.sources[{index}]"),
                index=index,
            )
            for index, item in enumerate(raw_sources)
        )
        source_ids = [item.id for item in sources]
        if len(source_ids) != len(set(source_ids)):
            raise ConfigurationError("evaluation.case_authoring.sources must use unique source ids")
        if "target" in source_ids:
            raise ConfigurationError(
                "evaluation.case_authoring.sources reserves the source id 'target'"
            )
        return cls(
            _relative_path(payload.get("instructions"), "evaluation.case_authoring.instructions"),
            sources,
        )


@dataclass(frozen=True)
class JudgeConfiguration:
    program: DspyProgramConfiguration
    validation_directory: str
    repetitions: int

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> JudgeConfiguration:
        _reject_unknown(payload, {"dspy", "validation", "repetitions"}, "evaluation.judge")
        validation = _mapping(payload.get("validation"), "evaluation.judge.validation")
        _reject_unknown(validation, {"directory"}, "evaluation.judge.validation")
        return cls(
            DspyProgramConfiguration.from_mapping(
                _mapping(payload.get("dspy"), "evaluation.judge.dspy"),
                field="evaluation.judge.dspy",
            ),
            _relative_path(validation.get("directory"), "evaluation.judge.validation.directory"),
            _positive_int(payload.get("repetitions", 1), "evaluation.judge.repetitions"),
        )


@dataclass(frozen=True)
class CaseCoverageConfiguration:
    catalog: str
    minimum_cases_per_skill: int
    minimum_leakage_groups_per_skill: int

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> CaseCoverageConfiguration:
        _reject_unknown(
            payload,
            {"catalog", "minimum_cases_per_skill", "minimum_leakage_groups_per_skill"},
            "evaluation.cases.coverage",
        )
        catalog = _relative_path(payload.get("catalog"), "evaluation.cases.coverage.catalog")
        minimum_cases = _positive_int(
            payload.get("minimum_cases_per_skill", 8),
            "evaluation.cases.coverage.minimum_cases_per_skill",
        )
        minimum_groups = _positive_int(
            payload.get("minimum_leakage_groups_per_skill", 4),
            "evaluation.cases.coverage.minimum_leakage_groups_per_skill",
        )
        if minimum_groups > minimum_cases:
            raise ConfigurationError(
                "evaluation.cases.coverage.minimum_leakage_groups_per_skill "
                "cannot exceed minimum_cases_per_skill"
            )
        return cls(catalog, minimum_cases, minimum_groups)


@dataclass(frozen=True)
class CasesConfiguration:
    directory: str
    coverage: CaseCoverageConfiguration

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> CasesConfiguration:
        _reject_unknown(payload, {"directory", "coverage"}, "evaluation.cases")
        return cls(
            _relative_path(payload.get("directory"), "evaluation.cases.directory"),
            CaseCoverageConfiguration.from_mapping(
                _mapping(payload.get("coverage"), "evaluation.cases.coverage")
            ),
        )


@dataclass(frozen=True)
class SplitPolicy:
    seed: str
    train: float
    test: float
    challenge: float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> SplitPolicy:
        _reject_unknown(
            payload,
            {"seed", "train", "test", "challenge"},
            "evaluation.splits.policy",
        )
        seed = _string(payload.get("seed", "ms-agent-eval"), "splits.policy.seed")
        values: dict[str, float] = {}
        for split in sorted(_SPLITS):
            raw = payload.get(split, 0.0)
            if not isinstance(raw, (int, float)) or isinstance(raw, bool) or float(raw) < 0:
                raise ConfigurationError(f"splits.policy.{split} must be non-negative")
            values[split] = float(raw)
        if abs(sum(values.values()) - 1.0) > 1e-9:
            raise ConfigurationError("split policy proportions must sum to 1.0")
        return cls(seed, values["train"], values["test"], values["challenge"])


@dataclass(frozen=True)
class SplitsConfiguration:
    file: str
    policy: SplitPolicy

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> SplitsConfiguration:
        _reject_unknown(payload, {"file", "policy"}, "evaluation.splits")
        default_policy = {
            "seed": "ms-agent-eval",
            "train": 0.7,
            "test": 0.2,
            "challenge": 0.1,
        }
        return cls(
            _relative_path(payload.get("file"), "evaluation.splits.file"),
            SplitPolicy.from_mapping(
                _mapping(payload.get("policy", default_policy), "evaluation.splits.policy")
            ),
        )


@dataclass(frozen=True)
class EvaluationConfiguration:
    repository: RepositoryConfiguration
    instructions: InstructionConfiguration
    case_authoring: CaseAuthoringConfiguration
    cases: CasesConfiguration
    splits: SplitsConfiguration
    judge: JudgeConfiguration

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> EvaluationConfiguration:
        _reject_unknown(
            payload,
            {"repository", "instructions", "case_authoring", "cases", "splits", "judge"},
            "evaluation",
        )
        return cls(
            RepositoryConfiguration.from_mapping(
                _mapping(payload.get("repository"), "evaluation.repository")
            ),
            InstructionConfiguration.from_mapping(
                _mapping(payload.get("instructions"), "evaluation.instructions")
            ),
            CaseAuthoringConfiguration.from_mapping(
                _mapping(payload.get("case_authoring"), "evaluation.case_authoring")
            ),
            CasesConfiguration.from_mapping(_mapping(payload.get("cases"), "evaluation.cases")),
            SplitsConfiguration.from_mapping(_mapping(payload.get("splits"), "evaluation.splits")),
            JudgeConfiguration.from_mapping(_mapping(payload.get("judge"), "evaluation.judge")),
        )


@dataclass(frozen=True)
class SolverConfiguration:
    program: DspyProgramConfiguration

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> SolverConfiguration:
        _reject_unknown(payload, {"dspy"}, "experiment.solver")
        return cls(
            DspyProgramConfiguration.from_mapping(
                _mapping(payload.get("dspy"), "experiment.solver.dspy"),
                field="experiment.solver.dspy",
            ),
        )


@dataclass(frozen=True)
class RuntimeConfiguration:
    type: str
    python: str
    image: str | None
    network: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> RuntimeConfiguration:
        _reject_unknown(payload, {"type", "python", "image", "network"}, "experiment.runtime")
        runtime_type = _string(payload.get("type"), "experiment.runtime.type")
        if runtime_type not in {"response_only", "docker"}:
            raise ConfigurationError("runtime.type must be response_only or docker")
        image = payload.get("image")
        if runtime_type == "docker" and not isinstance(image, str):
            raise ConfigurationError("docker runtime requires a digest-pinned image")
        if isinstance(image, str) and "@sha256:" not in image:
            raise ConfigurationError("runtime image must be digest-pinned")
        network = _string(payload.get("network", "none"), "experiment.runtime.network")
        if network not in {"none", "restricted"}:
            raise ConfigurationError("runtime.network must be none or restricted")
        python = _string(payload.get("python", "3.12"), "experiment.runtime.python")
        version = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", python)
        if version is None or tuple(map(int, version.groups()[:2])) < (3, 12):
            raise ConfigurationError("runtime.python must be Python 3.12 or newer")
        return cls(
            runtime_type,
            python,
            image,
            network,
        )


@dataclass(frozen=True)
class LabeledFewShotConfiguration:
    strategy: str
    k_values: tuple[int, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> LabeledFewShotConfiguration:
        _reject_unknown(payload, {"strategy", "search"}, "experiment.optimizer")
        search = _mapping(payload.get("search"), "experiment.optimizer.search")
        _reject_unknown(search, {"k"}, "experiment.optimizer.search")
        raw_values = search.get("k")
        if not isinstance(raw_values, Sequence) or isinstance(raw_values, (str, bytes, bytearray)):
            raise ConfigurationError("experiment.optimizer.search.k must be a list")
        values = tuple(raw_values)
        if not values or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in values
        ):
            raise ConfigurationError("experiment.optimizer.search.k must contain positive integers")
        if len(values) != len(set(values)):
            raise ConfigurationError("experiment.optimizer.search.k must not contain duplicates")
        return cls("labeled_few_shot", tuple(sorted(values)))


@dataclass(frozen=True)
class CoproConfiguration:
    strategy: str
    optimize_prompt: bool
    skills: str | tuple[str, ...]
    breadth: int
    depth: int
    partitions: int
    init_temperature: float
    metric: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> CoproConfiguration:
        _reject_unknown(
            payload,
            {"strategy", "targets", "search", "metric"},
            "experiment.optimizer",
        )
        targets = _mapping(payload.get("targets"), "experiment.optimizer.targets")
        _reject_unknown(targets, {"prompt", "skills"}, "experiment.optimizer.targets")
        optimize_prompt = targets.get("prompt")
        if not isinstance(optimize_prompt, bool):
            raise ConfigurationError("experiment.optimizer.targets.prompt must be a boolean")
        raw_skills = targets.get("skills")
        if raw_skills == "all":
            skills: str | tuple[str, ...] = "all"
        else:
            skills = _string_list(
                raw_skills,
                "experiment.optimizer.targets.skills",
                allow_empty=optimize_prompt,
            )
            if any("*" in item or "?" in item or "[" in item for item in skills):
                raise ConfigurationError(
                    "experiment.optimizer.targets.skills must contain exact skill identifiers, not globs"
                )
        if not optimize_prompt and skills == ():
            raise ConfigurationError("COPRO must optimize the prompt or at least one skill")

        search = _mapping(payload.get("search"), "experiment.optimizer.search")
        _reject_unknown(
            search,
            {"breadth", "depth", "partitions", "init_temperature"},
            "experiment.optimizer.search",
        )
        breadth = _positive_int(search.get("breadth"), "experiment.optimizer.search.breadth")
        if breadth < 2:
            raise ConfigurationError("experiment.optimizer.search.breadth must be at least 2")
        depth = _positive_int(search.get("depth"), "experiment.optimizer.search.depth")
        partitions = _positive_int(
            search.get("partitions", 1),
            "experiment.optimizer.search.partitions",
        )
        temperature = _positive_number(
            search.get("init_temperature"),
            "experiment.optimizer.search.init_temperature",
        )

        metric = _mapping(payload.get("metric"), "experiment.optimizer.metric")
        _reject_unknown(metric, {"name"}, "experiment.optimizer.metric")
        metric_name = _identifier(metric.get("name"), "experiment.optimizer.metric.name")
        if metric_name != "rubric_mean_score":
            raise ConfigurationError("experiment.optimizer.metric.name must be rubric_mean_score")
        return cls(
            "copro",
            optimize_prompt,
            skills,
            breadth,
            depth,
            partitions,
            temperature,
            metric_name,
        )


OptimizerConfiguration = LabeledFewShotConfiguration | CoproConfiguration


def _optimizer_configuration(payload: Mapping[str, object]) -> OptimizerConfiguration:
    strategy = payload.get("strategy")
    if strategy is None and "name" in payload:
        raise ConfigurationError(
            "experiment.optimizer.name was removed; use optimizer.strategy with "
            "labeled_few_shot or copro"
        )
    if strategy == "labeled_few_shot":
        return LabeledFewShotConfiguration.from_mapping(payload)
    if strategy == "copro":
        return CoproConfiguration.from_mapping(payload)
    raise ConfigurationError("experiment.optimizer.strategy must be labeled_few_shot or copro")


@dataclass(frozen=True)
class ValidationConfiguration:
    strategy: str
    fraction: float
    seed: int

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ValidationConfiguration:
        _reject_unknown(payload, {"strategy", "fraction", "seed"}, "experiment.validation")
        strategy = _identifier(payload.get("strategy"), "experiment.validation.strategy")
        if strategy != "holdout":
            raise ConfigurationError("experiment.validation.strategy must be holdout")
        fraction = _positive_number(payload.get("fraction"), "experiment.validation.fraction")
        if fraction >= 1:
            raise ConfigurationError("experiment.validation.fraction must be less than 1")
        seed = payload.get("seed", 0)
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ConfigurationError("experiment.validation.seed must be an integer")
        return cls(strategy, fraction, seed)


@dataclass(frozen=True)
class OptimizationBudget:
    optimizer: RoleBudget | None
    solver: RoleBudget
    judge: RoleBudget
    wall_seconds: float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> OptimizationBudget:
        _reject_unknown(
            payload,
            {"optimizer", "solver", "judge", "wall_seconds"},
            "experiment.budget",
        )
        raw_optimizer = payload.get("optimizer")
        return cls(
            (
                RoleBudget.from_mapping(
                    _mapping(raw_optimizer, "experiment.budget.optimizer"),
                    field="experiment.budget.optimizer",
                )
                if raw_optimizer is not None
                else None
            ),
            RoleBudget.from_mapping(
                _mapping(payload.get("solver"), "experiment.budget.solver"),
                field="experiment.budget.solver",
            ),
            RoleBudget.from_mapping(
                _mapping(payload.get("judge"), "experiment.budget.judge"),
                field="experiment.budget.judge",
            ),
            _positive_number(payload.get("wall_seconds"), "experiment.budget.wall_seconds"),
        )


@dataclass(frozen=True)
class ExperimentRoles:
    solver: str
    judge: str
    optimizer: str | None

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        field: str,
        requires_optimizer: bool,
    ) -> ExperimentRoles:
        _reject_unknown(payload, {"solver", "optimizer", "judge"}, field)
        raw_optimizer = payload.get("optimizer")
        optimizer = (
            _identifier(raw_optimizer, f"{field}.optimizer") if raw_optimizer is not None else None
        )
        if requires_optimizer and optimizer is None:
            raise ConfigurationError(f"{field}.optimizer is required for COPRO")
        if not requires_optimizer and optimizer is not None:
            raise ConfigurationError(f"{field}.optimizer is valid only for COPRO")
        return cls(
            _identifier(payload.get("solver"), f"{field}.solver"),
            _identifier(payload.get("judge"), f"{field}.judge"),
            optimizer,
        )


@dataclass(frozen=True)
class ExperimentConfiguration:
    id: str
    mode: ExperimentMode
    roles: ExperimentRoles
    solver: SolverConfiguration | None
    based_on: str | None
    runtime: RuntimeConfiguration | None
    repetitions: int
    optimizer: OptimizerConfiguration | None
    validation: ValidationConfiguration | None
    budget: OptimizationBudget | None

    @classmethod
    def from_mapping(
        cls, experiment_id: str, payload: Mapping[str, object]
    ) -> ExperimentConfiguration:
        try:
            mode = ExperimentMode(payload.get("mode"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError("experiment.mode must be evaluate or optimize") from error
        if mode is ExperimentMode.EVALUATE:
            _reject_unknown(
                payload,
                {"mode", "roles", "solver", "runtime", "repetitions"},
                f"experiments.{experiment_id}",
            )
            if payload.get("based_on") is not None or payload.get("optimizer") is not None:
                raise ConfigurationError("evaluate experiments cannot declare optimization fields")
            return cls(
                _identifier(experiment_id, "experiment id"),
                mode,
                ExperimentRoles.from_mapping(
                    _mapping(payload.get("roles"), f"experiments.{experiment_id}.roles"),
                    field=f"experiments.{experiment_id}.roles",
                    requires_optimizer=False,
                ),
                SolverConfiguration.from_mapping(
                    _mapping(payload.get("solver"), f"experiments.{experiment_id}.solver")
                ),
                None,
                RuntimeConfiguration.from_mapping(
                    _mapping(payload.get("runtime"), f"experiments.{experiment_id}.runtime")
                ),
                _positive_int(payload.get("repetitions", 1), "experiment.repetitions"),
                None,
                None,
                None,
            )
        _reject_unknown(
            payload,
            {"mode", "roles", "based_on", "optimizer", "validation", "budget"},
            f"experiments.{experiment_id}",
        )
        optimizer_configuration = _optimizer_configuration(
            _mapping(payload.get("optimizer"), "experiment.optimizer")
        )
        return cls(
            _identifier(experiment_id, "experiment id"),
            mode,
            ExperimentRoles.from_mapping(
                _mapping(payload.get("roles"), f"experiments.{experiment_id}.roles"),
                field=f"experiments.{experiment_id}.roles",
                requires_optimizer=isinstance(optimizer_configuration, CoproConfiguration),
            ),
            None,
            _identifier(payload.get("based_on"), "experiment.based_on"),
            None,
            1,
            optimizer_configuration,
            ValidationConfiguration.from_mapping(
                _mapping(payload.get("validation"), "experiment.validation")
            ),
            OptimizationBudget.from_mapping(_mapping(payload.get("budget"), "experiment.budget")),
        )


@dataclass(frozen=True)
class ComparisonConfiguration:
    id: str
    baseline_experiments: tuple[str, ...]
    optimization_experiments: tuple[str, ...]
    primary_metric: str
    grouping: tuple[str, ...]

    @classmethod
    def from_mapping(
        cls, comparison_id: str, payload: Mapping[str, object]
    ) -> ComparisonConfiguration:
        field = f"comparisons.{comparison_id}"
        _reject_unknown(
            payload,
            {"baseline_experiments", "optimization_experiments", "primary_metric", "grouping"},
            field,
        )
        baselines = tuple(
            _identifier(item, f"{field}.baseline_experiments")
            for item in _sequence(
                payload.get("baseline_experiments"), f"{field}.baseline_experiments"
            )
        )
        optimizations = tuple(
            _identifier(item, f"{field}.optimization_experiments")
            for item in _sequence(
                payload.get("optimization_experiments"),
                f"{field}.optimization_experiments",
            )
        )
        if not baselines or not optimizations:
            raise ConfigurationError(f"{field} requires baseline and optimization experiments")
        if len(set((*baselines, *optimizations))) != len((*baselines, *optimizations)):
            raise ConfigurationError(f"{field} experiment ids must be unique")
        grouping = tuple(
            _identifier(item, f"{field}.grouping")
            for item in _sequence(
                payload.get("grouping", ["solver", "optimizer"]), f"{field}.grouping"
            )
        )
        if not grouping or not set(grouping) <= {"solver", "optimizer"}:
            raise ConfigurationError(f"{field}.grouping supports only solver and optimizer")
        if len(grouping) != len(set(grouping)):
            raise ConfigurationError(f"{field}.grouping values must be unique")
        primary_metric = _string(
            payload.get("primary_metric", "score.mean"),
            f"{field}.primary_metric",
        )
        if primary_metric != "score.mean":
            raise ConfigurationError(
                f"{field}.primary_metric supports only score.mean in schema 16"
            )
        return cls(
            _identifier(comparison_id, "comparison id"),
            baselines,
            optimizations,
            primary_metric,
            grouping,
        )


@dataclass(frozen=True)
class WorkspaceConfiguration:
    schema_version: int
    id: str
    data_root: str | None
    recording: RecordingConfiguration
    recorders: Mapping[str, RecorderConfiguration]
    recorder_queues: Mapping[str, RecorderQueueConfiguration]
    executors: Mapping[str, ExecutorConfiguration]
    deployments: Mapping[str, DeploymentConfiguration]
    models: Mapping[str, ModelConfiguration]
    stages: Mapping[str, StageConfiguration]
    evaluation: EvaluationConfiguration
    experiments: Mapping[str, ExperimentConfiguration]
    comparisons: Mapping[str, ComparisonConfiguration]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> WorkspaceConfiguration:
        _reject_unknown(
            payload,
            {
                "schema_version",
                "workspace",
                "recording",
                "recorders",
                "recorder_queues",
                "executors",
                "deployments",
                "models",
                "stages",
                "evaluation",
                "experiments",
                "comparisons",
            },
            "workspace",
        )
        if payload.get("schema_version") != WORKSPACE_SCHEMA_VERSION:
            raise ConfigurationError(
                f"workspace schema_version must be {WORKSPACE_SCHEMA_VERSION}; no legacy schemas are supported"
            )
        workspace = _mapping(payload.get("workspace"), "workspace")
        _reject_unknown(workspace, {"id", "data_root"}, "workspace.workspace")
        raw_data_root = workspace.get("data_root")
        data_root = (
            _string(raw_data_root, "workspace.data_root") if raw_data_root is not None else None
        )
        recording = RecordingConfiguration.from_mapping(
            _mapping(payload.get("recording"), "recording")
        )
        recorders_payload = _mapping(payload.get("recorders"), "recorders")
        recorders = {
            recorder_id: RecorderConfiguration.from_mapping(
                recorder_id, _mapping(item, f"recorders.{recorder_id}")
            )
            for recorder_id, item in recorders_payload.items()
        }
        if recording.recorder not in recorders:
            raise ConfigurationError(
                f"recording references unknown recorder {recording.recorder!r}"
            )
        queues_payload = _mapping(payload.get("recorder_queues"), "recorder_queues")
        recorder_queues = {
            queue_id: RecorderQueueConfiguration.from_mapping(
                queue_id, _mapping(item, f"recorder_queues.{queue_id}")
            )
            for queue_id, item in queues_payload.items()
        }
        if recording.queue not in recorder_queues:
            raise ConfigurationError(
                f"recording references unknown recorder queue {recording.queue!r}"
            )
        experiments_payload = _mapping(payload.get("experiments"), "experiments")
        experiments = {
            experiment_id: ExperimentConfiguration.from_mapping(
                experiment_id, _mapping(item, f"experiments.{experiment_id}")
            )
            for experiment_id, item in experiments_payload.items()
        }
        if not experiments:
            raise ConfigurationError("workspace requires at least one experiment")
        executors_payload = _mapping(payload.get("executors"), "executors")
        executors = {
            executor_id: ExecutorConfiguration.from_mapping(
                executor_id, _mapping(item, f"executors.{executor_id}")
            )
            for executor_id, item in executors_payload.items()
        }
        if not executors:
            raise ConfigurationError("workspace requires at least one executor")
        deployments_payload = _mapping(payload.get("deployments"), "deployments")
        deployments = {
            deployment_id: DeploymentConfiguration.from_mapping(
                deployment_id, _mapping(item, f"deployments.{deployment_id}")
            )
            for deployment_id, item in deployments_payload.items()
        }
        if not deployments:
            raise ConfigurationError("workspace requires at least one deployment")
        models_payload = _mapping(payload.get("models"), "models")
        models = {
            model_id: ModelConfiguration.from_mapping(
                _mapping(item, f"models.{model_id}"), field=f"models.{model_id}"
            )
            for model_id, item in models_payload.items()
        }
        if not models:
            raise ConfigurationError("workspace requires at least one model")
        stages_payload = _mapping(payload.get("stages"), "stages")
        if set(stages_payload) != _CANONICAL_STAGES:
            raise ConfigurationError(
                f"stages must configure exactly the canonical stages: {sorted(_CANONICAL_STAGES)}"
            )
        stages = {
            stage_id: StageConfiguration.from_mapping(
                stage_id, _mapping(item, f"stages.{stage_id}")
            )
            for stage_id, item in stages_payload.items()
        }
        for executor in executors.values():
            for endpoint in executor.endpoints:
                for route in endpoint.routes:
                    deployment = deployments.get(route.deployment)
                    if deployment is None:
                        raise ConfigurationError(
                            f"endpoint {endpoint.id!r} references unknown deployment {route.deployment!r}"
                        )
                    if deployment.executor != executor.id:
                        raise ConfigurationError(
                            f"endpoint {endpoint.id!r} route for deployment {route.deployment!r} "
                            f"belongs to executor {deployment.executor!r}"
                        )
        for deployment in deployments.values():
            if deployment.executor not in executors:
                raise ConfigurationError(
                    f"deployment {deployment.id!r} references unknown executor {deployment.executor!r}"
                )
            executor = executors[deployment.executor]
            if executor.type is ExecutorType.ENDPOINT and not any(
                endpoint.route(deployment.id) is not None for endpoint in executor.endpoints
            ):
                raise ConfigurationError(
                    f"deployment {deployment.id!r} has no eligible endpoint route"
                )
        for model_id, model in models.items():
            if model.deployment not in deployments:
                raise ConfigurationError(
                    f"model {model_id!r} references unknown deployment {model.deployment!r}"
                )
        for stage in stages.values():
            if stage.executor is not None and stage.executor not in executors:
                raise ConfigurationError(
                    f"stage {stage.id!r} references unknown executor {stage.executor!r}"
                )
            if stage.model is not None and stage.model not in models:
                raise ConfigurationError(
                    f"stage {stage.id!r} references unknown model {stage.model!r}"
                )
            if stage.executor is None:
                continue
            executor = executors[stage.executor]
            if stage.id in _MODEL_STAGES and executor.type is ExecutorType.PROCESS:
                raise ConfigurationError(
                    f"model-driven stage {stage.id!r} cannot use a process executor"
                )
            if stage.id not in _MODEL_STAGES and executor.type is ExecutorType.ENDPOINT:
                raise ConfigurationError(
                    f"deterministic stage {stage.id!r} cannot use an LLM endpoint executor"
                )
            if stage.id not in _MODEL_STAGES and executor.adapter == "codex_cli":
                raise ConfigurationError(
                    f"deterministic stage {stage.id!r} cannot use codex_cli; "
                    "use local or artifact_exchange"
                )
        for experiment in experiments.values():
            for role, model_id in (
                ("solver", experiment.roles.solver),
                ("judge", experiment.roles.judge),
                ("optimizer", experiment.roles.optimizer),
            ):
                if model_id is not None and model_id not in models:
                    raise ConfigurationError(
                        f"experiment {experiment.id!r} {role} role references unknown model {model_id!r}"
                    )
            if experiment.mode is ExperimentMode.OPTIMIZE:
                base = experiments.get(experiment.based_on or "")
                if base is None or base.mode is not ExperimentMode.EVALUATE:
                    raise ConfigurationError(
                        f"optimization experiment {experiment.id!r} must reference an evaluate experiment"
                    )
                assert experiment.optimizer is not None and experiment.budget is not None
                assert base is not None
                if experiment.roles.solver != base.roles.solver:
                    raise ConfigurationError(
                        f"optimization experiment {experiment.id!r} must use the solver from "
                        f"baseline {base.id!r}"
                    )
                if experiment.roles.judge != base.roles.judge:
                    raise ConfigurationError(
                        f"optimization experiment {experiment.id!r} must use the judge from "
                        f"baseline {base.id!r}"
                    )
                if isinstance(experiment.optimizer, CoproConfiguration):
                    if experiment.budget.optimizer is None:
                        raise ConfigurationError(
                            f"COPRO experiment {experiment.id!r} requires budget.optimizer"
                        )
                elif experiment.budget.optimizer is not None:
                    raise ConfigurationError(
                        f"labeled_few_shot experiment {experiment.id!r} cannot declare budget.optimizer"
                    )
        comparisons_payload = _mapping(payload.get("comparisons", {}), "comparisons")
        comparisons = {
            comparison_id: ComparisonConfiguration.from_mapping(
                comparison_id, _mapping(item, f"comparisons.{comparison_id}")
            )
            for comparison_id, item in comparisons_payload.items()
        }
        for comparison in comparisons.values():
            for experiment_id in comparison.baseline_experiments:
                experiment = experiments.get(experiment_id)
                if experiment is None or experiment.mode is not ExperimentMode.EVALUATE:
                    raise ConfigurationError(
                        f"comparison {comparison.id!r} baseline {experiment_id!r} must be an evaluate experiment"
                    )
            for experiment_id in comparison.optimization_experiments:
                experiment = experiments.get(experiment_id)
                if experiment is None or experiment.mode is not ExperimentMode.OPTIMIZE:
                    raise ConfigurationError(
                        f"comparison {comparison.id!r} optimization {experiment_id!r} must be an optimize experiment"
                    )
                if experiment.based_on not in comparison.baseline_experiments:
                    raise ConfigurationError(
                        f"comparison {comparison.id!r} optimization {experiment_id!r} is based on "
                        f"{experiment.based_on!r}, which is not one of its baseline experiments"
                    )
            comparison_experiments = tuple(
                experiments[experiment_id]
                for experiment_id in (
                    *comparison.baseline_experiments,
                    *comparison.optimization_experiments,
                )
            )
            judge_profiles = {item.roles.judge for item in comparison_experiments}
            if len(judge_profiles) != 1:
                raise ConfigurationError(
                    f"comparison {comparison.id!r} must use one fixed judge profile"
                )
        comparison_memberships = Counter(
            experiment_id
            for comparison in comparisons.values()
            for experiment_id in (
                *comparison.baseline_experiments,
                *comparison.optimization_experiments,
            )
        )
        duplicated_memberships = sorted(
            experiment_id for experiment_id, count in comparison_memberships.items() if count > 1
        )
        if duplicated_memberships:
            raise ConfigurationError(
                "an experiment may belong to only one comparison in schema 16: "
                f"{duplicated_memberships}"
            )
        result = cls(
            WORKSPACE_SCHEMA_VERSION,
            _identifier(workspace.get("id"), "workspace.id"),
            data_root,
            recording,
            MappingProxyType(recorders),
            MappingProxyType(recorder_queues),
            MappingProxyType(executors),
            MappingProxyType(deployments),
            MappingProxyType(models),
            MappingProxyType(stages),
            EvaluationConfiguration.from_mapping(_mapping(payload.get("evaluation"), "evaluation")),
            MappingProxyType(experiments),
            MappingProxyType(comparisons),
        )
        result.evaluation.judge.program.validate_role("judge", field="evaluation.judge.dspy")
        for experiment in result.experiments.values():
            if experiment.mode is ExperimentMode.EVALUATE:
                assert experiment.solver is not None
                experiment.solver.program.validate_role(
                    "solver", field=f"experiments.{experiment.id}.solver.dspy"
                )
        return result

    @property
    def identity_hash(self) -> str:
        return content_hash(
            {
                "schema_version": self.schema_version,
                "id": self.id,
                # Recorder selection and destination are operational projection
                # concerns. They must not invalidate experiment work or reuse.
                "executors": self.executors,
                "deployments": self.deployments,
                "models": self.models,
                "stages": self.stages,
                "evaluation": self.evaluation,
                "experiments": self.experiments,
                "comparisons": self.comparisons,
            }
        )

    def solver(self, experiment_id: str) -> SolverConfiguration:
        experiment = self.experiments.get(experiment_id)
        if experiment is None:
            raise ResolutionError(f"unknown experiment: {experiment_id}")
        if experiment.mode is ExperimentMode.OPTIMIZE:
            base = self.experiments[experiment.based_on or ""]
            assert base.solver is not None
            return base.solver
        assert experiment.solver is not None
        return experiment.solver

    def runtime(self, experiment_id: str) -> RuntimeConfiguration:
        experiment = self.experiments.get(experiment_id)
        if experiment is None:
            raise ResolutionError(f"unknown experiment: {experiment_id}")
        if experiment.mode is ExperimentMode.OPTIMIZE:
            base = self.experiments[experiment.based_on or ""]
            assert base.runtime is not None
            return base.runtime
        assert experiment.runtime is not None
        return experiment.runtime


@dataclass(frozen=True)
class ResolvedRoleModel:
    role: str
    profile_id: str
    deployment_id: str
    executor_id: str
    provider: str
    model: str
    deployment_revision: str
    base_url: str
    api_key_env: str | None
    reasoning_effort: str | None
    parameters: Mapping[str, object]
    configured_cost_per_call: float
    content_hash: str

    @classmethod
    def resolve(
        cls,
        role: str,
        profile_id: str,
        configuration: ModelConfiguration,
        deployment: DeploymentConfiguration,
        executor: ExecutorConfiguration,
        environment: Mapping[str, str] | None = None,
    ) -> ResolvedRoleModel:
        del environment
        semantic_identity = {
            "provider": executor.adapter,
            "deployment_id": deployment.id,
            "model": deployment.model,
            "deployment_revision": deployment.revision,
            "reasoning_effort": configuration.reasoning_effort,
            "parameters": configuration.parameters,
        }
        return cls(
            role=role,
            profile_id=profile_id,
            deployment_id=deployment.id,
            executor_id=executor.id,
            provider=executor.adapter,
            model=deployment.model,
            deployment_revision=deployment.revision,
            base_url="",
            api_key_env=None,
            reasoning_effort=configuration.reasoning_effort,
            parameters=configuration.parameters,
            configured_cost_per_call=configuration.configured_cost_per_call,
            content_hash=content_hash(semantic_identity),
        )

    def bind_endpoint(self, endpoint: EndpointConfiguration) -> ResolvedRoleModel:
        """Return a transport-bound copy without changing semantic model identity."""

        route = endpoint.route(self.deployment_id)
        if route is None:
            raise ResolutionError(
                f"endpoint {endpoint.id!r} does not serve deployment {self.deployment_id!r}"
            )

        return replace(
            self,
            model=route.model,
            base_url=endpoint.base_url,
            api_key_env=endpoint.api_key_env,
        )

    def eligible_endpoints(
        self, executor: ExecutorConfiguration
    ) -> tuple[EndpointConfiguration, ...]:
        if executor.id != self.executor_id:
            raise ResolutionError(
                f"model profile {self.profile_id!r} requires executor {self.executor_id!r}"
            )
        return tuple(
            endpoint
            for endpoint in executor.endpoints
            if endpoint.route(self.deployment_id) is not None
        )


def resolve_role_models(
    workspace: WorkspaceConfiguration,
    experiment_id: str,
    environment: Mapping[str, str] | None = None,
) -> Mapping[str, ResolvedRoleModel]:
    workspace.solver(experiment_id)
    experiment = workspace.experiments[experiment_id]

    def resolve(role: str, profile_id: str) -> ResolvedRoleModel:
        configuration = workspace.models[profile_id]
        deployment = workspace.deployments[configuration.deployment]
        executor = workspace.executors[deployment.executor]
        return ResolvedRoleModel.resolve(
            role,
            profile_id,
            configuration,
            deployment,
            executor,
            environment,
        )

    models = {
        "solver": resolve("solver", experiment.roles.solver),
        "judge": resolve("judge", experiment.roles.judge),
    }
    if experiment.mode is ExperimentMode.OPTIMIZE and isinstance(
        experiment.optimizer, CoproConfiguration
    ):
        assert experiment.roles.optimizer is not None
        models["optimizer"] = resolve("optimizer", experiment.roles.optimizer)
    return MappingProxyType(models)


def resolve_stage_model(
    workspace: WorkspaceConfiguration,
    stage_id: str,
    *,
    experiment_id: str | None = None,
    role: str | None = None,
) -> ResolvedRoleModel | None:
    try:
        stage = workspace.stages[stage_id]
    except KeyError as error:
        raise ResolutionError(f"unknown stage: {stage_id}") from error
    if stage_id in _EXPERIMENT_MODEL_STAGES:
        if experiment_id is None:
            raise ResolutionError(f"stage {stage_id!r} requires an experiment id")
        mapped_role = (
            role
            or {
                "validate_judge": "judge",
                "optimize": "optimizer",
                "solve": "solver",
                "judge": "judge",
            }[stage_id]
        )
        return resolve_role_models(workspace, experiment_id).get(mapped_role)
    if stage.model is None or stage.executor is None:
        return None
    configuration = workspace.models[stage.model]
    deployment = workspace.deployments[configuration.deployment]
    return ResolvedRoleModel.resolve(
        role or stage_id,
        stage.model,
        configuration,
        deployment,
        workspace.executors[stage.executor],
    )


@dataclass(frozen=True)
class WorkspaceRepository:
    workspace_file: Path
    root: Path
    workspace: WorkspaceConfiguration

    @classmethod
    def from_file(cls, path: Path) -> WorkspaceRepository:
        resolved = path.resolve()
        return cls(
            resolved,
            resolved.parent,
            WorkspaceConfiguration.from_mapping(load_yaml(resolved)),
        )

    def path(self, relative: str, *, field: str) -> Path:
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ConfigurationError(f"{field} escapes the workspace")
        return candidate

    @property
    def cases_root(self) -> Path:
        return self.path(self.workspace.evaluation.cases.directory, field="cases.directory")

    @property
    def split_file(self) -> Path:
        return self.path(self.workspace.evaluation.splits.file, field="splits.file")

    @property
    def capability_catalog_path(self) -> Path:
        relative = self.workspace.evaluation.cases.coverage.catalog
        source = self.root / relative
        if source.is_symlink() or not source.is_file():
            raise ResolutionError(f"capability catalog does not exist or is unsafe: {source}")
        return self.path(relative, field="evaluation.cases.coverage.catalog")

    @property
    def judge_validation_root(self) -> Path:
        return self.path(
            self.workspace.evaluation.judge.validation_directory,
            field="judge.validation.directory",
        )

    @property
    def case_authoring_path(self) -> Path:
        relative = self.workspace.evaluation.case_authoring.instructions
        source = self.root / relative
        if source.is_symlink() or not source.is_file():
            raise ResolutionError(
                f"case-authoring instructions do not exist or are unsafe: {source}"
            )
        return self.path(relative, field="evaluation.case_authoring.instructions")

    def case_authoring_instructions(self) -> str:
        path = self.case_authoring_path
        try:
            instructions = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as error:
            raise ConfigurationError(
                "case-authoring instructions must be UTF-8", path=path
            ) from error
        if not instructions.strip():
            raise ConfigurationError("case-authoring instructions must not be empty", path=path)
        return instructions

    def case_paths(self) -> tuple[Path, ...]:
        root = self.cases_root
        if not root.is_dir() or root.is_symlink():
            raise ResolutionError(f"cases directory does not exist or is unsafe: {root}")
        paths = tuple(sorted(path for path in root.rglob("case.yaml") if path.is_file()))
        for path in paths:
            if path.is_symlink() or root not in path.resolve().parents:
                raise ResolutionError(f"case path is unsafe: {path}")
        return paths

    def target_specification(self) -> TargetSpecification:
        instructions = self.workspace.evaluation.instructions
        contexts = tuple(
            ContextFileSpecification(f"global-{index:03d}", path, True)
            for index, path in enumerate(instructions.global_paths, start=1)
        )
        selection = instructions.skills
        if selection.directory is not None:
            source = UnitSourceSpecification(
                id="skills",
                type="directory",
                root=selection.directory,
                entries=(),
            )
        else:
            source = UnitSourceSpecification(
                id="skills",
                type="explicit",
                root=None,
                entries=selection.explicit_entries(),
            )
        bundle = InstructionBundleSpecification(
            id="instructions",
            display_name="Repository instructions",
            global_context=contexts,
            unit_sources=(source,),
        )
        return TargetSpecification(
            schema_version=1,
            id=self.workspace.id,
            display_name=self.workspace.id,
            source=self.workspace.evaluation.repository.to_git_source(),
            instruction_bundles=(bundle,),
        )

    def authoring_source_specifications(
        self,
    ) -> tuple[tuple[AuthoringSourceConfiguration, TargetSpecification], ...]:
        return tuple(
            (
                source,
                TargetSpecification(
                    schema_version=1,
                    id=f"{self.workspace.id}-{source.id}",
                    display_name=source.id,
                    source=source.repository.to_git_source(),
                    instruction_bundles=(),
                ),
            )
            for source in self.workspace.evaluation.case_authoring.sources
        )

    def environment(self) -> Mapping[str, str]:
        values = dict(os.environ)
        dotenv = self.root / ".env"
        if not dotenv.exists():
            return MappingProxyType(values)
        if dotenv.is_symlink() or not dotenv.is_file():
            raise ConfigurationError("workspace .env must be a regular file", path=dotenv)
        for line_number, raw_line in enumerate(
            dotenv.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line.removeprefix("export ").lstrip()
            key, separator, raw_value = line.partition("=")
            if not separator or _ENVIRONMENT.fullmatch(key.strip()) is None:
                raise ConfigurationError(f"invalid .env entry on line {line_number}", path=dotenv)
            value = raw_value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values.setdefault(key.strip(), value)
        return MappingProxyType(values)

    @property
    def data_root(self) -> Path:
        configured = self.workspace.data_root
        if configured is None:
            candidate = Path.home() / "ms_agent_eval" / self.workspace.id
        else:
            candidate = Path(configured).expanduser()
            if not candidate.is_absolute():
                candidate = self.root / candidate
        return validate_external_data_root(candidate, self.root)
