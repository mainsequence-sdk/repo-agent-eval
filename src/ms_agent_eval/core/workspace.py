from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


WORKSPACE_SCHEMA_VERSION = 4
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ENVIRONMENT = re.compile(r"^[A-Z][A-Z0-9_]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SPLITS = {"train", "development", "test", "challenge"}
_PROGRAM_FIELDS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "case_builder": {
        "inputs": (
            "global_context",
            "skill_context",
            "source_context",
            "coverage_request",
            "existing_case_summaries",
        ),
        "outputs": (
            "case_spec",
            "prompt",
            "expected_response",
            "rubric",
            "expected_artifacts",
            "source_paths",
            "leakage_group",
        ),
    },
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
    "case_builder": {
        "inputs": {
            "global_context": "str",
            "skill_context": "str",
            "source_context": "str",
            "coverage_request": "str",
            "existing_case_summaries": "list[str]",
        },
        "outputs": {
            "case_spec": "dict[str, object]",
            "prompt": "str",
            "expected_response": "str",
            "rubric": "dict[str, object]",
            "expected_artifacts": "dict[str, str]",
            "source_paths": "list[str]",
            "leakage_group": "str",
        },
    },
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
        raise ConfigurationError(
            f"{field} must not contain credentials, a query, or a fragment"
        )
    return result


def _relative_path(value: object, field: str) -> str:
    result = _string(value, field)
    path = PurePosixPath(result)
    if path.is_absolute() or ".." in path.parts or "\x00" in result:
        raise ConfigurationError(f"{field} must be a safe relative POSIX path")
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
        (
            ancestor
            for ancestor in (workspace, *workspace.parents)
            if (ancestor / ".git").exists()
        ),
        None,
    )
    if git_root is not None and (root == git_root or git_root in root.parents):
        raise ConfigurationError("workspace.data_root must be outside the Git repository")
    return root


class ExperimentMode(str, Enum):
    EVALUATE = "evaluate"
    OPTIMIZE = "optimize"


