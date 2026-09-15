from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ms_agent_eval.programs.dspy.engine import (
    create_judge_program,
    create_solver_program,
    program_hash,
)

from .authoring import SnapshotContext, load_snapshot_context
from .errors import ConfigurationError, IntegrityError, PreflightError, ResolutionError
from .evaluation import (
    JudgeValidationCorpus,
    CapabilityCatalog,
    CaseBank,
    SplitManifest,
    deterministic_group_training_partitions,
    deterministic_group_validation_split,
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
    CoproConfiguration,
    ExperimentConfiguration,
    ExperimentMode,
    ModelConfiguration,
    ResolvedRoleModel,
    WorkspaceRepository,
    resolve_role_models,
)


def _optimization_preflight(
    repository: WorkspaceRepository,
    context: SnapshotContext,
    case_bank: CaseBank,
    splits: SplitManifest,
) -> Mapping[str, object]:
    workspace = repository.workspace
    plans: dict[str, object] = {}
    train = tuple(case for case in case_bank.cases if splits.split_for(case) == "train")
    test = tuple(case for case in case_bank.cases if splits.split_for(case) == "test")
    judge_repetitions = workspace.evaluation.judge.repetitions

    def test_solver_repetitions(experiment: ExperimentConfiguration) -> int:
        reuse = workspace.stages["solve"].reuse
        return reuse.samples if reuse.policy.value == "replicate" else experiment.repetitions

    def output_limit(experiment_id: str, role: str) -> int | None:
        model = resolve_role_models(workspace, experiment_id).get(role)
        if model is None:
            return None
        parameters = model.parameters
        value = parameters.get("max_tokens", parameters.get("max_completion_tokens"))
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def validate_budget(
        experiment_id: str,
        strategy: str,
        required_calls: Mapping[str, int],
        configured_budget: Mapping[str, object],
    ) -> Mapping[str, object]:
        stage_by_role = {"optimizer": "optimize", "solver": "solve", "judge": "judge"}
        configured_calls = {
            role: int(getattr(configured_budget[role], "model_calls")) for role in required_calls
        }
        configured_tokens = {
            role: int(getattr(configured_budget[role], "tokens")) for role in required_calls
        }
        output_tokens = {
            role: (
                None
                if output_limit(experiment_id, role) is None
                else required_calls[role] * int(output_limit(experiment_id, role))
            )
            for role in required_calls
        }
        short_calls = {
            role: {"required": required_calls[role], "configured": configured_calls[role]}
            for role in required_calls
            if configured_calls[role] < required_calls[role]
        }
        short_tokens = {
            role: {
                "required_maximum_output_tokens": output_tokens[role],
                "configured": configured_tokens[role],
            }
            for role in required_calls
            if (
                output_tokens[role] is not None
                and configured_tokens[role] < int(output_tokens[role])
            )
            or configured_tokens[role] < workspace.stages[stage_by_role[role]].budget.tokens
        }
        if short_calls or short_tokens:
            raise PreflightError(
                f"{strategy} experiment {experiment_id!r} has insufficient hard budget: "
                f"model_calls={short_calls}, tokens={short_tokens}"
            )
        return {
            "configured_model_calls": configured_calls,
            "configured_tokens": configured_tokens,
            "planned_maximum_output_tokens": output_tokens,
        }

    for experiment_id, experiment in workspace.experiments.items():
        if experiment.mode is not ExperimentMode.OPTIMIZE:
            continue
        optimizer = experiment.optimizer
        budget = experiment.budget
        validation = experiment.validation
        assert optimizer is not None and budget is not None and validation is not None
        validation_assignments = deterministic_group_validation_split(
            train,
            fraction=validation.fraction,
            seed=validation.seed,
        )
        validation_by_group = {item.group: item.split for item in validation_assignments}
        fitting = tuple(case for case in train if validation_by_group[case.group] == "training")
        held_out_validation = tuple(
            case for case in train if validation_by_group[case.group] == "validation"
        )
        if isinstance(optimizer, CoproConfiguration):
            skills = (
                sorted(context.skill_contexts)
                if optimizer.skills == "all"
                else list(optimizer.skills)
            )
            unknown = sorted(set(skills) - set(context.skill_contexts))
            if unknown:
                raise PreflightError(
                    f"COPRO experiment {experiment_id!r} targets unknown skills: {unknown}"
                )
            groups_by_skill = {
                skill: sorted({case.group for case in fitting if case.skill == skill})
                for skill in skills
            }
            insufficient = {
                skill: groups for skill, groups in groups_by_skill.items() if not groups
            }
            if insufficient:
                detail = ", ".join(
                    f"{skill}={len(groups)} groups"
                    for skill, groups in sorted(insufficient.items())
                )
                raise PreflightError(
                    f"COPRO experiment {experiment_id!r} has no optimization-training "
                    f"groups for selected skills: {detail}"
                )
            missing_validation = sorted(
                skill
                for skill in skills
                if not any(case.skill == skill for case in held_out_validation)
            )
            if missing_validation:
                raise PreflightError(
                    f"COPRO experiment {experiment_id!r} has no validation cases for "
                    f"selected skills: {missing_validation}"
                )
            partition_assignments = deterministic_group_training_partitions(
                fitting,
                partitions=optimizer.partitions,
                seed=validation.seed,
            )
            partition_by_group = {item.group: item.partition for item in partition_assignments}
            candidate_evaluations = 0
            proposal_calls = 0
            for partition in range(optimizer.partitions):
                partition_cases = tuple(
                    case for case in fitting if partition_by_group[case.group] == partition
                )
                active_skills = [
                    skill
                    for skill in skills
                    if any(case.skill == skill for case in partition_cases)
                ]
                targets = int(optimizer.optimize_prompt) + len(active_skills)
                proposal_calls += optimizer.depth * targets
                validation_evaluations = (
                    len(held_out_validation) if optimizer.optimize_prompt else 0
                ) + sum(
                    sum(case.skill == skill for case in held_out_validation)
                    for skill in active_skills
                )
                candidate_evaluations += (
                    optimizer.depth * optimizer.breadth * validation_evaluations
                )
            final_test_calls = 2 * len(test) * test_solver_repetitions(experiment)
            solver_calls = candidate_evaluations + final_test_calls
            judge_calls = solver_calls * judge_repetitions
            required = {
                "optimizer": proposal_calls,
                "solver": solver_calls,
                "judge": judge_calls,
            }
            assert budget.optimizer is not None
            budget_plan = validate_budget(
                experiment_id,
                "COPRO",
                required,
                {
                    "optimizer": budget.optimizer,
                    "solver": budget.solver,
                    "judge": budget.judge,
                },
            )
            plans[experiment_id] = {
                "strategy": "copro",
                "targets": {"prompt": optimizer.optimize_prompt, "skills": skills},
                "validation": {
                    "strategy": validation.strategy,
                    "fraction": validation.fraction,
                    "case_count": len(held_out_validation),
                    "assignment_hash": content_hash(validation_assignments),
                },
                "training_case_count": len(fitting),
                "training_partitions": optimizer.partitions,
                "breadth": optimizer.breadth,
                "depth": optimizer.depth,
                "planned_upper_bound": required,
                **budget_plan,
                "training_groups_by_skill": {
                    skill: len(groups) for skill, groups in groups_by_skill.items()
                },
            }
        else:
            candidates = len(optimizer.k_values) + 1
            solver_calls = candidates * len(held_out_validation) + len(
                test
            ) * test_solver_repetitions(experiment)
            judge_calls = (solver_calls) * judge_repetitions
            required = {"solver": solver_calls, "judge": judge_calls}
            budget_plan = validate_budget(
                experiment_id,
                "labeled_few_shot",
                required,
                {"solver": budget.solver, "judge": budget.judge},
            )
            plans[experiment_id] = {
                "strategy": "labeled_few_shot",
                "validation": {
                    "strategy": validation.strategy,
                    "fraction": validation.fraction,
                    "case_count": len(held_out_validation),
                    "assignment_hash": content_hash(validation_assignments),
                },
                "training_case_count": len(fitting),
                "candidates": candidates,
                "planned_upper_bound": required,
                **budget_plan,
            }
    return plans


