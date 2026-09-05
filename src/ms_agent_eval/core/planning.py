from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ms_agent_eval.programs.dspy.engine import (
    create_case_builder_program,
    create_judge_program,
    create_solver_program,
    program_hash,
)

from .case_builder import SnapshotContext, load_snapshot_context
from .errors import ConfigurationError, IntegrityError, ResolutionError
from .evaluation import (
    CalibrationCorpus,
    CaseBank,
    SplitManifest,
    validate_case_bank,
)
from .hashing import content_hash, json_value
from .models import SnapshotLock
from .snapshots import (
    ExternalSnapshotStore,
    SnapshotBuilder,
    extraction_configuration_hash,
)
from .sources import GitHubSourceProvider
from .workspace import (
    ExperimentConfiguration,
    ExperimentMode,
    ModelConfiguration,
    ResolvedRoleModel,
    WorkspaceRepository,
    resolve_role_models,
)


@dataclass(frozen=True)
class CaseLock:
    id: str
    skill: str
    group: str
    split: str
    content_hash: str


@dataclass(frozen=True)
class ExperimentLock:
    schema_version: int
    id: str
    workspace_id: str
    workspace_hash: str
    mode: str
    source_snapshot_hash: str
    source_commit: str
    case_bank_hash: str
    split_hash: str
    calibration_hash: str
    builder_program_hash: str
    solver_program_hash: str
    judge_program_hash: str
    models: Mapping[str, ResolvedRoleModel]
    cases: tuple[CaseLock, ...]
    configuration: ExperimentConfiguration
    content_hash: str


