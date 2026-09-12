from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import uuid4

import dspy

from ms_agent_eval.programs.dspy.budget import BudgetLedger, BudgetLimits
from ms_agent_eval.programs.dspy.engine import (
    DspyExecutionContract,
    DspyExecutor,
    create_judge_program,
    create_solver_program,
    program_hash,
)
from ms_agent_eval.providers.openai_compatible import create_observed_lm

from .errors import IntegrityError, PreflightError
from .evaluation import (
    CalibrationResult,
    CaseDefinition,
    EvaluationRecord,
    LlmJudge,
    calibrate_judge,
)
from .hashing import content_hash, json_value
from .models import ArtifactReference, ProgramResult
from .planning import CompiledWorkspace, ExperimentLock, lock_as_dict
from .providers import ModelCallObserver
from .storage import FilesystemArtifactStore
from .workspace import ExperimentMode, ResolvedRoleModel, RoleBudget


LmFactory = Callable[
    [ResolvedRoleModel, ModelCallObserver, BudgetLedger | None, str | None], dspy.BaseLM
]


def _default_lm_factory(
    model: ResolvedRoleModel,
    observer: ModelCallObserver,
    budget: BudgetLedger | None,
    api_key: str | None,
) -> dspy.BaseLM:
    return create_observed_lm(model, observer, budget=budget, api_key=api_key)


@dataclass(frozen=True)
class CaseRun:
    case_id: str
    split: str
    repetition: int
    solver: ProgramResult
    evaluation: EvaluationRecord
    response_artifact: ArtifactReference
    evaluation_artifact: ArtifactReference


@dataclass(frozen=True)
class ExperimentRun:
    schema_version: int
    id: str
    lock_hash: str
    mode: str
    calibration: CalibrationResult
    compiled_program: ArtifactReference | None
    cases: tuple[CaseRun, ...]
    role_usage: Mapping[str, Mapping[str, object]]
    result_artifact: ArtifactReference


class _ObservedJudgeProgram:
    def __init__(
        self,
        *,
        executor: DspyExecutor,
        program: dspy.Module,
        lm: dspy.BaseLM,
        observer: ModelCallObserver,
    ) -> None:
        self.executor = executor
        self.program = program
        self.lm = lm
        self.observer = observer
        self.hash = program_hash(program)

    def __call__(self, **inputs: object) -> Mapping[str, object]:
        result = self.executor.execute(
            contract=DspyExecutionContract(
                role="judge",
                program_hash=self.hash,
                inputs=inputs,
                required_outputs=("criterion_scores", "hard_failures", "feedback"),
                primary_output=None,
            ),
            program=self.program,
            lm=self.lm,
            observer=self.observer,
        )
        if result.status != "completed":
            raise IntegrityError(f"judge DSPy call failed: {result.error_kind}")
        return {**result.outputs, "call_ids": [item.call_id for item in result.calls]}