@dataclass(frozen=True)
class CaseLock:
    id: str
    skill: str
    group: str
    split: str
    solver_view_hash: str
    judge_view_hash: str
    metadata_hash: str
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
    judge_validation_hash: str
    authoring_instructions_hash: str
    capability_catalog_hash: str
    solver_program_hash: str
    judge_program_hash: str
    benchmark_revision_id: str
    comparison_group_id: str
    comparison_id: str
    arm_id: str
    effective_configuration: Mapping[str, object]
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
    judge_validation: JudgeValidationCorpus
    validation: Mapping[str, object]
    lock_overrides: Mapping[str, ExperimentLock] = field(default_factory=dict, repr=False)

    def experiment_lock(
        self,
        experiment_id: str,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> ExperimentLock:
        override = self.lock_overrides.get(experiment_id)
        if override is not None:
            return override
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
                case.solver_view_hash,
                case.judge_view_hash,
                case.metadata_hash,
                case.content_hash,
            )
            for case in self.case_bank.cases
        )
        referenced_stage_ids = (
            "author",
            "validate_judge",
            *(("optimize",) if isinstance(experiment.optimizer, CoproConfiguration) else ()),
            "solve",
            "judge",
            "fit",
            "select",
            "aggregate",
        )
        referenced_executor_ids = {item.executor_id for item in models.values()} | {
            stage.executor
            for stage_id, stage in workspace.stages.items()
            if stage_id in referenced_stage_ids and stage.executor is not None
        }
        referenced_model_ids = {
            experiment.roles.solver,
            experiment.roles.judge,
            *(
                tuple([experiment.roles.optimizer])
                if experiment.roles.optimizer is not None
                else ()
            ),
            *(
                tuple([workspace.stages["author"].model])
                if workspace.stages["author"].model is not None
                else ()
            ),
        }
        effective_configuration = {
            "experiment": experiment,
            "base_experiment": (
                workspace.experiments[experiment.based_on]
                if experiment.based_on is not None
                else None
            ),
            "runtime": workspace.runtime(experiment_id),
            "judge": workspace.evaluation.judge,
            "stages": {stage_id: workspace.stages[stage_id] for stage_id in referenced_stage_ids},
            "executors": {
                executor_id: workspace.executors[executor_id]
                for executor_id in sorted(referenced_executor_ids)
            },
            "deployments": {
                model.deployment_id: workspace.deployments[model.deployment_id]
                for model in models.values()
            },
            "models": {
                model_id: workspace.models[model_id] for model_id in sorted(referenced_model_ids)
            },
        }
        benchmark_revision_id = content_hash(
            {
                "schema_version": 1,
                "source_snapshot_hash": self.context.snapshot_hash,
                "case_bank_hash": self.case_bank.content_hash,
                "split_hash": self.splits.content_hash,
                "judge_validation_hash": self.judge_validation.content_hash,
                "authoring_instructions_hash": authoring_hash,
                "capability_catalog_hash": catalog_hash,
                "solver_program_hash": solver_hash,
                "judge_program_hash": judge_hash,
                "cases": cases,
                "runtime": workspace.runtime(experiment_id),
                "judge_policy": workspace.evaluation.judge,
                "aggregation_implementation": "run-summary-v4",
            }
        )
        comparison_group_id = content_hash(
            {
                "schema_version": 1,
                "benchmark_revision_id": benchmark_revision_id,
                "judge_model": models["judge"].content_hash,
                "judge_program_hash": judge_hash,
                "judge_repetitions": workspace.evaluation.judge.repetitions,
            }
        )
        arm_id = content_hash(
            {
                "schema_version": 1,
                "comparison_group_id": comparison_group_id,
                "roles": experiment.roles,
                "models": models,
                "mode": experiment.mode.value,
                "optimizer": experiment.optimizer,
                "solver_repetitions": experiment.repetitions,
            }
        )
        comparison_id = next(
            (
                comparison.id
                for comparison in workspace.comparisons.values()
                if experiment_id
                in (
                    *comparison.baseline_experiments,
                    *comparison.optimization_experiments,
                )
            ),
            "all-compatible-arms",
        )
        identity = {
            "schema_version": 8,
            "id": f"{workspace.id}-{experiment.id}",
            "workspace_id": workspace.id,
            "mode": experiment.mode.value,
            "source_snapshot_hash": self.context.snapshot_hash,
            "source_commit": self.snapshot.resolved_commit,
            "case_bank_hash": self.case_bank.content_hash,
            "split_hash": self.splits.content_hash,
            "judge_validation_hash": self.judge_validation.content_hash,
            "authoring_instructions_hash": authoring_hash,
            "capability_catalog_hash": catalog_hash,
            "solver_program_hash": solver_hash,
            "judge_program_hash": judge_hash,
            "benchmark_revision_id": benchmark_revision_id,
            "comparison_group_id": comparison_group_id,
            "arm_id": arm_id,
            "effective_configuration": effective_configuration,
            "models": models,
            "cases": cases,
            "configuration": experiment,
        }
        return ExperimentLock(
            **identity,
            workspace_hash=workspace.identity_hash,
            comparison_id=comparison_id,
            content_hash=content_hash(identity),
        )

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
            "schema_version": 5,
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
                        "id": item.id,
                        "path": item.source_path,
                        "content_hash": item.content_hash,
                        "source_id": item.source_id,
                        "source_type": item.source_type,
                    }
                    for item in self.context.instruction_units
                ],
                "imports": json_value(self.context.instruction_imports),
                "skill_count": len(self.context.instruction_units),
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
                    "satisfied_capabilities": self.validation["satisfied_capability_count"],
                    "missing_primary_cases": self.validation["missing_primary_case_count"],
                },
                "coverage_by_skill": self.validation["coverage_by_skill"],
                "coverage_gaps": self.validation["coverage_gaps"],
                "capability_gaps": self.validation["capability_gaps"],
                "rubric_coverage": len(self.case_bank.cases),
                "expected_response_coverage": len(self.case_bank.cases),
                "authoring_provenance_coverage": len(self.case_bank.cases),
                "projections": [
                    {
                        "id": case.id,
                        "solver_view_hash": case.solver_view_hash,
                        "judge_view_hash": case.judge_view_hash,
                        "metadata_hash": case.metadata_hash,
                        "case_hash": case.content_hash,
                    }
                    for case in self.case_bank.cases
                ],
            },
            "case_authoring": {
                "instructions": self.repository.case_authoring_path.as_posix(),
                "instructions_hash": sha256_file(self.repository.case_authoring_path),
                "stage": json_value(workspace.stages["author"]),
                "authoring_source_set_hash": self.context.snapshot_hash,
                "authoring_sources": _authoring_sources(self.context),
            },
            "judge": {
                "program_hash": program_hash(create_judge_program()),
                "model": json_value(next(iter(locks.values())).models["judge"]),
                "validation": {
                    "fixture_count": len(self.judge_validation.fixtures),
                    "content_hash": self.judge_validation.content_hash,
                    "status": "configured",
                },
            },
            "experiments": {
                key: {
                    "mode": lock.mode,
                    "lock_id": lock.id,
                    "lock_hash": lock.content_hash,
                    "experiment_revision_id": lock.content_hash,
                    "benchmark_revision_id": lock.benchmark_revision_id,
                    "comparison_group_id": lock.comparison_group_id,
                    "comparison_id": lock.comparison_id,
                    "arm_id": lock.arm_id,
                    "revision": f"{key}@{lock.content_hash.removeprefix('sha256:')[:12]}",
                    "solver_program_hash": lock.solver_program_hash,
                    "solver_model": json_value(lock.models["solver"]),
                    "optimizer": json_value(lock.configuration.optimizer),
                    "optimizer_model": (
                        json_value(lock.models["optimizer"]) if "optimizer" in lock.models else None
                    ),
                    "optimization_preflight": self.validation.get("optimization", {}).get(key),
                    "runtime": json_value(workspace.runtime(key)),
                    "projected_case_runs": len(lock.cases)
                    * (2 if isinstance(lock.configuration.optimizer, CoproConfiguration) else 1),
                    "reuse": {
                        stage_id: json_value(configuration.reuse)
                        for stage_id, configuration in workspace.stages.items()
                    },
                }
                for key, lock in locks.items()
            },
            "executors": json_value(workspace.executors),
            "deployments": json_value(workspace.deployments),
            "stages": json_value(workspace.stages),
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
        workspace_root=repository.root,
        instruction_imports=repository.workspace.evaluation.instructions.imports,
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
        test=policy.test,
    )


