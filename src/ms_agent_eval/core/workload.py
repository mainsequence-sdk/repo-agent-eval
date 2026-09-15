from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from .errors import ConfigurationError
from .evaluation import (
    CaseDefinition,
    deterministic_group_training_partitions,
    deterministic_group_validation_split,
)
from .planning import CompiledWorkspace, ExperimentLock
from .workspace import CoproConfiguration, ExecutorType, ExperimentMode


def calculate_workload(
    workspace: CompiledWorkspace,
    experiment_id: str,
    *,
    environment: Mapping[str, str],
    pilot: bool = False,
) -> Mapping[str, object]:
    """Expand an experiment into its fresh-run logical work without executing it."""

    lock = workspace.experiment_lock(experiment_id, environment=environment)
    configuration = lock.configuration
    if pilot and configuration.mode is ExperimentMode.OPTIMIZE:
        raise ConfigurationError("optimization experiments do not support pilot execution")

    cases_by_split = _cases_by_split(workspace)
    solver_repetitions = _solver_repetitions(workspace, lock, pilot=pilot)
    judge_repetitions = 1 if pilot else workspace.repository.workspace.evaluation.judge.repetitions
    calibration_calls = len(workspace.judge_validation.fixtures) * judge_repetitions
    steps: list[Mapping[str, object]] = [
        _model_step(
            "validate_judge",
            "experiment gate",
            "judge",
            len(workspace.judge_validation.fixtures),
            judge_repetitions,
            calibration_calls,
            "fixtures × judge repetitions; executed once for the experiment",
        )
    ]
    partitions: list[Mapping[str, object]] = []
    derived: Mapping[str, object] | None = None

    if configuration.mode is ExperimentMode.EVALUATE:
        execution_split = "train" if pilot else "test"
        execution_cases = cases_by_split[execution_split]
        solve_calls = len(execution_cases) * solver_repetitions
        judge_calls = solve_calls * judge_repetitions
        steps.extend(
            (
                _model_step(
                    "solve",
                    "pilot" if pilot else "test",
                    "solver",
                    len(execution_cases),
                    solver_repetitions,
                    solve_calls,
                    "cases × solver repetitions",
                ),
                _model_step(
                    "judge",
                    "pilot" if pilot else "test",
                    "judge",
                    solve_calls,
                    judge_repetitions,
                    judge_calls,
                    "solver responses × judge repetitions",
                ),
                _local_step("aggregate", "final", 1, "one deterministic aggregation"),
            )
        )
        execution = {
            "split": execution_split,
            "case_count": len(execution_cases),
            "arms": 1,
        }
        strategy = "evaluation"
    else:
        permanent_train = cases_by_split["train"]
        validation = configuration.validation
        optimizer = configuration.optimizer
        assert validation is not None and optimizer is not None
        validation_assignments = deterministic_group_validation_split(
            permanent_train,
            fraction=validation.fraction,
            seed=validation.seed,
        )
        validation_by_group = {item.group: item.split for item in validation_assignments}
        training_cases = tuple(
            case for case in permanent_train if validation_by_group[case.group] == "training"
        )
        validation_cases = tuple(
            case for case in permanent_train if validation_by_group[case.group] == "validation"
        )
        derived = {
            "optimization_training_cases": len(training_cases),
            "optimization_training_groups": len({case.group for case in training_cases}),
            "validation_cases": len(validation_cases),
            "validation_groups": len({case.group for case in validation_cases}),
            "validation_fraction": validation.fraction,
            "validation_seed": validation.seed,
        }
        if isinstance(optimizer, CoproConfiguration):
            strategy = "copro"
            configured_skills = (
                tuple(sorted(workspace.context.skill_contexts))
                if optimizer.skills == "all"
                else optimizer.skills
            )
            assignments = deterministic_group_training_partitions(
                training_cases,
                partitions=optimizer.partitions,
                seed=validation.seed,
            )
            partition_by_group = {item.group: item.partition for item in assignments}
            proposal_calls = 0
            candidate_solve_calls = 0
            candidate_judge_calls = 0
            candidate_fit_operations = 0
            selection_operations = 0
            for partition in range(optimizer.partitions):
                partition_cases = tuple(
                    case for case in training_cases if partition_by_group[case.group] == partition
                )
                active_skills = tuple(
                    skill
                    for skill in configured_skills
                    if any(case.skill == skill for case in partition_cases)
                )
                prompt_targets = int(optimizer.optimize_prompt)
                target_count = prompt_targets + len(active_skills)
                validation_slots = (
                    len(validation_cases) if optimizer.optimize_prompt else 0
                ) + sum(
                    sum(case.skill == skill for case in validation_cases) for skill in active_skills
                )
                partition_proposals = optimizer.depth * target_count
                partition_solves = optimizer.depth * optimizer.breadth * validation_slots
                partition_judges = partition_solves * judge_repetitions
                partition_fits = optimizer.depth * optimizer.breadth * target_count + 1
                partition_selections = optimizer.depth * target_count
                proposal_calls += partition_proposals
                candidate_solve_calls += partition_solves
                candidate_judge_calls += partition_judges
                candidate_fit_operations += partition_fits
                selection_operations += partition_selections
                partitions.append(
                    {
                        "partition": partition + 1,
                        "training_cases": len(partition_cases),
                        "training_groups": len({case.group for case in partition_cases}),
                        "active_skills": list(active_skills),
                        "prompt_targets": prompt_targets,
                        "skill_targets": len(active_skills),
                        "target_count": target_count,
                        "rounds": optimizer.depth,
                        "candidates_per_target": optimizer.breadth,
                        "validation_case_slots_per_candidate_round": validation_slots,
                        "optimizer_calls": partition_proposals,
                        "candidate_solver_calls": partition_solves,
                        "candidate_judge_calls": partition_judges,
                        "local_fit_operations": partition_fits,
                        "local_selection_operations": partition_selections,
                    }
                )
            steps.extend(
                (
                    _model_step(
                        "optimize",
                        "stateful search",
                        "optimizer",
                        sum(int(item["target_count"]) for item in partitions),
                        optimizer.depth,
                        proposal_calls,
                        "partition targets × rounds; each call requests breadth - 1 proposals",
                    ),
                    _local_step(
                        "fit",
                        "candidate search",
                        candidate_fit_operations,
                        "candidate fits plus one carried winner fit per partition",
                    ),
                    _model_step(
                        "solve",
                        "validation search",
                        "solver",
                        sum(
                            int(item["validation_case_slots_per_candidate_round"])
                            for item in partitions
                        ),
                        optimizer.depth * optimizer.breadth,
                        candidate_solve_calls,
                        "validation slots × rounds × candidates",
                    ),
                    _model_step(
                        "judge",
                        "validation search",
                        "judge",
                        candidate_solve_calls,
                        judge_repetitions,
                        candidate_judge_calls,
                        "candidate solver responses × judge repetitions",
                    ),
                    _local_step(
                        "select",
                        "stateful search",
                        selection_operations,
                        "one deterministic winner selection per target and round",
                    ),
                    _local_step(
                        "fit",
                        "final materialization",
                        1,
                        "materialize the carried final winner on all optimization training",
                    ),
                )
            )
            test_arms = 2
            execution = {
                "split": "test",
                "case_count": len(cases_by_split["test"]),
                "arms": test_arms,
                "arm_names": ["baseline", "optimized"],
            }
        else:
            strategy = "labeled_few_shot"
            candidate_count = len(optimizer.k_values) + 1
            validation_solves = candidate_count * len(validation_cases)
            validation_judges = validation_solves * judge_repetitions
            steps.extend(
                (
                    _local_step(
                        "fit",
                        "candidate search",
                        candidate_count,
                        "one local fit for k=0 and every configured k",
                    ),
                    _model_step(
                        "solve",
                        "validation search",
                        "solver",
                        len(validation_cases),
                        candidate_count,
                        validation_solves,
                        "validation cases × candidate k values",
                    ),
                    _model_step(
                        "judge",
                        "validation search",
                        "judge",
                        validation_solves,
                        judge_repetitions,
                        validation_judges,
                        "candidate solver responses × judge repetitions",
                    ),
                    _local_step(
                        "select", "validation", 1, "select the highest mean validation score"
                    ),
                    _local_step(
                        "fit", "final materialization", 1, "materialize the selected k once"
                    ),
                )
            )
            test_arms = 1
            execution = {
                "split": "test",
                "case_count": len(cases_by_split["test"]),
                "arms": test_arms,
                "arm_names": ["optimized"],
            }

        test_solve_calls = execution["case_count"] * test_arms * solver_repetitions
        test_judge_calls = test_solve_calls * judge_repetitions
        steps.extend(
            (
                _model_step(
                    "solve",
                    "untouched test",
                    "solver",
                    int(execution["case_count"]) * test_arms,
                    solver_repetitions,
                    test_solve_calls,
                    "test cases × arms × solver repetitions",
                ),
                _model_step(
                    "judge",
                    "untouched test",
                    "judge",
                    test_solve_calls,
                    judge_repetitions,
                    test_judge_calls,
                    "test solver responses × judge repetitions",
                ),
                _local_step("aggregate", "final", 1, "one deterministic aggregation"),
            )
        )

    model_steps = [step for step in steps if step["kind"] == "model"]
    local_steps = [step for step in steps if step["kind"] == "local"]
    by_role = Counter()
    by_stage = Counter()
    for step in model_steps:
        by_role[str(step["role"])] += int(step["calls"])
        by_stage[str(step["stage"])] += int(step["calls"])

    return {
        "schema_version": 1,
        "kind": "fresh_upper_bound",
        "strategy": strategy,
        "case_inventory": {
            "authored_total": sum(len(items) for items in cases_by_split.values()),
            "permanent_splits": {
                split: len(cases_by_split[split]) for split in ("train", "test", "challenge")
            },
            "derived": derived,
            "execution": execution,
        },
        "repetitions": {
            "solver_test": solver_repetitions,
            "judge": judge_repetitions,
        },
        "steps": steps,
        "partitions": partitions,
        "model_routes": _model_routes(workspace, lock),
        "totals": {
            "logical_model_calls": sum(int(step["calls"]) for step in model_steps),
            "by_role": dict(sorted(by_role.items())),
            "by_stage": dict(sorted(by_stage.items())),
            "local_operations": sum(int(step["operations"]) for step in local_steps),
        },
        "reuse_note": (
            "Fresh-run logical upper bound before semantic cache reuse. Exact reusable work is "
            "resolved only after stage packets are materialized."
        ),
    }