class ExperimentRunner:
    """Execute evaluate and optimize runs through one DSPy solver/judge path."""

    def __init__(
        self,
        workspace: CompiledWorkspace,
        *,
        lm_factory: LmFactory = _default_lm_factory,
    ) -> None:
        self.workspace = workspace
        self.artifacts = FilesystemArtifactStore(
            workspace.repository.data_root,
            workspace_root=workspace.repository.root,
        )
        self.lm_factory = lm_factory
        self.executor = DspyExecutor(self.artifacts)

    def run(
        self,
        experiment_id: str,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> ExperimentRun:
        # CompiledWorkspace construction is the structural/source/provenance preflight.
        lock = self.workspace.experiment_lock(experiment_id, environment=environment)
        resolved_environment = (
            environment if environment is not None else self.workspace.repository.environment()
        )
        self._validate_calibration_split(lock)
        run_id = str(uuid4())
        lock_reference = self.artifacts.put_manifest(
            f"runs/{run_id}/experiment.lock", lock_as_dict(lock)
        )
        judge_observer = ModelCallObserver(self.artifacts, role="judge")
        solver_observer = ModelCallObserver(self.artifacts, role="solver")
        solver_budget, judge_budget = self._budgets(lock)
        judge_model = lock.models["judge"]
        judge_lm = self.lm_factory(
            judge_model,
            judge_observer,
            judge_budget,
            judge_model.api_key(resolved_environment),
        )
        judge_program = create_judge_program()
        observed_judge = _ObservedJudgeProgram(
            executor=self.executor,
            program=judge_program,
            lm=judge_lm,
            observer=judge_observer,
        )
        judge = LlmJudge(
            observed_judge,
            model_hash=lock.models["judge"].content_hash,
            program_hash=lock.judge_program_hash,
            repetitions=self.workspace.repository.workspace.evaluation.judge.repetitions,
        )
        calibration = calibrate_judge(
            judge,
            self.workspace.calibration,
            self.workspace.case_bank,
            self.workspace.context.skill,
        )
        calibration_reference = self.artifacts.put_manifest(
            f"runs/{run_id}/judge-calibration",
            json_value(calibration),  # type: ignore[arg-type]
        )
        if not calibration.passed:
            raise PreflightError("configured LLM judge failed calibration; no solver call was made")
        solver_model = lock.models["solver"]
        solver_lm = self.lm_factory(
            solver_model,
            solver_observer,
            solver_budget,
            solver_model.api_key(resolved_environment),
        )
        if lock.configuration.mode is ExperimentMode.EVALUATE:
            program = create_solver_program()
            cases = self._evaluate_cases(
                run_id=run_id,
                lock=lock,
                program=program,
                solver_lm=solver_lm,
                judge=judge,
                repetitions=lock.configuration.repetitions,
            )
            compiled_reference = None
        else:
            program, compiled_reference = self._compile(
                run_id=run_id,
                lock=lock,
                solver_lm=solver_lm,
                judge=judge,
            )
            # Held-out test cases are first loaded and evaluated only after state publication.
            cases = self._evaluate_cases(
                run_id=run_id,
                lock=lock,
                program=program,
                solver_lm=solver_lm,
                judge=judge,
                repetitions=1,
                selected_split="test",
            )
        role_usage = {
            "solver": _usage(solver_observer),
            "judge": _usage(judge_observer),
        }
        result_payload = {
            "schema_version": 2,
            "id": run_id,
            "lock_hash": lock.content_hash,
            "lock_artifact": lock_reference,
            "mode": lock.mode,
            "calibration": calibration,
            "calibration_artifact": calibration_reference,
            "compiled_program": compiled_reference,
            "cases": cases,
            "role_usage": role_usage,
        }
        result_reference = self.artifacts.put_manifest(
            f"runs/{run_id}/result",
            json_value(result_payload),  # type: ignore[arg-type]
        )
        return ExperimentRun(
            schema_version=2,
            id=run_id,
            lock_hash=lock.content_hash,
            mode=lock.mode,
            calibration=calibration,
            compiled_program=compiled_reference,
            cases=cases,
            role_usage=role_usage,
            result_artifact=result_reference,
        )

    def _compile(
        self,
        *,
        run_id: str,
        lock: ExperimentLock,
        solver_lm: dspy.BaseLM,
        judge: LlmJudge,
    ) -> tuple[dspy.Module, ArtifactReference]:
        experiment = lock.configuration
        if experiment.optimizer is None:
            raise IntegrityError("optimization run has no optimizer")
        by_split = self._cases_by_split()
        for required in ("train", "development", "test"):
            if not by_split[required]:
                raise PreflightError(f"optimization requires a non-empty {required} split")
        train_examples = [
            dspy.Example(
                global_context=self.workspace.context.global_context,
                skill_context=self.workspace.context.skill(case.skill),
                task=case.prompt,
                response=case.expected_response,
            ).with_inputs("global_context", "skill_context", "task")
            for case in by_split["train"]
        ]
        parameters = experiment.optimizer.parameters
        raw_k = parameters.get("k", 2)
        if not isinstance(raw_k, int) or isinstance(raw_k, bool) or raw_k < 1:
            raise PreflightError("LabeledFewShot k must be a positive integer")
        raw_seed = parameters.get("seed", 0)
        if not isinstance(raw_seed, int) or isinstance(raw_seed, bool):
            raise PreflightError("LabeledFewShot seed must be an integer")
        previous_state = random.getstate()
        try:
            random.seed(raw_seed)
            compiled = dspy.LabeledFewShot(k=raw_k).compile(
                create_solver_program(), trainset=train_examples, sample=True
            )
        finally:
            random.setstate(previous_state)
        # Development feedback is visible to the optimization run; test is still sealed.
        development = self._evaluate_cases(
            run_id=run_id,
            lock=lock,
            program=compiled,
            solver_lm=solver_lm,
            judge=judge,
            repetitions=1,
            selected_split="development",
        )
        if not development:
            raise PreflightError("compiled solver produced no development evaluations")
        state_reference = self.executor.save_state_artifact(compiled)
        manifest = {
            "schema_version": 2,
            "run_id": run_id,
            "base_program_hash": lock.solver_program_hash,
            "compiled_program_hash": program_hash(compiled),
            "optimizer": json_value(experiment.optimizer),
            "train_case_hash": content_hash(
                tuple((case.id, case.content_hash) for case in by_split["train"])
            ),
            "development_case_hash": content_hash(
                tuple((case.id, case.content_hash) for case in by_split["development"])
            ),
            "held_out_assignment_hash": content_hash(
                tuple((case.id, case.content_hash) for case in by_split["test"])
            ),
            "state_format": "json",
            "state_artifact": state_reference,
            "development_results": [item.evaluation.content_hash for item in development],
        }
        reference = self.artifacts.put_manifest(
            f"runs/{run_id}/compiled-program",
            json_value(manifest),  # type: ignore[arg-type]
        )
        return compiled, reference

    def _evaluate_cases(
        self,
        *,
        run_id: str,
        lock: ExperimentLock,
        program: dspy.Module,
        solver_lm: dspy.BaseLM,
        judge: LlmJudge,
        repetitions: int,
        selected_split: str | None = None,
    ) -> tuple[CaseRun, ...]:
        solver_observer = getattr(solver_lm, "observer", None)
        if not isinstance(solver_observer, ModelCallObserver):
            raise IntegrityError("solver LM must use the observed DSPy binding")
        results: list[CaseRun] = []
        actual_program_hash = program_hash(program)
        for case in self.workspace.case_bank.cases:
            split = self.workspace.splits.split_for(case)
            if selected_split is not None and split != selected_split:
                continue
            for repetition in range(1, repetitions + 1):
                values = {
                    "global_context": self.workspace.context.global_context,
                    "skill_context": self.workspace.context.skill(case.skill),
                    "task": case.prompt,
                }
                solver_result = self.executor.execute(
                    contract=DspyExecutionContract(
                        role="solver",
                        program_hash=actual_program_hash,
                        inputs=values,
                        required_outputs=("response",),
                        primary_output="response",
                    ),
                    program=program,
                    lm=solver_lm,
                    observer=solver_observer,
                )
                if solver_result.status != "completed" or solver_result.primary_response is None:
                    raise IntegrityError(
                        f"solver failed for case {case.id!r}: {solver_result.error_kind}"
                    )
                response_reference = self.artifacts.put_manifest(
                    f"runs/{run_id}/cases/{case.id}/response-{repetition}",
                    {
                        "case_id": case.id,
                        "split": split,
                        "repetition": repetition,
                        "solver_model_hash": lock.models["solver"].content_hash,
                        "program_hash": actual_program_hash,
                        "result": json_value(solver_result),
                    },
                )
                evaluation = judge.evaluate(
                    case,
                    solver_result.primary_response,
                    skill_context=self.workspace.context.skill(case.skill),
                )
                evaluation_reference = self.artifacts.put_manifest(
                    f"runs/{run_id}/cases/{case.id}/evaluation-{repetition}",
                    json_value(evaluation),  # type: ignore[arg-type]
                )
                results.append(
                    CaseRun(
                        case.id,
                        split,
                        repetition,
                        solver_result,
                        evaluation,
                        response_reference,
                        evaluation_reference,
                    )
                )
        return tuple(results)

    def _cases_by_split(self) -> Mapping[str, tuple[CaseDefinition, ...]]:
        values: dict[str, list[CaseDefinition]] = {
            "train": [],
            "development": [],
            "test": [],
            "challenge": [],
        }
        for case in self.workspace.case_bank.cases:
            values[self.workspace.splits.split_for(case)].append(case)
        return {key: tuple(items) for key, items in values.items()}

    def _validate_calibration_split(self, lock: ExperimentLock) -> None:
        if lock.configuration.mode is not ExperimentMode.OPTIMIZE:
            return
        cases = {case.id: case for case in self.workspace.case_bank.cases}
        held_out = sorted(
            fixture.id
            for fixture in self.workspace.calibration.fixtures
            if self.workspace.splits.split_for(cases[fixture.case_id]) in {"test", "challenge"}
        )
        if held_out:
            raise PreflightError(
                "optimization judge calibration cannot use held-out test/challenge cases: "
                f"{held_out}"
            )

    @staticmethod
    def _budgets(lock: ExperimentLock) -> tuple[BudgetLedger | None, BudgetLedger | None]:
        budget = lock.configuration.budget
        if budget is None:
            return None, None
        return (
            _ledger(budget.solver, wall_seconds=budget.wall_seconds),
            _ledger(budget.judge, wall_seconds=budget.wall_seconds),
        )


def _ledger(budget: RoleBudget, *, wall_seconds: float | None = None) -> BudgetLedger:
    return BudgetLedger(
        BudgetLimits(
            model_calls=budget.model_calls,
            configured_cost=float("inf"),
            tokens=budget.tokens,
            wall_seconds=min(budget.wall_seconds, wall_seconds or budget.wall_seconds),
            concurrency=1,
        )
    )


def _usage(observer: ModelCallObserver) -> Mapping[str, object]:
    completed = [item for item in observer.records if item.status == "completed"]
    failed = [item for item in observer.records if item.status == "failed"]
    tokens = sum(
        int(item.usage.get("total_tokens", 0) or 0)
        for item in completed
        if isinstance(item.usage.get("total_tokens", 0), int)
    )
    costs = sum(item.configured_cost for item in completed)
    return {
        "model_calls": len(observer.records),
        "completed_calls": len(completed),
        "failed_calls": len(failed),
        "tokens": tokens,
        "configured_cost": round(costs, 10),
        "error_kinds": dict(Counter(item.error_kind for item in failed)),
    }