@dataclass(frozen=True)
class RepositoryConfiguration:
    url: str
    ref: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> RepositoryConfiguration:
        _reject_unknown(payload, {"url", "ref"}, "evaluation.repository")
        url = _string(payload.get("url"), "evaluation.repository.url")
        ref = _string(payload.get("ref"), "evaluation.repository.ref")
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
class SkillSelection:
    directory: str | None
    files: tuple[str, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> SkillSelection:
        _reject_unknown(payload, {"directory", "files"}, "evaluation.instructions.skills")
        raw_directory = payload.get("directory")
        raw_files = payload.get("files")
        if (raw_directory is None) == (raw_files is None):
            raise ConfigurationError(
                "evaluation.instructions.skills requires exactly one of directory or files"
            )
        if raw_directory is not None:
            return cls(_relative_path(raw_directory, "skills.directory"), ())
        files = tuple(
            _relative_path(item, "skills.files[]")
            for item in _string_list(raw_files, "skills.files")
        )
        if any(PurePosixPath(item).name != "SKILL.md" for item in files):
            raise ConfigurationError("every explicit skill file must be named SKILL.md")
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
class InstructionConfiguration:
    global_paths: tuple[str, ...]
    skills: SkillSelection

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> InstructionConfiguration:
        _reject_unknown(payload, {"global", "skills"}, "evaluation.instructions")
        global_paths = tuple(
            _relative_path(item, "evaluation.instructions.global[]")
            for item in _string_list(payload.get("global", []), "evaluation.instructions.global")
        )
        return cls(
            global_paths,
            SkillSelection.from_mapping(
                _mapping(payload.get("skills"), "evaluation.instructions.skills")
            ),
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
        if (
            tuple(self.inputs) != expected["inputs"]
            or tuple(self.outputs) != expected["outputs"]
            or dict(self.inputs) != expected_types["inputs"]
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
    provider: str
    model: str
    base_url: str
    api_key_env: str | None
    parameters: Mapping[str, object]
    configured_cost_per_call: float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, field: str) -> ModelConfiguration:
        _reject_unknown(
            payload,
            {
                "provider",
                "name",
                "base_url",
                "api_key_env",
                "parameters",
                "configured_cost_per_call",
            },
            field,
        )
        provider = _identifier(payload.get("provider"), f"{field}.provider")
        if provider != "openai_compatible":
            raise ConfigurationError(
                f"{field}.provider must currently be openai_compatible"
            )
        parameters = MappingProxyType(
            dict(_mapping(payload.get("parameters", {}), f"{field}.parameters"))
        )
        cost = payload.get("configured_cost_per_call", 0.0)
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or float(cost) < 0:
            raise ConfigurationError(f"{field}.configured_cost_per_call must be non-negative")
        return cls(
            provider,
            _string(payload.get("name"), f"{field}.name"),
            _api_base_url(payload.get("base_url"), f"{field}.base_url"),
            (
                _environment(payload.get("api_key_env"), f"{field}.api_key_env")
                if payload.get("api_key_env") is not None
                else None
            ),
            parameters,
            float(cost),
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
            _positive_int(payload.get("model_calls"), f"{field}.model_calls"),
            _positive_int(payload.get("tokens"), f"{field}.tokens"),
            _positive_number(payload.get("wall_seconds", 1800), f"{field}.wall_seconds"),
        )


@dataclass(frozen=True)
class CaseBuilderConfiguration:
    instructions: str
    program: DspyProgramConfiguration
    model: ModelConfiguration
    budget: RoleBudget
    drafts: str
    promotion: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> CaseBuilderConfiguration:
        _reject_unknown(
            payload,
            {"instructions", "dspy", "model", "budget", "output"},
            "evaluation.case_builder",
        )
        output = _mapping(payload.get("output"), "evaluation.case_builder.output")
        _reject_unknown(output, {"drafts", "promotion"}, "evaluation.case_builder.output")
        drafts = _string(output.get("drafts"), "case_builder.output.drafts")
        promotion = _string(output.get("promotion"), "case_builder.output.promotion")
        if drafts != "external" or promotion != "explicit":
            raise ConfigurationError(
                "case_builder output must use external drafts and explicit promotion"
            )
        return cls(
            _relative_path(
                payload.get("instructions"), "evaluation.case_builder.instructions"
            ),
            DspyProgramConfiguration.from_mapping(
                _mapping(payload.get("dspy"), "evaluation.case_builder.dspy"),
                field="evaluation.case_builder.dspy",
            ),
            ModelConfiguration.from_mapping(
                _mapping(payload.get("model"), "evaluation.case_builder.model"),
                field="evaluation.case_builder.model",
            ),
            RoleBudget.from_mapping(
                _mapping(payload.get("budget"), "evaluation.case_builder.budget"),
                field="evaluation.case_builder.budget",
            ),
            drafts,
            promotion,
        )


@dataclass(frozen=True)
class JudgeConfiguration:
    program: DspyProgramConfiguration
    model: ModelConfiguration
    calibration_directory: str
    repetitions: int

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> JudgeConfiguration:
        _reject_unknown(
            payload, {"dspy", "model", "calibration", "repetitions"}, "evaluation.judge"
        )
        calibration = _mapping(payload.get("calibration"), "evaluation.judge.calibration")
        _reject_unknown(calibration, {"directory"}, "evaluation.judge.calibration")
        return cls(
            DspyProgramConfiguration.from_mapping(
                _mapping(payload.get("dspy"), "evaluation.judge.dspy"),
                field="evaluation.judge.dspy",
            ),
            ModelConfiguration.from_mapping(
                _mapping(payload.get("model"), "evaluation.judge.model"),
                field="evaluation.judge.model",
            ),
            _relative_path(calibration.get("directory"), "evaluation.judge.calibration.directory"),
            _positive_int(payload.get("repetitions", 1), "evaluation.judge.repetitions"),
        )


@dataclass(frozen=True)
class CasesConfiguration:
    directory: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> CasesConfiguration:
        _reject_unknown(payload, {"directory"}, "evaluation.cases")
        return cls(_relative_path(payload.get("directory"), "evaluation.cases.directory"))


@dataclass(frozen=True)
class SplitPolicy:
    seed: str
    train: float
    development: float
    test: float
    challenge: float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> SplitPolicy:
        _reject_unknown(
            payload,
            {"seed", "train", "development", "test", "challenge"},
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
        return cls(
            seed, values["train"], values["development"], values["test"], values["challenge"]
        )


@dataclass(frozen=True)
class SplitsConfiguration:
    file: str
    policy: SplitPolicy

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> SplitsConfiguration:
        _reject_unknown(payload, {"file", "policy"}, "evaluation.splits")
        default_policy = {
            "seed": "ms-agent-eval",
            "train": 0.5,
            "development": 0.2,
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
    case_builder: CaseBuilderConfiguration
    cases: CasesConfiguration
    splits: SplitsConfiguration
    judge: JudgeConfiguration

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> EvaluationConfiguration:
        _reject_unknown(
            payload,
            {"repository", "instructions", "case_builder", "cases", "splits", "judge"},
            "evaluation",
        )
        return cls(
            RepositoryConfiguration.from_mapping(
                _mapping(payload.get("repository"), "evaluation.repository")
            ),
            InstructionConfiguration.from_mapping(
                _mapping(payload.get("instructions"), "evaluation.instructions")
            ),
            CaseBuilderConfiguration.from_mapping(
                _mapping(payload.get("case_builder"), "evaluation.case_builder")
            ),
            CasesConfiguration.from_mapping(_mapping(payload.get("cases"), "evaluation.cases")),
            SplitsConfiguration.from_mapping(_mapping(payload.get("splits"), "evaluation.splits")),
            JudgeConfiguration.from_mapping(_mapping(payload.get("judge"), "evaluation.judge")),
        )


@dataclass(frozen=True)
class SolverConfiguration:
    program: DspyProgramConfiguration
    model: ModelConfiguration

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> SolverConfiguration:
        _reject_unknown(payload, {"dspy", "model"}, "experiment.solver")
        return cls(
            DspyProgramConfiguration.from_mapping(
                _mapping(payload.get("dspy"), "experiment.solver.dspy"),
                field="experiment.solver.dspy",
            ),
            ModelConfiguration.from_mapping(
                _mapping(payload.get("model"), "experiment.solver.model"),
                field="experiment.solver.model",
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
class OptimizerConfiguration:
    name: str
    parameters: Mapping[str, object]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> OptimizerConfiguration:
        _reject_unknown(payload, {"name", "parameters"}, "experiment.optimizer")
        name = _string(payload.get("name"), "experiment.optimizer.name")
        if name != "LabeledFewShot":
            raise ConfigurationError("the initial DSPy optimizer must be LabeledFewShot")
        return cls(
            name,
            MappingProxyType(
                dict(_mapping(payload.get("parameters", {}), "experiment.optimizer.parameters"))
            ),
        )


@dataclass(frozen=True)
class OptimizationBudget:
    solver: RoleBudget
    judge: RoleBudget
    wall_seconds: float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> OptimizationBudget:
        _reject_unknown(payload, {"solver", "judge", "wall_seconds"}, "experiment.budget")
        return cls(
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
class ExperimentConfiguration:
    id: str
    mode: ExperimentMode
    solver: SolverConfiguration | None
    based_on: str | None
    runtime: RuntimeConfiguration | None
    repetitions: int
    dataset: Mapping[str, str] | None
    optimizer: OptimizerConfiguration | None
    budget: OptimizationBudget | None
    compiled_output: str | None

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
                {"mode", "solver", "runtime", "repetitions"},
                f"experiments.{experiment_id}",
            )
            if payload.get("based_on") is not None or payload.get("optimizer") is not None:
                raise ConfigurationError("evaluate experiments cannot declare optimization fields")
            return cls(
                _identifier(experiment_id, "experiment id"),
                mode,
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
                None,
            )
        _reject_unknown(
            payload,
            {"mode", "based_on", "dataset", "optimizer", "budget", "output"},
            f"experiments.{experiment_id}",
        )
        dataset = {
            key: _string(value, f"experiment.dataset.{key}")
            for key, value in _mapping(payload.get("dataset"), "experiment.dataset").items()
        }
        required = {"train", "development", "final_evaluation"}
        if set(dataset) != required or set(dataset.values()) != {"train", "development", "test"}:
            raise ConfigurationError(
                "optimization dataset must map train, development, and final_evaluation to protected splits"
            )
        output = _mapping(payload.get("output"), "experiment.output")
        _reject_unknown(output, {"compiled_program"}, "experiment.output")
        compiled = _string(output.get("compiled_program"), "output.compiled_program")
        if compiled != "content_addressed_json":
            raise ConfigurationError("compiled programs must use content_addressed_json")
        return cls(
            _identifier(experiment_id, "experiment id"),
            mode,
            None,
            _identifier(payload.get("based_on"), "experiment.based_on"),
            None,
            1,
            MappingProxyType(dataset),
            OptimizerConfiguration.from_mapping(
                _mapping(payload.get("optimizer"), "experiment.optimizer")
            ),
            OptimizationBudget.from_mapping(_mapping(payload.get("budget"), "experiment.budget")),
            compiled,
        )


@dataclass(frozen=True)
class WorkspaceConfiguration:
    schema_version: int
    id: str
    data_root: str | None
    evaluation: EvaluationConfiguration
    experiments: Mapping[str, ExperimentConfiguration]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> WorkspaceConfiguration:
        _reject_unknown(
            payload, {"schema_version", "workspace", "evaluation", "experiments"}, "workspace"
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
        experiments_payload = _mapping(payload.get("experiments"), "experiments")
        experiments = {
            experiment_id: ExperimentConfiguration.from_mapping(
                experiment_id, _mapping(item, f"experiments.{experiment_id}")
            )
            for experiment_id, item in experiments_payload.items()
        }
        if not experiments:
            raise ConfigurationError("workspace requires at least one experiment")
        for experiment in experiments.values():
            if experiment.mode is ExperimentMode.OPTIMIZE:
                base = experiments.get(experiment.based_on or "")
                if base is None or base.mode is not ExperimentMode.EVALUATE:
                    raise ConfigurationError(
                        f"optimization experiment {experiment.id!r} must reference an evaluate experiment"
                    )
        result = cls(
            WORKSPACE_SCHEMA_VERSION,
            _identifier(workspace.get("id"), "workspace.id"),
            data_root,
            EvaluationConfiguration.from_mapping(_mapping(payload.get("evaluation"), "evaluation")),
            MappingProxyType(experiments),
        )
        result._validate_role_configuration()
        result.evaluation.case_builder.program.validate_role(
            "case_builder", field="evaluation.case_builder.dspy"
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
                "evaluation": self.evaluation,
                "experiments": self.experiments,
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

    def _validate_role_configuration(self) -> None:
        builder = self.evaluation.case_builder.model
        judge = self.evaluation.judge.model
        if (builder.provider, builder.model) == (judge.provider, judge.model):
            raise ConfigurationError("case-builder and judge model identities must differ")
        for experiment in self.experiments.values():
            if experiment.mode is not ExperimentMode.EVALUATE:
                continue
            assert experiment.solver is not None
            solver = experiment.solver.model
            identities = {
                (builder.provider, builder.model),
                (solver.provider, solver.model),
                (judge.provider, judge.model),
            }
            if len(identities) != 3:
                raise ConfigurationError(
                    f"experiment {experiment.id!r} must configure three distinct LLM models"
                )


@dataclass(frozen=True)
class ResolvedRoleModel:
    role: str
    provider: str
    model: str
    base_url: str
    api_key_env: str | None
    parameters: Mapping[str, object]
    configured_cost_per_call: float
    content_hash: str

    @classmethod
    def resolve(
        cls,
        role: str,
        configuration: ModelConfiguration,
        environment: Mapping[str, str] | None = None,
    ) -> ResolvedRoleModel:
        del environment
        identity = {
            "role": role,
            "provider": configuration.provider,
            "model": configuration.model,
            "base_url": configuration.base_url,
            "api_key_env": configuration.api_key_env,
            "parameters": configuration.parameters,
            "configured_cost_per_call": configuration.configured_cost_per_call,
        }
        return cls(**identity, content_hash=content_hash(identity))

    def api_key(self, environment: Mapping[str, str]) -> str | None:
        if self.api_key_env is None:
            return None
        value = environment.get(self.api_key_env, "").strip()
        if not value:
            raise ResolutionError(
                f"{self.role} API credential is required in {self.api_key_env}"
            )
        return value


def resolve_role_models(
    workspace: WorkspaceConfiguration,
    experiment_id: str,
    environment: Mapping[str, str] | None = None,
) -> Mapping[str, ResolvedRoleModel]:
    solver = workspace.solver(experiment_id)
    models = {
        "case_builder": ResolvedRoleModel.resolve(
            "case_builder", workspace.evaluation.case_builder.model, environment
        ),
        "solver": ResolvedRoleModel.resolve("solver", solver.model, environment),
        "judge": ResolvedRoleModel.resolve("judge", workspace.evaluation.judge.model, environment),
    }
    identities = {(item.provider, item.model) for item in models.values()}
    if len(identities) != 3:
        raise ConfigurationError(
            "case-builder, solver, and judge must resolve to three distinct provider/model identities"
        )
    return MappingProxyType(models)


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
    def calibration_root(self) -> Path:
        return self.path(
            self.workspace.evaluation.judge.calibration_directory,
            field="judge.calibration.directory",
        )

    @property
    def case_authoring_path(self) -> Path:
        relative = self.workspace.evaluation.case_builder.instructions
        source = self.root / relative
        if source.is_symlink() or not source.is_file():
            raise ResolutionError(
                f"case-authoring instructions do not exist or are unsafe: {source}"
            )
        return self.path(relative, field="evaluation.case_builder.instructions")

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
