from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ms_agent_eval.programs.dspy.engine import (
    create_judge_program,
    create_solver_program,
    program_hash,
)

from .authoring import SnapshotContext, load_snapshot_context
from .errors import ConfigurationError, IntegrityError, PreflightError, ResolutionError
from .evaluation import (
    CalibrationCorpus,
    CapabilityCatalog,
    CaseBank,
    SplitManifest,
    validate_deterministic_splits,
    validate_case_authoring_provenance,
    validate_case_bank,
)
from .hashing import content_hash, json_value, sha256_file
from .models import SnapshotLock, TargetSpecification
from .snapshots import (
    ExternalSnapshotStore,
    SnapshotBuilder,
    extraction_configuration_hash,
)
from .sources import GitHubSourceProvider
from .workspace import (
    AuthoringSourceConfiguration,
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
    authoring_instructions_hash: str
    capability_catalog_hash: str
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
    capability_catalog: CapabilityCatalog
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
        authoring_hash = sha256_file(self.repository.case_authoring_path)
        catalog_hash = self.capability_catalog.content_hash
        solver_hash = program_hash(create_solver_program())
        judge_hash = program_hash(create_judge_program())
        validate_case_authoring_provenance(
            self.case_bank,
            authoring_instructions_hash=authoring_hash,
            capability_catalog_hash=catalog_hash,
            source_snapshot_hash=self.context.snapshot_hash,
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
            "schema_version": 3,
            "id": f"{workspace.id}-{experiment.id}",
            "workspace_id": workspace.id,
            "workspace_hash": workspace.identity_hash,
            "mode": experiment.mode.value,
            "source_snapshot_hash": self.context.snapshot_hash,
            "source_commit": self.snapshot.resolved_commit,
            "case_bank_hash": self.case_bank.content_hash,
            "split_hash": self.splits.content_hash,
            "calibration_hash": self.calibration.content_hash,
            "authoring_instructions_hash": authoring_hash,
            "capability_catalog_hash": catalog_hash,
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
            "schema_version": 3,
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
                "minimum_cases_per_skill": self.validation["minimum_cases_per_skill"],
                "minimum_leakage_groups_per_skill": self.validation[
                    "minimum_leakage_groups_per_skill"
                ],
                "capability_catalog": {
                    "path": self.repository.capability_catalog_path.as_posix(),
                    "content_hash": self.validation["capability_catalog_hash"],
                    "status": self.validation["capability_catalog_status"],
                    "missing_skills": self.validation["missing_catalog_skills"],
                    "capabilities": self.validation["capability_count"],
                    "satisfied_capabilities": self.validation[
                        "satisfied_capability_count"
                    ],
                    "missing_primary_cases": self.validation[
                        "missing_primary_case_count"
                    ],
                },
                "coverage_by_skill": self.validation["coverage_by_skill"],
                "coverage_gaps": self.validation["coverage_gaps"],
                "capability_gaps": self.validation["capability_gaps"],
                "rubric_coverage": len(self.case_bank.cases),
                "expected_response_coverage": len(self.case_bank.cases),
                "authoring_provenance_coverage": len(self.case_bank.cases),
            },
            "case_authoring": {
                "instructions": self.repository.case_authoring_path.as_posix(),
                "instructions_hash": sha256_file(self.repository.case_authoring_path),
                "agent": "codex",
                "authoring_source_set_hash": self.context.snapshot_hash,
                "authoring_sources": _authoring_sources(self.context),
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


def _acquire_target_snapshot(
    target: TargetSpecification,
    *,
    store: ExternalSnapshotStore,
    provider: GitHubSourceProvider,
) -> tuple[SnapshotLock, Path]:
    # Kept as one implementation so the target and every case-authoring reference
    # use identical immutable resolution, cache, and verification semantics.
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


def acquire_snapshot(repository: WorkspaceRepository) -> tuple[SnapshotLock, Path]:
    store = ExternalSnapshotStore(repository.data_root, workspace_root=repository.root)
    return _acquire_target_snapshot(
        repository.target_specification(),
        store=store,
        provider=GitHubSourceProvider(),
    )


def acquire_authoring_snapshots(
    repository: WorkspaceRepository,
) -> tuple[tuple[AuthoringSourceConfiguration, SnapshotLock, Path], ...]:
    store = ExternalSnapshotStore(repository.data_root, workspace_root=repository.root)
    provider = GitHubSourceProvider()
    return tuple(
        (
            configuration,
            *_acquire_target_snapshot(target, store=store, provider=provider),
        )
        for configuration, target in repository.authoring_source_specifications()
    )


def _load_context(
    repository: WorkspaceRepository,
    snapshot: SnapshotLock,
    snapshot_directory: Path,
) -> SnapshotContext:
    return load_snapshot_context(
        snapshot,
        snapshot_directory,
        global_paths=repository.workspace.evaluation.instructions.global_paths,
        authoring_snapshots=acquire_authoring_snapshots(repository),
    )


def _authoring_sources(context: SnapshotContext) -> list[Mapping[str, object]]:
    return [
        {
            "id": source.id,
            "role": source.role,
            "url": source.repository_url,
            "requested_ref": source.requested_ref,
            "resolved_commit": source.resolved_commit,
            "snapshot_hash": source.snapshot_hash,
            "include": list(source.include),
        }
        for source in context.sources
    ]


def _validate_splits(repository: WorkspaceRepository, splits: SplitManifest) -> None:
    policy = repository.workspace.evaluation.splits.policy
    validate_deterministic_splits(
        splits,
        seed=policy.seed,
        train=policy.train,
        development=policy.development,
        test=policy.test,
    )


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
    context = _load_context(repository, snapshot, snapshot_directory)
    capability_catalog = CapabilityCatalog.load(repository.capability_catalog_path)
    case_bank = CaseBank.discover(repository.cases_root)
    validate_case_authoring_provenance(
        case_bank,
        authoring_instructions_hash=sha256_file(repository.case_authoring_path),
        capability_catalog_hash=capability_catalog.content_hash,
        source_snapshot_hash=context.snapshot_hash,
    )
    splits = SplitManifest.load(repository.split_file, case_bank)
    _validate_splits(repository, splits)
    calibration = CalibrationCorpus.load(repository.calibration_root, case_bank)
    validation = validate_case_bank(
        case_bank,
        capability_catalog=capability_catalog,
        skill_ids=tuple(context.skill_contexts),
        split_manifest=splits,
        source_paths=context.source_paths,
        minimum_cases_per_skill=(
            repository.workspace.evaluation.cases.coverage.minimum_cases_per_skill
        ),
        minimum_leakage_groups_per_skill=(
            repository.workspace.evaluation.cases.coverage.minimum_leakage_groups_per_skill
        ),
    )
    if validation["status"] != "valid":
        raise PreflightError(
            "case bank does not satisfy the complete capability catalog and configured "
            "per-skill case/group minima"
        )
    if snapshot.target_specification_hash != repository.target_specification().specification_hash:
        raise IntegrityError("snapshot was not built from the active workspace manifest")
    return CompiledWorkspace(
        repository,
        snapshot,
        snapshot_directory,
        context,
        capability_catalog,
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
    context = _load_context(repository, snapshot, snapshot_directory)
    capability_catalog = CapabilityCatalog.load(repository.capability_catalog_path)
    case_bank = CaseBank.discover(repository.cases_root, require_cases=False)
    validate_case_authoring_provenance(
        case_bank,
        authoring_instructions_hash=sha256_file(repository.case_authoring_path),
        capability_catalog_hash=capability_catalog.content_hash,
        source_snapshot_hash=context.snapshot_hash,
    )
    splits = SplitManifest.load(repository.split_file, case_bank)
    _validate_splits(repository, splits)
    validation = validate_case_bank(
        case_bank,
        capability_catalog=capability_catalog,
        skill_ids=tuple(context.skill_contexts),
        split_manifest=splits,
        source_paths=context.source_paths,
        minimum_cases_per_skill=(
            repository.workspace.evaluation.cases.coverage.minimum_cases_per_skill
        ),
        minimum_leakage_groups_per_skill=(
            repository.workspace.evaluation.cases.coverage.minimum_leakage_groups_per_skill
        ),
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
    if validation["capability_catalog_status"] != "complete":
        blockers.append("capability catalog is still marked draft")
    if validation["missing_catalog_skills"]:
        blockers.append("capability catalog does not cover every configured skill")
    if not case_bank.cases:
        blockers.append("case bank has no authored cases")
    elif validation["coverage_gaps"]:
        blockers.append(
            "case bank does not satisfy the configured per-skill case and "
            "leakage-group minima"
        )
    if validation["capability_gaps"]:
        blockers.append("case bank does not satisfy required primary capability coverage")
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
            capability_catalog,
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
    judge_model = (
        json_value(resolved[first_experiment]["judge"])
        if first_experiment in resolved
        else _unresolved_model(workspace.evaluation.judge.model)
    )
    split_counts = Counter(splits.split_for(case) for case in case_bank.cases)
    return {
        "schema_version": 3,
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
            "minimum_cases_per_skill": validation["minimum_cases_per_skill"],
            "minimum_leakage_groups_per_skill": validation[
                "minimum_leakage_groups_per_skill"
            ],
            "capability_catalog": {
                "path": repository.capability_catalog_path.as_posix(),
                "content_hash": validation["capability_catalog_hash"],
                "status": validation["capability_catalog_status"],
                "missing_skills": validation["missing_catalog_skills"],
                "capabilities": validation["capability_count"],
                "satisfied_capabilities": validation["satisfied_capability_count"],
                "missing_primary_cases": validation["missing_primary_case_count"],
            },
            "coverage_by_skill": validation["coverage_by_skill"],
            "coverage_gaps": validation["coverage_gaps"],
            "capability_gaps": validation["capability_gaps"],
            "rubric_coverage": len(case_bank.cases),
            "expected_response_coverage": len(case_bank.cases),
            "authoring_provenance_coverage": len(case_bank.cases),
        },
        "case_authoring": {
            "instructions": repository.case_authoring_path.as_posix(),
            "instructions_hash": sha256_file(repository.case_authoring_path),
            "agent": "codex",
            "authoring_source_set_hash": context.snapshot_hash,
            "authoring_sources": _authoring_sources(context),
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