def _cases_by_split(
    workspace: CompiledWorkspace,
) -> Mapping[str, tuple[CaseDefinition, ...]]:
    return {
        split: tuple(
            case for case in workspace.case_bank.cases if workspace.splits.split_for(case) == split
        )
        for split in ("train", "test", "challenge")
    }


def _solver_repetitions(
    workspace: CompiledWorkspace,
    lock: ExperimentLock,
    *,
    pilot: bool,
) -> int:
    if pilot:
        return 1
    reuse = workspace.repository.workspace.stages["solve"].reuse
    return reuse.samples if reuse.policy.value == "replicate" else lock.configuration.repetitions


def _model_step(
    stage: str,
    phase: str,
    role: str,
    units: int,
    multiplicity: int,
    calls: int,
    formula: str,
) -> Mapping[str, object]:
    return {
        "kind": "model",
        "stage": stage,
        "phase": phase,
        "role": role,
        "units": units,
        "multiplicity": multiplicity,
        "calls": calls,
        "formula": formula,
    }


def _local_step(stage: str, phase: str, operations: int, formula: str) -> Mapping[str, object]:
    return {
        "kind": "local",
        "stage": stage,
        "phase": phase,
        "operations": operations,
        "formula": formula,
    }


def _model_routes(
    workspace: CompiledWorkspace,
    lock: ExperimentLock,
) -> list[Mapping[str, object]]:
    routes = []
    for role, model in sorted(lock.models.items()):
        executor = workspace.repository.workspace.executors[model.executor_id]
        endpoints = (
            model.eligible_endpoints(executor) if executor.type is ExecutorType.ENDPOINT else ()
        )
        routes.append(
            {
                "role": role,
                "profile": model.profile_id,
                "model": model.model,
                "deployment": model.deployment_id,
                "deployment_revision": model.deployment_revision,
                "executor": executor.id,
                "adapter": executor.adapter,
                "configured_capacity": (
                    sum(endpoint.max_concurrent_requests for endpoint in endpoints)
                    if endpoints
                    else None
                ),
                "endpoints": [
                    {
                        "id": endpoint.id,
                        "capacity": endpoint.max_concurrent_requests,
                        "base_url": endpoint.base_url,
                    }
                    for endpoint in endpoints
                ],
            }
        )
    return routes


__all__ = ["calculate_workload"]