def compile_workspace(
    repository: WorkspaceRepository,
    *,
    snapshot: SnapshotLock | None = None,
    snapshot_directory: Path | None = None,
    allow_incomplete_coverage: bool = False,
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
    judge_validation = JudgeValidationCorpus.load(repository.judge_validation_root, case_bank)
    validation = dict(
        validate_case_bank(
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
    )
    if allow_incomplete_coverage:
        if capability_catalog.status != "complete":
            raise PreflightError("pilot runs require a complete capability catalog")
        if validation["missing_catalog_skills"]:
            raise PreflightError("pilot runs require capability entries for every configured skill")
        if not case_bank.cases:
            raise PreflightError("pilot runs require at least one authored case")
    elif validation["status"] != "valid":
        raise PreflightError(
            "case bank does not satisfy the complete capability catalog and configured "
            "per-skill case/group minima"
        )
    if snapshot.target_specification_hash != repository.target_specification().specification_hash:
        raise IntegrityError("snapshot was not built from the active workspace manifest")
    validation["optimization"] = _optimization_preflight(
        repository,
        context,
        case_bank,
        splits,
    )
    return CompiledWorkspace(
        repository,
        snapshot,
        snapshot_directory,
        context,
        capability_catalog,
        case_bank,
        splits,
        judge_validation,
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
    judge_validation = JudgeValidationCorpus.load(
        repository.judge_validation_root,
        case_bank,
        require_complete=False,
    )
    required_labels = {"strong", "partial", "incorrect", "contradictory", "adversarial"}
    judge_validation_labels = {item.label for item in judge_validation.fixtures}
    missing_labels = sorted(required_labels - judge_validation_labels)

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
            "case bank does not satisfy the configured per-skill case and leakage-group minima"
        )
    if validation["capability_gaps"]:
        blockers.append("case bank does not satisfy required primary capability coverage")
    if missing_labels:
        blockers.append("judge validation is missing labels: " + ", ".join(missing_labels))
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
            judge_validation,
            validation,
        )
        locks = {
            experiment_id: compiled.experiment_lock(experiment_id, environment=values)
            for experiment_id in repository.workspace.experiments
        }

    workspace = repository.workspace
    first_experiment = next(iter(workspace.experiments))
    first_configuration = workspace.experiments[first_experiment]
    judge_model = (
        json_value(resolved[first_experiment]["judge"])
        if first_experiment in resolved
        else _unresolved_model(
            first_configuration.roles.judge,
            workspace.models[first_configuration.roles.judge],
            workspace,
        )
    )
    split_counts = Counter(splits.split_for(case) for case in case_bank.cases)
    return {
        "schema_version": 5,
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
                    "id": item.id,
                    "path": item.source_path,
                    "content_hash": item.content_hash,
                    "source_id": item.source_id,
                    "source_type": item.source_type,
                }
                for item in context.instruction_units
            ],
            "imports": json_value(context.instruction_imports),
            "skill_count": len(context.instruction_units),
        },
        "cases": {
            "count": len(case_bank.cases),
            "content_hash": case_bank.content_hash,
            "split_counts": dict(sorted(split_counts.items())),
            "split_hash": splits.content_hash,
            "minimum_cases_per_skill": validation["minimum_cases_per_skill"],
            "minimum_leakage_groups_per_skill": validation["minimum_leakage_groups_per_skill"],
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
            "projections": [
                {
                    "id": case.id,
                    "solver_view_hash": case.solver_view_hash,
                    "judge_view_hash": case.judge_view_hash,
                    "metadata_hash": case.metadata_hash,
                    "case_hash": case.content_hash,
                }
                for case in case_bank.cases
            ],
        },
        "case_authoring": {
            "instructions": repository.case_authoring_path.as_posix(),
            "instructions_hash": sha256_file(repository.case_authoring_path),
            "stage": json_value(workspace.stages["author"]),
            "authoring_source_set_hash": context.snapshot_hash,
            "authoring_sources": _authoring_sources(context),
        },
        "judge": {
            "program_hash": program_hash(create_judge_program()),
            "model": judge_model,
            "validation": {
                "fixture_count": len(judge_validation.fixtures),
                "labels": sorted(judge_validation_labels),
                "missing_labels": missing_labels,
                "content_hash": judge_validation.content_hash,
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
                "experiment_revision_id": (
                    locks[experiment_id].content_hash if experiment_id in locks else None
                ),
                "revision": (
                    f"{experiment_id}@"
                    f"{locks[experiment_id].content_hash.removeprefix('sha256:')[:12]}"
                    if experiment_id in locks
                    else None
                ),
                "solver_program_hash": program_hash(create_solver_program()),
                "solver_model": (
                    json_value(resolved[experiment_id]["solver"])
                    if experiment_id in resolved
                    else _unresolved_model(
                        experiment.roles.solver,
                        workspace.models[experiment.roles.solver],
                        workspace,
                    )
                ),
                "model_resolution_error": unresolved_models.get(experiment_id),
                "runtime": json_value(workspace.runtime(experiment_id)),
                "projected_case_runs": (
                    len(case_bank.cases) * experiment.repetitions
                    if experiment.mode is ExperimentMode.EVALUATE
                    else sum(splits.split_for(case) == "test" for case in case_bank.cases)
                    * (2 if isinstance(experiment.optimizer, CoproConfiguration) else 1)
                ),
                "reuse": {
                    stage_id: json_value(configuration.reuse)
                    for stage_id, configuration in workspace.stages.items()
                },
            }
            for experiment_id, experiment in workspace.experiments.items()
        },
        "executors": {
            executor_id: {
                **dict(json_value(executor)),
                **(
                    {
                        "pool_hash": executor.pool_hash,
                        "total_concurrency": executor.total_concurrency,
                    }
                    if executor.endpoints
                    else {}
                ),
            }
            for executor_id, executor in workspace.executors.items()
        },
        "deployments": json_value(workspace.deployments),
        "stages": json_value(workspace.stages),
    }


