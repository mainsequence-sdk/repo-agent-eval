from __future__ import annotations

import json
import hashlib
import re
import statistics
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

import yaml

from .errors import ConfigurationError, IntegrityError, PreflightError, ResolutionError
from .hashing import content_hash, sha256_file


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_CONTENT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_CASE_ARCHETYPES = {"construction", "review", "diagnosis", "migration", "evidence"}
_CASE_DIFFICULTIES = {"easy", "complex"}


def _identifier(value: object, field: str, *, path: Path) -> str:
    result = _string(value, field, path=path)
    if _IDENTIFIER.fullmatch(result) is None:
        raise ConfigurationError(f"{field} must be a lowercase identifier", path=path)
    return result


def _skill_id(value: object, field: str, *, path: Path) -> str:
    result = _string(value, field, path=path)
    if any(_IDENTIFIER.fullmatch(part) is None for part in result.split("/")):
        raise ConfigurationError(f"{field} must be a slash-delimited identifier", path=path)
    return result


def _content_hash_value(value: object, field: str, *, path: Path) -> str:
    result = _string(value, field, path=path)
    if _CONTENT_HASH.fullmatch(result) is None:
        raise ConfigurationError(f"{field} must be a sha256 content hash", path=path)
    return result


def _mapping(value: object, field: str, *, path: Path) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field} must be a mapping", path=path)
    return {str(key): item for key, item in value.items()}