@dataclass(frozen=True)
class CompiledWorkspace:
    repository: WorkspaceRepository
    snapshot: SnapshotLock
    snapshot_directory: Path
    context: SnapshotContext
    case_bank: CaseBank
    splits: SplitManifest
    calibration: CalibrationCorpus
    validation: Mapping[str, object]

    def experiment_lock(
        self,
        experiment_id: str,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> ExperimentLock:
        workspace = self.repository.workspace
        try:
            experiment = workspace.experiments[experiment_id]
        except KeyError as error:
            raise ResolutionError(f"unknown experiment: {experiment_id}") from error
        models = resolve_role_models(workspace, experiment_id, environment)
        builder_hash = program_hash(
            create_case_builder_program(self.repository.case_authoring_instructions())
        )
        solver_hash = program_hash(create_solver_program())
        judge_hash = program_hash(create_judge_program())
        for case in self.case_bank.cases:
            if case.provenance.builder_model_hash != models["case_builder"].content_hash:
                raise IntegrityError(
                    f"case {case.id!r} was not built by the active case-builder model"
                )
            if case.provenance.builder_program_hash != builder_hash:
                raise IntegrityError(
                    f"case {case.id!r} was not built by the active DSPy builder program"
                )
            if case.provenance.source_snapshot_hash != self.snapshot.content_hash:
                raise IntegrityError(
                    f"case {case.id!r} was not built from the active source snapshot"
                )
        cases = tuple(
            CaseLock(
                case.id,
                case.skill,
                case.group,
                self.splits.split_for(case),
                case.content_hash,
            )
            for case in self.case_bank.cases
        )
        identity = {
            "schema_version": 2,
            "id": f"{workspace.id}-{experiment.id}",
            "workspace_id": workspace.id,
            "workspace_hash": workspace.identity_hash,
            "mode": experiment.mode.value,
            "source_snapshot_hash": self.snapshot.content_hash,
            "source_commit": self.snapshot.resolved_commit,
            "case_bank_hash": self.case_bank.content_hash,
            "split_hash": self.splits.content_hash,
            "calibration_hash": self.calibration.content_hash,
            "builder_program_hash": builder_hash,
            "solver_program_hash": solver_hash,
            "judge_program_hash": judge_hash,
            "models": models,
            "cases": cases,
            "configuration": experiment,
        }
        return ExperimentLock(**identity, content_hash=content_hash(identity))

    def inspect(
        self,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        workspace = self.repository.workspace
        locks = {
            experiment_id: self.experiment_lock(experiment_id, environment=environment)
            for experiment_id in workspace.experiments
        }
        counts = Counter(self.splits.split_for(case) for case in self.case_bank.cases)
        return {
            "schema_version": 2,
            "workspace": {
                "id": workspace.id,
                "manifest": self.repository.workspace_file.as_posix(),
                "data_root": self.repository.data_root.as_posix(),
                "content_hash": workspace.identity_hash,
            },
            "repository": {
                "url": self.snapshot.repository_url,
                "requested_ref": self.snapshot.requested_ref.value,
                "resolved_commit": self.snapshot.resolved_commit,
                "snapshot_hash": self.snapshot.content_hash,
                "snapshot_directory": self.snapshot_directory.as_posix(),
            },
            "instructions": {
                "global_files": [
                    item.source_path
                    for item in self.snapshot.files
                    if item.source_path in set(workspace.evaluation.instructions.global_paths)
                ],
                "skills": [
                    {
                        "id": item.unit_id,
                        "path": item.source_path,
                        "content_hash": item.content_hash,
                    }
                    for item in self.snapshot.units
                ],
                "skill_count": len(self.snapshot.units),
            },
            "cases": {
                "count": len(self.case_bank.cases),
                "content_hash": self.case_bank.content_hash,
                "split_counts": dict(sorted(counts.items())),
                "split_hash": self.splits.content_hash,
                "rubric_coverage": len(self.case_bank.cases),
                "expected_response_coverage": len(self.case_bank.cases),
                "builder_provenance_coverage": len(self.case_bank.cases),
            },
            "case_builder": {
                "instructions": self.repository.case_authoring_path.as_posix(),
                "program_hash": program_hash(
                    create_case_builder_program(
                        self.repository.case_authoring_instructions()
                    )
                ),
                "model": json_value(next(iter(locks.values())).models["case_builder"]),
                "draft_count": _draft_count(self.repository.data_root),
            },
            "judge": {
                "program_hash": program_hash(create_judge_program()),
                "model": json_value(next(iter(locks.values())).models["judge"]),
                "calibration": {
                    "fixture_count": len(self.calibration.fixtures),
                    "content_hash": self.calibration.content_hash,
                    "status": "configured",
                },
            },
            "experiments": {
                key: {
                    "mode": lock.mode,
                    "lock_id": lock.id,
                    "lock_hash": lock.content_hash,
                    "solver_program_hash": lock.solver_program_hash,
                    "solver_model": json_value(lock.models["solver"]),
                    "runtime": json_value(workspace.runtime(key)),
                    "projected_case_runs": len(lock.cases),
                }
                for key, lock in locks.items()
            },
        }


def _draft_count(data_root: Path) -> int:
    root = data_root / "case-drafts"
    if not root.is_dir():
        return 0
    return sum(path.is_dir() and not path.is_symlink() for path in root.iterdir())


def acquire_snapshot(repository: WorkspaceRepository) -> tuple[SnapshotLock, Path]:
    store = ExternalSnapshotStore(repository.data_root, workspace_root=repository.root)
    target = repository.target_specification()
    provider = GitHubSourceProvider()
    resolved = provider.resolve(target.source)
    for lock_file in sorted(store.snapshot_root.glob("*/snapshot.lock.json")):
        try:
            payload = json.loads(lock_file.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                continue
            lock = SnapshotLock.from_mapping(payload)
        except (OSError, ValueError, ConfigurationError):
            continue
        if (
            lock.repository_url == resolved.repository_url_canonical
            and lock.requested_ref == target.source.ref
            and lock.resolved_commit == resolved.resolved_commit
            and lock.target_specification_hash == target.specification_hash
            and lock.extraction_configuration_hash == extraction_configuration_hash(target)
        ):
            store.verify(lock)
            return lock, store.directory(lock)
    lock = SnapshotBuilder(provider, store).create_resolved(target, resolved)
    return lock, store.directory(lock)


def compile_workspace(
    repository: WorkspaceRepository,
    *,
    snapshot: SnapshotLock | None = None,
    snapshot_directory: Path | None = None,
) -> CompiledWorkspace:
    if (snapshot is None) != (snapshot_directory is None):
        raise IntegrityError("snapshot and snapshot_directory must be supplied together")
    if snapshot is None or snapshot_directory is None:
        snapshot, snapshot_directory = acquire_snapshot(repository)
    context = load_snapshot_context(
        snapshot,
        snapshot_directory,
        global_paths=repository.workspace.evaluation.instructions.global_paths,
    )
    case_bank = CaseBank.discover(repository.cases_root)
    splits = SplitManifest.load(repository.split_file, case_bank)
    calibration = CalibrationCorpus.load(repository.calibration_root, case_bank)
    validation = validate_case_bank(
        case_bank,
        skill_ids=tuple(context.skill_contexts),
        split_manifest=splits,
        source_paths=context.source_paths,
    )
    if snapshot.target_specification_hash != repository.target_specification().specification_hash:
        raise IntegrityError("snapshot was not built from the active workspace manifest")
    return CompiledWorkspace(
        repository,
        snapshot,
        snapshot_directory,
        context,
        case_bank,
        splits,
        calibration,
        validation,
    )


def inspect_workspace(
    repository: WorkspaceRepository,
    *,
    environment: Mapping[str, str] | None = None,
    snapshot: SnapshotLock | None = None,
    snapshot_directory: Path | None = None,
) -> Mapping[str, object]:
    """Inspect a workspace during bootstrap without weakening scored-run preflight."""

    if (snapshot is None) != (snapshot_directory is None):
        raise IntegrityError("snapshot and snapshot_directory must be supplied together")
    if snapshot is None or snapshot_directory is None:
        snapshot, snapshot_directory = acquire_snapshot(repository)
    if snapshot.target_specification_hash != repository.target_specification().specification_hash:
        raise IntegrityError("snapshot was not built from the active workspace manifest")
    context = load_snapshot_context(
        snapshot,
        snapshot_directory,
        global_paths=repository.workspace.evaluation.instructions.global_paths,
    )
    case_bank = CaseBank.discover(repository.cases_root, require_cases=False)
    splits = SplitManifest.load(repository.split_file, case_bank)
    validation = validate_case_bank(
        case_bank,
        skill_ids=tuple(context.skill_contexts),
        split_manifest=splits,
        source_paths=context.source_paths,
    )
    calibration = CalibrationCorpus.load(
        repository.calibration_root,
        case_bank,
        require_complete=False,
    )
    required_labels = {"strong", "partial", "incorrect", "contradictory", "adversarial"}
    calibration_labels = {item.label for item in calibration.fixtures}
    missing_labels = sorted(required_labels - calibration_labels)

    values = environment if environment is not None else repository.environment()
    resolved: dict[str, Mapping[str, ResolvedRoleModel]] = {}
    unresolved_models: dict[str, str] = {}
    for experiment_id in repository.workspace.experiments:
        try:
            resolved[experiment_id] = resolve_role_models(
                repository.workspace, experiment_id, values
            )
        except ResolutionError as error:
            unresolved_models[experiment_id] = str(error)

    blockers: list[str] = []
    if not case_bank.cases:
        blockers.append("case bank has no promoted builder-authored cases")
    if missing_labels:
        blockers.append("judge calibration is missing labels: " + ", ".join(missing_labels))
    if unresolved_models:
        blockers.append("one or more LLM role models are unresolved")

    locks: dict[str, ExperimentLock] = {}
    if not blockers:
        compiled = CompiledWorkspace(
            repository,
            snapshot,
            snapshot_directory,
            context,
            case_bank,
            splits,
            calibration,
            validation,
        )
        locks = {
            experiment_id: compiled.experiment_lock(experiment_id, environment=values)
            for experiment_id in repository.workspace.experiments
        }

    workspace = repository.workspace
    first_experiment = next(iter(workspace.experiments))
    case_builder_model = (
        json_value(resolved[first_experiment]["case_builder"])
        if first_experiment in resolved
        else _unresolved_model(workspace.evaluation.case_builder.model)
    )
    judge_model = (
        json_value(resolved[first_experiment]["judge"])
        if first_experiment in resolved
        else _unresolved_model(workspace.evaluation.judge.model)
    )
    split_counts = Counter(splits.split_for(case) for case in case_bank.cases)
    return {
        "schema_version": 2,
        "status": "ready" if not blockers else "incomplete",
        "ready_for_scored_run": not blockers,
        "blockers": blockers,
        "workspace": {
            "id": workspace.id,
            "manifest": repository.workspace_file.as_posix(),
            "data_root": repository.data_root.as_posix(),
            "content_hash": workspace.identity_hash,
        },
        "repository": {
            "url": snapshot.repository_url,
            "requested_ref": snapshot.requested_ref.value,
            "resolved_commit": snapshot.resolved_commit,
            "snapshot_hash": snapshot.content_hash,
            "snapshot_directory": snapshot_directory.as_posix(),
        },
        "instructions": {
            "global_files": [
                item.source_path
                for item in snapshot.files
                if item.source_path in set(workspace.evaluation.instructions.global_paths)
            ],
            "skills": [
                {
                    "id": item.unit_id,
                    "path": item.source_path,
                    "content_hash": item.content_hash,
                }
                for item in snapshot.units
            ],
            "skill_count": len(snapshot.units),
        },
        "cases": {
            "count": len(case_bank.cases),
            "content_hash": case_bank.content_hash,
            "split_counts": dict(sorted(split_counts.items())),
            "split_hash": splits.content_hash,
            "rubric_coverage": len(case_bank.cases),
            "expected_response_coverage": len(case_bank.cases),
            "builder_provenance_coverage": len(case_bank.cases),
        },
        "case_builder": {
            "instructions": repository.case_authoring_path.as_posix(),
            "program_hash": program_hash(
                create_case_builder_program(repository.case_authoring_instructions())
            ),
            "model": case_builder_model,
            "draft_count": _draft_count(repository.data_root),
        },
        "judge": {
            "program_hash": program_hash(create_judge_program()),
            "model": judge_model,
            "calibration": {
                "fixture_count": len(calibration.fixtures),
                "labels": sorted(calibration_labels),
                "missing_labels": missing_labels,
                "content_hash": calibration.content_hash,
                "status": "configured" if not missing_labels else "incomplete",
            },
        },
        "experiments": {
            experiment_id: {
                "mode": experiment.mode.value,
                "lock_id": locks[experiment_id].id if experiment_id in locks else None,
                "lock_hash": (
                    locks[experiment_id].content_hash if experiment_id in locks else None
                ),
                "solver_program_hash": program_hash(create_solver_program()),
                "solver_model": (
                    json_value(resolved[experiment_id]["solver"])
                    if experiment_id in resolved
                    else _unresolved_model(workspace.solver(experiment_id).model)
                ),
                "model_resolution_error": unresolved_models.get(experiment_id),
                "runtime": json_value(workspace.runtime(experiment_id)),
                "projected_case_runs": (
                    len(case_bank.cases) * experiment.repetitions
                    if experiment.mode is ExperimentMode.EVALUATE
                    else sum(splits.split_for(case) == "test" for case in case_bank.cases)
                ),
            }
            for experiment_id, experiment in workspace.experiments.items()
        },
    }


def _unresolved_model(configuration: ModelConfiguration) -> Mapping[str, object]:
    return {
        "status": "unresolved",
        "provider": configuration.provider,
        "name": configuration.model,
        "base_url": configuration.base_url,
        "api_key_env": configuration.api_key_env,
        "parameters": dict(configuration.parameters),
    }


def lock_as_dict(lock: ExperimentLock) -> dict[str, object]:
    value = json_value(lock)
    assert isinstance(value, dict)
    return value