def _unresolved_model(
    profile_id: str,
    configuration: ModelConfiguration,
    workspace,
) -> Mapping[str, object]:  # type: ignore[no-untyped-def]
    deployment = workspace.deployments[configuration.deployment]
    executor = workspace.executors[deployment.executor]
    return {
        "status": "unresolved",
        "profile_id": profile_id,
        "deployment_id": deployment.id,
        "executor": executor.id,
        "provider": executor.adapter,
        "name": deployment.model,
        "deployment_revision": deployment.revision,
        "endpoints": [
            {
                "id": endpoint.id,
                "base_url": endpoint.base_url,
                "max_concurrent_requests": endpoint.max_concurrent_requests,
                "api_key_env": endpoint.api_key_env,
                "route_model": (
                    endpoint.route(deployment.id).model
                    if endpoint.route(deployment.id) is not None
                    else None
                ),
            }
            for endpoint in executor.endpoints
            if endpoint.route(deployment.id) is not None
        ],
        "reasoning_effort": configuration.reasoning_effort,
        "parameters": dict(configuration.parameters),
    }


def lock_as_dict(lock: ExperimentLock) -> dict[str, object]:
    value = json_value(lock)
    assert isinstance(value, dict)
    # The full workspace hash is run provenance, not part of this selected
    # experiment revision. Keeping it out makes the revision document stable
    # when an unrelated experiment changes.
    value.pop("workspace_hash", None)
    return value