def _reject_unknown(
    payload: Mapping[str, object], allowed: set[str], field: str, *, path: Path
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ConfigurationError(f"{field} contains unknown fields: {unknown}", path=path)


def _sequence(value: object, field: str, *, path: Path) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ConfigurationError(f"{field} must be a list", path=path)
    return value


def _string(value: object, field: str, *, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty string", path=path)
    return value.strip()


def _relative_file(directory: Path, value: object, field: str, *, path: Path) -> Path:
    relative = Path(_string(value, field, path=path))
    if relative.is_absolute() or ".." in relative.parts:
        raise ConfigurationError(f"{field} must be a safe relative path", path=path)
    result = (directory / relative).resolve()
    root = directory.resolve()
    if root not in result.parents or result.is_symlink() or not result.is_file():
        raise ResolutionError(f"{field} does not identify a safe file: {relative}")
    return result


def _yaml(path: Path) -> Mapping[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ResolutionError(f"required document is missing or unsafe: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConfigurationError(f"invalid YAML: {error}", path=path) from error
    return _mapping(payload, "document", path=path)


@dataclass(frozen=True)
class CapabilityRequirement:
    id: str
    title: str
    description: str
    minimum_primary_cases: int


@dataclass(frozen=True)
class SkillCapabilityCatalog:
    skill: str
    capabilities: tuple[CapabilityRequirement, ...]


@dataclass(frozen=True)
class CapabilityCatalog:
    status: str
    default_minimum_primary_cases: int
    skills: tuple[SkillCapabilityCatalog, ...]
    content_hash: str

    @classmethod
    def load(cls, path: Path) -> CapabilityCatalog:
        payload = _yaml(path)
        _reject_unknown(
            payload,
            {
                "schema_version",
                "status",
                "default_minimum_primary_cases",
                "skills",
            },
            "capability catalog",
            path=path,
        )
        if payload.get("schema_version") != 1:
            raise ConfigurationError("capability catalog schema_version must be 1", path=path)
        status = _string(payload.get("status"), "capability catalog.status", path=path)
        if status not in {"draft", "complete"}:
            raise ConfigurationError(
                "capability catalog.status must be draft or complete", path=path
            )
        raw_default = payload.get("default_minimum_primary_cases", 3)
        if (
            not isinstance(raw_default, int)
            or isinstance(raw_default, bool)
            or raw_default < 1
        ):
            raise ConfigurationError(
                "capability catalog.default_minimum_primary_cases must be a positive integer",
                path=path,
            )
        raw_skills = _mapping(payload.get("skills", {}), "capability catalog.skills", path=path)
        skills: list[SkillCapabilityCatalog] = []
        for raw_skill, raw_entry in sorted(raw_skills.items()):
            skill = _skill_id(raw_skill, "capability catalog skill", path=path)
            entry = _mapping(
                raw_entry, f"capability catalog.skills.{skill}", path=path
            )
            _reject_unknown(
                entry,
                {"capabilities"},
                f"capability catalog.skills.{skill}",
                path=path,
            )
            capabilities: list[CapabilityRequirement] = []
            for raw_capability in _sequence(
                entry.get("capabilities", []),
                f"capability catalog.skills.{skill}.capabilities",
                path=path,
            ):
                item = _mapping(raw_capability, "capability", path=path)
                _reject_unknown(
                    item,
                    {"id", "title", "description", "minimum_primary_cases"},
                    "capability",
                    path=path,
                )
                minimum = item.get("minimum_primary_cases", raw_default)
                if (
                    not isinstance(minimum, int)
                    or isinstance(minimum, bool)
                    or minimum < 1
                ):
                    raise ConfigurationError(
                        "capability.minimum_primary_cases must be a positive integer",
                        path=path,
                    )
                capabilities.append(
                    CapabilityRequirement(
                        _identifier(item.get("id"), "capability.id", path=path),
                        _string(item.get("title"), "capability.title", path=path),
                        _string(
                            item.get("description"),
                            "capability.description",
                            path=path,
                        ),
                        minimum,
                    )
                )
            capability_ids = [item.id for item in capabilities]
            if len(capability_ids) != len(set(capability_ids)):
                raise ConfigurationError(
                    f"capability ids must be unique within skill {skill!r}", path=path
                )
            if status == "complete" and not capabilities:
                raise ConfigurationError(
                    f"complete capability catalog has no capabilities for skill {skill!r}",
                    path=path,
                )
            skills.append(SkillCapabilityCatalog(skill, tuple(capabilities)))
        return cls(status, raw_default, tuple(skills), sha256_file(path))

    def requirements_for(self, skill: str) -> tuple[CapabilityRequirement, ...] | None:
        return next(
            (item.capabilities for item in self.skills if item.skill == skill),
            None,
        )


@dataclass(frozen=True)
class CaseCoverage:
    primary: str
    supporting: tuple[str, ...]
    archetype: str
    difficulty: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, path: Path) -> CaseCoverage:
        _reject_unknown(
            payload,
            {"primary", "supporting", "archetype", "difficulty"},
            "case.coverage",
            path=path,
        )
        primary = _identifier(payload.get("primary"), "coverage.primary", path=path)
        supporting = tuple(
            _identifier(item, "coverage.supporting[]", path=path)
            for item in _sequence(
                payload.get("supporting", []), "coverage.supporting", path=path
            )
        )
        if len(supporting) > 2:
            raise ConfigurationError(
                "coverage.supporting may contain at most two capabilities", path=path
            )
        if len(supporting) != len(set(supporting)) or primary in supporting:
            raise ConfigurationError(
                "coverage capabilities must be unique and primary cannot be supporting",
                path=path,
            )
        archetype = _identifier(payload.get("archetype"), "coverage.archetype", path=path)
        if archetype not in _CASE_ARCHETYPES:
            raise ConfigurationError(
                f"coverage.archetype must be one of {sorted(_CASE_ARCHETYPES)}", path=path
            )
        difficulty = _identifier(payload.get("difficulty"), "coverage.difficulty", path=path)
        if difficulty not in _CASE_DIFFICULTIES:
            raise ConfigurationError(
                f"coverage.difficulty must be one of {sorted(_CASE_DIFFICULTIES)}", path=path
            )
        return cls(primary, supporting, archetype, difficulty)


@dataclass(frozen=True)
class RubricCriterion:
    id: str
    weight: float
    description: str


@dataclass(frozen=True)
class HardFailure:
    id: str
    description: str


@dataclass(frozen=True)
class Rubric:
    passing_score: float
    criteria: tuple[RubricCriterion, ...]
    hard_failures: tuple[HardFailure, ...]

    @classmethod
    def load(cls, path: Path) -> Rubric:
        payload = _yaml(path)
        _reject_unknown(
            payload, {"passing_score", "criteria", "hard_failures"}, "rubric", path=path
        )
        passing = payload.get("passing_score")
        if (
            not isinstance(passing, (int, float))
            or isinstance(passing, bool)
            or not 0 <= float(passing) <= 1
        ):
            raise ConfigurationError("passing_score must be in [0, 1]", path=path)
        criteria: list[RubricCriterion] = []
        for raw in _sequence(payload.get("criteria"), "criteria", path=path):
            item = _mapping(raw, "criteria[]", path=path)
            _reject_unknown(item, {"id", "weight", "description"}, "criteria[]", path=path)
            weight = item.get("weight")
            if (
                not isinstance(weight, (int, float))
                or isinstance(weight, bool)
                or not 0 < float(weight) <= 1
            ):
                raise ConfigurationError("criterion weight must be in (0, 1]", path=path)
            criteria.append(
                RubricCriterion(
                    _identifier(item.get("id"), "criterion.id", path=path),
                    float(weight),
                    _string(item.get("description"), "criterion.description", path=path),
                )
            )
        if not criteria or abs(sum(item.weight for item in criteria) - 1.0) > 1e-9:
            raise ConfigurationError("rubric criterion weights must sum to 1.0", path=path)
        criterion_ids = [item.id for item in criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ConfigurationError("rubric criterion ids must be unique", path=path)
        failures: list[HardFailure] = []
        for raw in _sequence(payload.get("hard_failures", []), "hard_failures", path=path):
            if isinstance(raw, str):
                failures.append(HardFailure(_identifier(raw, "hard_failure.id", path=path), raw))
                continue
            item = _mapping(raw, "hard_failures[]", path=path)
            _reject_unknown(item, {"id", "description"}, "hard_failures[]", path=path)
            failures.append(
                HardFailure(
                    _identifier(item.get("id"), "hard_failure.id", path=path),
                    _string(item.get("description"), "hard_failure.description", path=path),
                )
            )
        failure_ids = [item.id for item in failures]
        if len(failure_ids) != len(set(failure_ids)):
            raise ConfigurationError("hard-failure ids must be unique", path=path)
        return cls(float(passing), tuple(criteria), tuple(failures))

    @property
    def content_hash(self) -> str:
        return content_hash(self)

    def prompt_text(self) -> str:
        """Render semantic judging guidance without the coordinator's pass threshold."""

        return json.dumps(
            {
                "criteria": [
                    {
                        "id": item.id,
                        "weight": item.weight,
                        "description": item.description,
                    }
                    for item in self.criteria
                ],
                "hard_failures": [
                    {"id": item.id, "description": item.description} for item in self.hard_failures
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )


@dataclass(frozen=True)
class CaseProvenance:
    authoring_agent: str
    authoring_instructions_hash: str
    capability_catalog_hash: str
    source_snapshot_hash: str
    case_content_hash: str

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        path: Path,
        allow_pending_case_hash: bool = False,
    ) -> CaseProvenance:
        _reject_unknown(
            payload,
            {
                "authoring_agent",
                "authoring_instructions_hash",
                "capability_catalog_hash",
                "source_snapshot_hash",
                "case_content_hash",
            },
            "case.provenance",
            path=path,
        )
        authoring_agent = _string(
            payload.get("authoring_agent"), "provenance.authoring_agent", path=path
        )
        if authoring_agent != "codex":
            raise ConfigurationError(
                "provenance.authoring_agent must be codex", path=path
            )
        instructions_hash = _content_hash_value(
            payload.get("authoring_instructions_hash"),
            "provenance.authoring_instructions_hash",
            path=path,
        )
        catalog_hash = _content_hash_value(
            payload.get("capability_catalog_hash"),
            "provenance.capability_catalog_hash",
            path=path,
        )
        snapshot_hash = _content_hash_value(
            payload.get("source_snapshot_hash"),
            "provenance.source_snapshot_hash",
            path=path,
        )
        raw_case_hash = payload.get("case_content_hash")
        if allow_pending_case_hash and raw_case_hash == "pending":
            case_hash = "pending"
        else:
            case_hash = _content_hash_value(
                raw_case_hash, "provenance.case_content_hash", path=path
            )
        return cls(
            authoring_agent,
            instructions_hash,
            catalog_hash,
            snapshot_hash,
            case_hash,
        )


@dataclass(frozen=True)
class CaseDefinition:
    id: str
    title: str
    skill: str
    group: str
    path: Path
    solver_prompt_path: Path
    solver_attachments_directory: Path | None
    judge_expected_path: Path
    judge_rubric_path: Path
    expected_artifacts_directory: Path | None
    prompt: str
    expected_response: str
    rubric: Rubric
    coverage: CaseCoverage
    source_paths: tuple[str, ...]
    provenance: CaseProvenance
    content_hash: str

    @classmethod
    def load(cls, case_directory: Path, *, verify_provenance: bool = True) -> CaseDefinition:
        directory = case_directory.resolve()
        case_file = directory / "case.yaml"
        payload = _yaml(case_file)
        _reject_unknown(
            payload,
            {
                "schema_version",
                "id",
                "title",
                "skill",
                "group",
                "coverage",
                "solver",
                "judge",
                "source_paths",
                "provenance",
            },
            "case",
            path=case_file,
        )
        if payload.get("schema_version") != 5:
            raise ConfigurationError(
                "case schema_version must be 5; legacy cases are not supported", path=case_file
            )
        solver = _mapping(payload.get("solver"), "case.solver", path=case_file)
        _reject_unknown(
            solver,
            {"prompt", "attachments"},
            "case.solver",
            path=case_file,
        )
        judge = _mapping(payload.get("judge"), "case.judge", path=case_file)
        _reject_unknown(
            judge,
            {"rubric", "expected_response", "expected_artifacts"},
            "case.judge",
            path=case_file,
        )
        prompt_path = _owned_relative_file(
            directory,
            solver.get("prompt"),
            "case.solver.prompt",
            owner="solver",
            path=case_file,
        )
        solver_attachments = _optional_owned_directory(
            directory,
            solver.get("attachments"),
            "case.solver.attachments",
            owner="solver",
            path=case_file,
        )
        expected_path = _owned_relative_file(
            directory,
            judge.get("expected_response"),
            "case.judge.expected_response",
            owner="judge",
            path=case_file,
        )
        rubric_path = _owned_relative_file(
            directory,
            judge.get("rubric"),
            "case.judge.rubric",
            owner="judge",
            path=case_file,
        )
        raw_artifacts = judge.get("expected_artifacts")
        artifacts_directory: Path | None = None
        if raw_artifacts is not None:
            artifacts_directory = _optional_owned_directory(
                directory,
                raw_artifacts,
                "case.judge.expected_artifacts",
                owner="judge",
                path=case_file,
            )
        _validate_case_package_files(
            directory,
            case_file=case_file,
            prompt_path=prompt_path,
            solver_attachments=solver_attachments,
            rubric_path=rubric_path,
            expected_path=expected_path,
            expected_artifacts=artifacts_directory,
        )
        raw_sources = payload.get("source_paths", [])
        source_paths = tuple(
            _safe_source_path(item, path=case_file)
            for item in _sequence(raw_sources, "case.source_paths", path=case_file)
        )
        if len(source_paths) != len(set(source_paths)):
            raise ConfigurationError("case.source_paths must be unique", path=case_file)
        provenance = CaseProvenance.from_mapping(
            _mapping(payload.get("provenance"), "case.provenance", path=case_file),
            path=case_file,
            allow_pending_case_hash=not verify_provenance,
        )
        coverage = CaseCoverage.from_mapping(
            _mapping(payload.get("coverage"), "case.coverage", path=case_file),
            path=case_file,
        )
        identity = {
            "schema_version": 5,
            "id": _identifier(payload.get("id"), "case.id", path=case_file),
            "title": _string(payload.get("title"), "case.title", path=case_file),
            "skill": _skill_id(payload.get("skill"), "case.skill", path=case_file),
            "group": _identifier(payload.get("group"), "case.group", path=case_file),
            "coverage": coverage,
            "solver": {
                "prompt_hash": sha256_file(prompt_path),
                "attachments": _artifact_inventory(solver_attachments),
            },
            "judge": {
                "expected_response_hash": sha256_file(expected_path),
                "rubric_hash": sha256_file(rubric_path),
                "expected_artifacts": _artifact_inventory(artifacts_directory),
            },
            "source_paths": source_paths,
        }
        package_hash = content_hash(identity)
        if verify_provenance and provenance.case_content_hash != package_hash:
            raise IntegrityError(
                f"case {identity['id']!r} changed after authoring: "
                f"{provenance.case_content_hash} != {package_hash}"
            )
        return cls(
            id=identity["id"],
            title=identity["title"],
            skill=identity["skill"],
            group=identity["group"],
            path=directory,
            solver_prompt_path=prompt_path,
            solver_attachments_directory=solver_attachments,
            judge_expected_path=expected_path,
            judge_rubric_path=rubric_path,
            expected_artifacts_directory=artifacts_directory,
            prompt=prompt_path.read_text(encoding="utf-8"),
            expected_response=expected_path.read_text(encoding="utf-8"),
            rubric=Rubric.load(rubric_path),
            coverage=coverage,
            source_paths=source_paths,
            provenance=provenance,
            content_hash=package_hash,
        )

    def solver_attachments(self) -> Mapping[str, str]:
        return _text_artifacts(self.solver_attachments_directory)

    def expected_artifacts(self) -> Mapping[str, str]:
        return _text_artifacts(self.expected_artifacts_directory)


def _owned_relative_file(
    directory: Path,
    value: object,
    field: str,
    *,
    owner: str,
    path: Path,
) -> Path:
    relative = PurePosixPath(_string(value, field, path=path))
    if not relative.parts or relative.parts[0] != owner:
        raise ConfigurationError(f"{field} must be owned by {owner}/", path=path)
    return _relative_file(directory, relative.as_posix(), field, path=path)


def _optional_owned_directory(
    directory: Path,
    value: object | None,
    field: str,
    *,
    owner: str,
    path: Path,
) -> Path | None:
    if value is None:
        return None
    relative = PurePosixPath(_string(value, field, path=path))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ConfigurationError(f"{field} must be a safe relative path", path=path)
    if relative.parts[0] != owner:
        raise ConfigurationError(f"{field} must be owned by {owner}/", path=path)
    result = (directory / relative.as_posix()).resolve()
    if directory.resolve() not in result.parents or result.is_symlink() or not result.is_dir():
        raise ResolutionError(f"{field} is missing or unsafe: {relative}")
    return result


def _text_artifacts(root: Path | None) -> Mapping[str, str]:
    if root is None:
        return {}
    result: dict[str, str] = {}
    for artifact in sorted(root.rglob("*")):
        if not artifact.is_file() or artifact.is_symlink():
            continue
        try:
            result[artifact.relative_to(root).as_posix()] = artifact.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ConfigurationError(
                f"case text artifact must be UTF-8: {artifact}"
            ) from error
    return result


def _artifact_inventory(root: Path | None) -> tuple[tuple[str, str], ...]:
    if root is None:
        return ()
    return tuple(
        (path.relative_to(root).as_posix(), sha256_file(path))
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )


def _validate_case_package_files(
    directory: Path,
    *,
    case_file: Path,
    prompt_path: Path,
    solver_attachments: Path | None,
    rubric_path: Path,
    expected_path: Path,
    expected_artifacts: Path | None,
) -> None:
    allowed = {
        case_file.resolve(),
        prompt_path.resolve(),
        rubric_path.resolve(),
        expected_path.resolve(),
        *(
            path.resolve()
            for root in (solver_attachments, expected_artifacts)
            if root is not None
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        ),
    }
    unexpected: list[str] = []
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise IntegrityError(f"case package contains a symlink: {path}")
        if path.is_file() and path.resolve() not in allowed:
            unexpected.append(path.relative_to(directory).as_posix())
    if unexpected:
        raise ConfigurationError(
            f"case package contains undeclared files: {sorted(unexpected)}",
            path=case_file,
        )


def _safe_source_path(value: object, *, path: Path) -> str:
    result = _string(value, "case.source_paths[]", path=path)
    candidate = PurePosixPath(result)
    if candidate.is_absolute() or ".." in candidate.parts or "\x00" in result:
        raise ConfigurationError("case source path must be safe and relative", path=path)
    return candidate.as_posix()


@dataclass(frozen=True)
class CaseBank:
    cases: tuple[CaseDefinition, ...]
    content_hash: str

    @classmethod
    def discover(
        cls,
        root: Path,
        *,
        verify_provenance: bool = True,
        require_cases: bool = True,
    ) -> CaseBank:
        resolved = root.resolve()
        if not resolved.is_dir() or resolved.is_symlink():
            raise ResolutionError(f"cases directory does not exist or is unsafe: {root}")
        cases = tuple(
            CaseDefinition.load(path.parent, verify_provenance=verify_provenance)
            for path in sorted(resolved.rglob("case.yaml"))
            if path.is_file() and not path.is_symlink()
        )
        if not cases and require_cases:
            raise ResolutionError("cases directory contains no case.yaml files")
        ids = [case.id for case in cases]
        if len(ids) != len(set(ids)):
            duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
            raise IntegrityError(f"duplicate case ids: {duplicates}")
        return cls(cases, content_hash(tuple((case.id, case.content_hash) for case in cases)))


@dataclass(frozen=True)
class SplitAssignment:
    group: str
    split: str


@dataclass(frozen=True)
class SplitManifest:
    assignments: tuple[SplitAssignment, ...]
    content_hash: str

    @classmethod
    def load(cls, path: Path, case_bank: CaseBank) -> SplitManifest:
        payload = _yaml(path)
        _reject_unknown(payload, {"schema_version", "groups"}, "splits", path=path)
        if payload.get("schema_version") != 2:
            raise ConfigurationError("split schema_version must be 2", path=path)
        raw = _mapping(payload.get("groups"), "splits.groups", path=path)
        assignments = tuple(
            SplitAssignment(
                _string(group, "split group", path=path),
                _string(split, f"split group {group}", path=path),
            )
            for group, split in sorted(raw.items())
        )
        allowed = {"train", "development", "test", "challenge"}
        if any(item.split not in allowed for item in assignments):
            raise ConfigurationError(
                "split names must be train/development/test/challenge", path=path
            )
        owners = {item.group: item.split for item in assignments}
        groups = {case.group for case in case_bank.cases}
        missing = sorted(groups - owners.keys())
        extra = sorted(owners.keys() - groups)
        if missing or extra:
            raise IntegrityError(
                f"split groups differ from case groups; missing={missing}, extra={extra}"
            )
        return cls(assignments, content_hash(assignments))

    def split_for(self, case: CaseDefinition) -> str:
        return dict((item.group, item.split) for item in self.assignments)[case.group]


def deterministic_group_split(
    group: str,
    *,
    seed: str,
    train: float,
    development: float,
    test: float,
) -> str:
    digest = hashlib.sha256(f"{seed}:{group}".encode()).digest()
    point = int.from_bytes(digest[:8], "big") / 2**64
    if point < train:
        return "train"
    if point < train + development:
        return "development"
    if point < train + development + test:
        return "test"
    return "challenge"


def validate_deterministic_splits(
    split_manifest: SplitManifest,
    *,
    seed: str,
    train: float,
    development: float,
    test: float,
) -> None:
    mismatches = {
        item.group: {
            "actual": item.split,
            "expected": deterministic_group_split(
                item.group,
                seed=seed,
                train=train,
                development=development,
                test=test,
            ),
        }
        for item in split_manifest.assignments
        if item.split
        != deterministic_group_split(
            item.group,
            seed=seed,
            train=train,
            development=development,
            test=test,
        )
    }
    if mismatches:
        raise IntegrityError(
            f"split assignments do not match the configured deterministic policy: {mismatches}"
        )


@dataclass(frozen=True)
class CriterionScore:
    id: str
    score: float


@dataclass(frozen=True)
class JudgeVote:
    criterion_scores: tuple[CriterionScore, ...]
    hard_failures: tuple[str, ...]
    feedback: str
    call_ids: tuple[str, ...] = ()


class JudgeProgram(Protocol):
    def __call__(
        self,
        *,
        task: str,
        skill_context: str,
        rubric: str,
        expected_response: str,
        expected_artifacts: str,
        candidate_response: str,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class EvaluationRecord:
    schema_version: int
    status: str
    case_id: str
    judge_model_hash: str
    judge_program_hash: str
    votes: tuple[JudgeVote, ...]
    criterion_scores: tuple[CriterionScore, ...]
    hard_failures: tuple[str, ...]
    score: float
    passing_score: float
    passed: bool
    feedback: str
    content_hash: str


class LlmJudge:
    """Validate and aggregate semantic judgments produced exclusively by an LLM."""

    def __init__(
        self,
        program: JudgeProgram,
        *,
        model_hash: str,
        program_hash: str,
        repetitions: int,
    ) -> None:
        if repetitions < 1:
            raise ConfigurationError("judge repetitions must be positive")
        self.program = program
        self.model_hash = model_hash
        self.program_hash = program_hash
        self.repetitions = repetitions

    def evaluate(
        self, case: CaseDefinition, candidate_response: str, *, skill_context: str
    ) -> EvaluationRecord:
        votes = tuple(
            self._vote(
                case,
                self.program(
                    task=case.prompt,
                    skill_context=skill_context,
                    rubric=case.rubric.prompt_text(),
                    expected_response=case.expected_response,
                    expected_artifacts=json.dumps(
                        case.expected_artifacts(), ensure_ascii=False, sort_keys=True
                    ),
                    candidate_response=candidate_response,
                ),
            )
            for _ in range(self.repetitions)
        )
        scores = tuple(
            CriterionScore(
                criterion.id,
                statistics.median(
                    next(score.score for score in vote.criterion_scores if score.id == criterion.id)
                    for vote in votes
                ),
            )
            for criterion in case.rubric.criteria
        )
        hard_failures = tuple(
            item.id
            for item in case.rubric.hard_failures
            if sum(item.id in vote.hard_failures for vote in votes) > len(votes) / 2
        )
        score = sum(
            item.weight * next(value.score for value in scores if value.id == item.id)
            for item in case.rubric.criteria
        )
        feedback = "\n\n".join(vote.feedback for vote in votes)
        identity = {
            "schema_version": 2,
            "status": "evaluated",
            "case_id": case.id,
            "judge_model_hash": self.model_hash,
            "judge_program_hash": self.program_hash,
            "votes": votes,
            "criterion_scores": scores,
            "hard_failures": hard_failures,
            "score": score,
            "passing_score": case.rubric.passing_score,
            "passed": score >= case.rubric.passing_score and not hard_failures,
            "feedback": feedback,
        }
        return EvaluationRecord(**identity, content_hash=content_hash(identity))

    @staticmethod
    def _vote(case: CaseDefinition, output: Mapping[str, object]) -> JudgeVote:
        raw_scores = output.get("criterion_scores")
        if not isinstance(raw_scores, Mapping):
            raise IntegrityError("judge criterion_scores must be a mapping")
        expected_ids = {item.id for item in case.rubric.criteria}
        actual_ids = {str(key) for key in raw_scores}
        if actual_ids != expected_ids:
            raise IntegrityError(
                f"judge criterion ids must match rubric exactly: {actual_ids} != {expected_ids}"
            )
        scores: list[CriterionScore] = []
        for criterion in case.rubric.criteria:
            score = raw_scores[criterion.id]
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not 0 <= float(score) <= 1
            ):
                raise IntegrityError(f"judge score for {criterion.id!r} must be in [0, 1]")
            scores.append(CriterionScore(criterion.id, float(score)))
        raw_failures = output.get("hard_failures", [])
        if not isinstance(raw_failures, Sequence) or isinstance(raw_failures, (str, bytes)):
            raise IntegrityError("judge hard_failures must be a list")
        failures = tuple(str(item) for item in raw_failures)
        known_failures = {item.id for item in case.rubric.hard_failures}
        if len(failures) != len(set(failures)) or not set(failures) <= known_failures:
            raise IntegrityError("judge returned duplicate or unknown hard-failure ids")
        feedback = output.get("feedback")
        if not isinstance(feedback, str) or not feedback.strip():
            raise IntegrityError("judge feedback must be a non-empty string")
        raw_call_ids = output.get("call_ids", [])
        if not isinstance(raw_call_ids, Sequence) or isinstance(raw_call_ids, (str, bytes)):
            raise IntegrityError("judge call_ids must be a list")
        return JudgeVote(tuple(scores), failures, feedback.strip(), tuple(map(str, raw_call_ids)))


@dataclass(frozen=True)
class CalibrationFixture:
    id: str
    case_id: str
    candidate_path: Path
    label: str
    minimum_score: float
    maximum_score: float


@dataclass(frozen=True)
class CalibrationCorpus:
    fixtures: tuple[CalibrationFixture, ...]
    content_hash: str

    @classmethod
    def load(
        cls,
        root: Path,
        case_bank: CaseBank,
        *,
        require_complete: bool = True,
    ) -> CalibrationCorpus:
        manifest = root / "manifest.yaml"
        payload = _yaml(manifest)
        _reject_unknown(payload, {"schema_version", "fixtures"}, "calibration", path=manifest)
        if payload.get("schema_version") != 2:
            raise ConfigurationError("calibration schema_version must be 2", path=manifest)
        fixtures: list[CalibrationFixture] = []
        known_cases = {case.id for case in case_bank.cases}
        for raw in _sequence(payload.get("fixtures"), "fixtures", path=manifest):
            item = _mapping(raw, "fixtures[]", path=manifest)
            _reject_unknown(
                item,
                {"id", "case", "candidate", "label", "score_range"},
                "fixtures[]",
                path=manifest,
            )
            fixture_id = _string(item.get("id"), "fixture.id", path=manifest)
            case_id = _string(item.get("case"), "fixture.case", path=manifest)
            if case_id not in known_cases:
                raise ConfigurationError(
                    f"calibration fixture references unknown case {case_id!r}", path=manifest
                )
            score_range = _sequence(item.get("score_range"), "score_range", path=manifest)
            if len(score_range) != 2 or any(
                not isinstance(value, (int, float)) or isinstance(value, bool)
                for value in score_range
            ):
                raise ConfigurationError("score_range must contain two numbers", path=manifest)
            minimum, maximum = map(float, score_range)
            if not 0 <= minimum <= maximum <= 1:
                raise ConfigurationError("score_range must be within [0, 1]", path=manifest)
            fixtures.append(
                CalibrationFixture(
                    fixture_id,
                    case_id,
                    _relative_file(root, item.get("candidate"), "fixture.candidate", path=manifest),
                    _string(item.get("label"), "fixture.label", path=manifest),
                    minimum,
                    maximum,
                )
            )
        required = {"strong", "partial", "incorrect", "contradictory", "adversarial"}
        labels = {item.label for item in fixtures}
        fixture_ids = [item.id for item in fixtures]
        if len(fixture_ids) != len(set(fixture_ids)):
            raise ConfigurationError("calibration fixture ids must be unique", path=manifest)
        unknown_labels = sorted(labels - required)
        if unknown_labels:
            raise ConfigurationError(f"unknown calibration labels: {unknown_labels}", path=manifest)
        if require_complete and not required <= labels:
            raise PreflightError(
                f"judge calibration requires labels {sorted(required)}; missing {sorted(required - labels)}"
            )
        identity = tuple(
            (
                item.id,
                item.case_id,
                item.label,
                item.minimum_score,
                item.maximum_score,
                sha256_file(item.candidate_path),
            )
            for item in fixtures
        )
        return cls(tuple(fixtures), content_hash(identity))


@dataclass(frozen=True)
class CalibrationResult:
    passed: bool
    fixture_scores: Mapping[str, float]
    fixture_passes: Mapping[str, bool]
    corpus_hash: str
    content_hash: str


def calibrate_judge(
    judge: LlmJudge,
    corpus: CalibrationCorpus,
    case_bank: CaseBank,
    skill_context: Callable[[str], str],
) -> CalibrationResult:
    cases = {case.id: case for case in case_bank.cases}
    scores: dict[str, float] = {}
    passes: dict[str, bool] = {}
    for fixture in corpus.fixtures:
        case = cases[fixture.case_id]
        result = judge.evaluate(
            case,
            fixture.candidate_path.read_text(encoding="utf-8"),
            skill_context=skill_context(case.skill),
        )
        scores[fixture.id] = result.score
        passes[fixture.id] = result.passed
    return calibration_result_from_scores(corpus, scores, fixture_passes=passes)


def calibration_result_from_scores(
    corpus: CalibrationCorpus,
    fixture_scores: Mapping[str, float],
    *,
    fixture_passes: Mapping[str, bool] | None = None,
) -> CalibrationResult:
    """Apply private calibration labels and ranges outside the judge model's view."""

    expected_ids = {fixture.id for fixture in corpus.fixtures}
    actual_ids = set(fixture_scores)
    if actual_ids != expected_ids:
        raise IntegrityError(
            "calibration scores must cover every fixture exactly: "
            f"{sorted(actual_ids)} != {sorted(expected_ids)}"
        )
    scores = {fixture_id: float(score) for fixture_id, score in fixture_scores.items()}
    if any(not 0 <= score <= 1 for score in scores.values()):
        raise IntegrityError("calibration scores must be in [0, 1]")
    if fixture_passes is None:
        fixture_passes = {
            fixture.id: scores[fixture.id] >= 0.5 for fixture in corpus.fixtures
        }
    if set(fixture_passes) != expected_ids:
        raise IntegrityError("calibration pass decisions must cover every fixture exactly")
    scores_by_label: dict[str, list[float]] = {}
    passed = True
    for fixture in corpus.fixtures:
        score = scores[fixture.id]
        scores_by_label.setdefault(fixture.label, []).append(score)
        if not fixture.minimum_score <= score <= fixture.maximum_score:
            passed = False
        expected_pass = fixture.label == "strong"
        if fixture_passes[fixture.id] is not expected_pass:
            passed = False
    strong_floor = min(scores_by_label["strong"])
    partial_ceiling = max(scores_by_label["partial"])
    negative_ceiling = max(
        score
        for label in ("incorrect", "contradictory", "adversarial")
        for score in scores_by_label[label]
    )
    passed = passed and strong_floor > partial_ceiling > negative_ceiling
    identity = {
        "passed": passed,
        "fixture_scores": scores,
        "fixture_passes": dict(fixture_passes),
        "corpus_hash": corpus.content_hash,
    }
    return CalibrationResult(**identity, content_hash=content_hash(identity))


def validate_case_bank(
    case_bank: CaseBank,
    *,
    capability_catalog: CapabilityCatalog,
    skill_ids: Sequence[str],
    split_manifest: SplitManifest,
    source_paths: Sequence[str],
    minimum_cases_per_skill: int = 8,
    minimum_leakage_groups_per_skill: int = 4,
) -> dict[str, object]:
    known_skills = set(skill_ids)
    known_sources = set(source_paths)
    unknown_skills = sorted({case.skill for case in case_bank.cases} - known_skills)
    missing_sources = sorted(
        {path for case in case_bank.cases for path in case.source_paths} - known_sources
    )
    if unknown_skills:
        raise IntegrityError(f"cases reference unknown skills: {unknown_skills}")
    if missing_sources:
        raise IntegrityError(f"cases reference paths absent from snapshot: {missing_sources}")
    catalog_skills = {item.skill for item in capability_catalog.skills}
    extra_catalog_skills = sorted(catalog_skills - known_skills)
    if extra_catalog_skills:
        raise IntegrityError(
            f"capability catalog references unknown skills: {extra_catalog_skills}"
        )
    missing_catalog_skills = sorted(known_skills - catalog_skills)
    requirements = {
        item.skill: {capability.id: capability for capability in item.capabilities}
        for item in capability_catalog.skills
    }
    for case in case_bank.cases:
        known_capabilities = requirements.get(case.skill)
        if known_capabilities is None:
            raise IntegrityError(
                f"case {case.id!r} belongs to a skill absent from the capability catalog"
            )
        referenced = (case.coverage.primary, *case.coverage.supporting)
        unknown_capabilities = sorted(set(referenced) - known_capabilities.keys())
        if unknown_capabilities:
            raise IntegrityError(
                f"case {case.id!r} references unknown capabilities: {unknown_capabilities}"
            )
    counts = Counter(split_manifest.split_for(case) for case in case_bank.cases)
    coverage_by_skill = []
    coverage_gaps = []
    capability_gaps = []
    for skill_id in skill_ids:
        cases = tuple(case for case in case_bank.cases if case.skill == skill_id)
        group_count = len({case.group for case in cases})
        primary_counts = Counter(case.coverage.primary for case in cases)
        supporting_counts = Counter(
            capability for case in cases for capability in case.coverage.supporting
        )
        capability_coverage = []
        for requirement in requirements.get(skill_id, {}).values():
            primary_count = primary_counts[requirement.id]
            capability_coverage.append(
                {
                    "capability": requirement.id,
                    "primary_cases": primary_count,
                    "supporting_cases": supporting_counts[requirement.id],
                    "required_primary_cases": requirement.minimum_primary_cases,
                }
            )
            if primary_count < requirement.minimum_primary_cases:
                capability_gaps.append(
                    {
                        "skill": skill_id,
                        "capability": requirement.id,
                        "primary_cases": primary_count,
                        "required_primary_cases": requirement.minimum_primary_cases,
                        "missing_primary_cases": (
                            requirement.minimum_primary_cases - primary_count
                        ),
                    }
                )
        coverage_by_skill.append(
            {
                "skill": skill_id,
                "cases": len(cases),
                "leakage_groups": group_count,
                "capabilities": capability_coverage,
            }
        )
        if len(cases) < minimum_cases_per_skill or group_count < minimum_leakage_groups_per_skill:
            coverage_gaps.append(
                {
                    "skill": skill_id,
                    "cases": len(cases),
                    "required_cases": minimum_cases_per_skill,
                    "leakage_groups": group_count,
                    "required_leakage_groups": minimum_leakage_groups_per_skill,
                }
            )
    return {
        "status": (
            "valid"
            if capability_catalog.status == "complete"
            and not missing_catalog_skills
            and not coverage_gaps
            and not capability_gaps
            else "incomplete"
        ),
        "case_count": len(case_bank.cases),
        "case_bank_hash": case_bank.content_hash,
        "split_hash": split_manifest.content_hash,
        "split_counts": dict(sorted(counts.items())),
        "minimum_cases_per_skill": minimum_cases_per_skill,
        "minimum_leakage_groups_per_skill": minimum_leakage_groups_per_skill,
        "capability_catalog_hash": capability_catalog.content_hash,
        "capability_catalog_status": capability_catalog.status,
        "missing_catalog_skills": missing_catalog_skills,
        "capability_count": sum(len(item.capabilities) for item in capability_catalog.skills),
        "satisfied_capability_count": (
            sum(len(item.capabilities) for item in capability_catalog.skills)
            - len(capability_gaps)
        ),
        "missing_primary_case_count": sum(
            int(item["missing_primary_cases"]) for item in capability_gaps
        ),
        "coverage_by_skill": coverage_by_skill,
        "coverage_gaps": coverage_gaps,
        "capability_gaps": capability_gaps,
        "rubric_coverage": len(case_bank.cases),
        "expected_response_coverage": len(case_bank.cases),
        "authoring_provenance_coverage": len(case_bank.cases),
    }


def validate_case_authoring_provenance(
    case_bank: CaseBank,
    *,
    authoring_instructions_hash: str,
    capability_catalog_hash: str,
    source_snapshot_hash: str,
) -> None:
    """Reject cases authored under a different policy or immutable source set."""

    for case in case_bank.cases:
        if case.provenance.authoring_instructions_hash != authoring_instructions_hash:
            raise IntegrityError(
                f"case {case.id!r} was not authored under the active instructions"
            )
        if case.provenance.capability_catalog_hash != capability_catalog_hash:
            raise IntegrityError(
                f"case {case.id!r} was not classified under the active capability catalog"
            )
        if case.provenance.source_snapshot_hash != source_snapshot_hash:
            raise IntegrityError(
                f"case {case.id!r} was not authored from the active authoring source set"
            )
