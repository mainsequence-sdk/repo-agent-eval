from __future__ import annotations

import json
import random
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import dspy

from ms_agent_eval.programs.dspy.budget import BudgetLedger, BudgetLimits
from ms_agent_eval.programs.dspy.engine import (
    DspyExecutionContract,
    DspyExecutor,
    create_author_program,
    create_judge_program,
    create_solver_program,
    load_state_json,
    program_hash,
)
from ms_agent_eval.providers.openai_compatible import create_observed_lm

from .errors import ConfigurationError, IntegrityError, PreflightError, ResolutionError
from .evaluation import LlmJudge, calibrate_judge, calibration_result_from_scores
from .hashing import content_hash, json_value
from .planning import CompiledWorkspace, ExperimentLock, lock_as_dict
from .providers import ModelCallObserver
from .stage_executors import (
    AutomaticExecutionFailed,
    ExecutorOutput,
    HarnessExecutionPending,
)
from .stages import (
    StageArtifact,
    StageBudget,
    StageError,
    StageExecutorIdentity,
    StageKind,
    StageLedger,
    StageModelIdentity,
    StagePacket,
    StageResult,
    StageState,
)
from .storage import FilesystemArtifactStore
from .workspace import (
    ExecutorConfiguration,
    ExperimentMode,
    ResolvedRoleModel,
    RoleBudget,
    resolve_stage_model,
)


LmFactory = Callable[
    [ResolvedRoleModel, ModelCallObserver, BudgetLedger | None, str | None], dspy.BaseLM
]


def default_lm_factory(
    model: ResolvedRoleModel,
    observer: ModelCallObserver,
    budget: BudgetLedger | None,
    api_key: str | None,
) -> dspy.BaseLM:
    return create_observed_lm(model, observer, budget=budget, api_key=api_key)


@dataclass(frozen=True)
class RunProgress:
    schema_version: int
    run_id: str
    experiment_id: str
    execution_mode: str
    benchmark_valid: bool
    coverage_status: str
    status: str
    current_stage: str | None
    packets: tuple[Mapping[str, object], ...]
    result: Mapping[str, object] | None


class _ExecutionSession:
    def __init__(
        self,
        coordinator: StageCoordinator,
        lock: ExperimentLock,
        environment: Mapping[str, str],
    ) -> None:
        self.coordinator = coordinator
        self.lock = lock
        self.environment = environment
    def lm(
        self, role: str, stage: StageKind
    ) -> tuple[dspy.BaseLM, ModelCallObserver]:
        model = self.lock.models.get(role) or resolve_stage_model(
            self.coordinator.workspace.repository.workspace,
            stage.value,
            role=role,
        )
        if model is None:
            raise ConfigurationError(f"stage {stage.value!r} has no model")
        executor = self.coordinator.workspace.repository.workspace.executors[model.executor_id]
        observer = ModelCallObserver(self.coordinator.artifacts, role=role)
        lm = self.coordinator.lm_factory(
            model,
            observer,
            self.coordinator._budget_ledger(stage),
            executor.api_key(self.environment),
        )
        return lm, observer


class StageCoordinator:
    """Advance experiments exclusively through the portable stage contract."""

    def __init__(
        self,
        workspace: CompiledWorkspace,
        *,
        lm_factory: LmFactory = default_lm_factory,
    ) -> None:
        self.workspace = workspace
        self.artifacts = FilesystemArtifactStore(
            workspace.repository.data_root,
            workspace_root=workspace.repository.root,
        )
        self.ledger = StageLedger(self.artifacts)
        self.dspy = DspyExecutor(self.artifacts)
        self.lm_factory = lm_factory

    def start(
        self,
        experiment_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        through: str | None = None,
        pilot: bool = False,
    ) -> RunProgress:
        resolved_environment = (
            environment if environment is not None else self.workspace.repository.environment()
        )
        lock = self.workspace.experiment_lock(experiment_id, environment=resolved_environment)
        if pilot and lock.configuration.mode is not ExperimentMode.EVALUATE:
            raise PreflightError("pilot execution supports evaluate experiments only")
        self._validate_calibration_split(lock, pilot=pilot)
        run_id = str(uuid4())
        execution_mode = "pilot" if pilot else "benchmark"
        lock_reference = self.artifacts.put_manifest(
            f"runs/{run_id}/experiment.lock", lock_as_dict(lock)
        )
        self.ledger.create_run(
            run_id,
            {
                "schema_version": 2,
                "run_id": run_id,
                "experiment_id": experiment_id,
                "execution_mode": execution_mode,
                "benchmark_valid": not pilot,
                "coverage_status": self.workspace.validation["status"],
                "selected_splits": (
                    ["train", "development"] if pilot else ["configured"]
                ),
                "judge_repetitions": (
                    1
                    if pilot
                    else self.workspace.repository.workspace.evaluation.judge.repetitions
                ),
                "workspace_id": lock.workspace_id,
                "workspace_hash": lock.workspace_hash,
                "experiment_lock_hash": lock.content_hash,
                "experiment_lock_artifact": json_value(lock_reference),
                "created_at": _timestamp(),
            },
        )
        return self._advance(
            run_id,
            lock,
            environment=resolved_environment,
            through=through,
            pilot=pilot,
        )

    def resume(
        self,
        run_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        through: str | None = None,
    ) -> RunProgress:
        run = self.ledger.load_run(run_id)
        experiment_id = _string(run.get("experiment_id"), "run experiment_id")
        pilot = _execution_mode(run) == "pilot"
        resolved_environment = (
            environment if environment is not None else self.workspace.repository.environment()
        )
        lock = self.workspace.experiment_lock(experiment_id, environment=resolved_environment)
        if run.get("workspace_hash") != lock.workspace_hash:
            raise IntegrityError("workspace changed since this run was created")
        if run.get("experiment_lock_hash") != lock.content_hash:
            raise IntegrityError("experiment lock changed since this run was created")
        self._validate_calibration_split(lock, pilot=pilot)
        return self._advance(
            run_id,
            lock,
            environment=resolved_environment,
            through=through,
            pilot=pilot,
        )

    def prepare_stage(
        self,
        run_id: str,
        stage: StageKind,
        *,
        campaign: str | None = None,
    ) -> tuple[StagePacket, ...]:
        run = self.ledger.load_run(run_id)
        experiment_id = _string(run.get("experiment_id"), "run experiment_id")
        pilot = _execution_mode(run) == "pilot"
        lock = self.workspace.experiment_lock(experiment_id)
        if stage is StageKind.AUTHOR:
            return (self._author_packet(run_id, lock, campaign=campaign),)
        if stage is StageKind.CALIBRATE_JUDGE:
            return (self._calibration_packet(run_id, lock),)
        calibration = self._persisted_packet(run_id, StageKind.CALIBRATE_JUDGE)
        if calibration is None or not self._calibration_passed(calibration):
            raise PreflightError("judge calibration must pass before this stage is prepared")
        if stage is StageKind.OPTIMIZE:
            if pilot:
                raise PreflightError("pilot runs cannot prepare optimization")
            return (self._optimize_packet(run_id, lock, calibration),)
        if stage is StageKind.SOLVE:
            program_dependency: StagePacket | None = None
            selected = None
            if lock.configuration.mode is ExperimentMode.OPTIMIZE:
                program_dependency = self._persisted_packet(run_id, StageKind.OPTIMIZE)
                if program_dependency is None:
                    raise PreflightError("optimize must persist before solve is prepared")
                final = self._persisted_packet(run_id, StageKind.FINAL_EVALUATE)
                selected = "test" if final is not None else "development"
                if final is not None:
                    program_dependency = final
            selected_splits = (
                ("train", "development")
                if pilot and lock.configuration.mode is ExperimentMode.EVALUATE
                else None
            )
            return self._solve_packets(
                run_id,
                lock,
                calibration,
                selected_split=selected,
                selected_splits=selected_splits,
                program_dependency=program_dependency,
            )
        if stage is StageKind.JUDGE:
            solves = tuple(
                item.packet
                for item in self.ledger.list(run_id)
                if item.packet.stage is StageKind.SOLVE and item.state is StageState.PERSISTED
            )
            if not solves:
                raise PreflightError("at least one solve result must persist before judge")
            return self._judge_packets(run_id, lock, calibration, solves)
        if stage is StageKind.FINAL_EVALUATE:
            if pilot:
                raise PreflightError("pilot runs cannot open held-out final evaluation")
            optimize = self._persisted_packet(run_id, StageKind.OPTIMIZE)
            if optimize is None:
                raise PreflightError("optimize must persist before final_evaluate")
            development = self._persisted_judges(run_id, split="development")
            if not development:
                raise PreflightError("development judgments must persist before final_evaluate")
            return (self._final_packet(run_id, lock, optimize, development),)
        if stage is StageKind.AGGREGATE:
            judges = self._persisted_judges(run_id)
            if not judges:
                raise PreflightError("judgments must persist before aggregate")
            return (self._aggregate_packet(run_id, lock, calibration, judges),)
        raise ResolutionError(f"unsupported stage: {stage.value}")

    def execute_packet(
        self,
        packet_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        executor_id: str | None = None,
    ) -> StageResult | None:
        packet = self.ledger.load_packet(packet_id)
        run = self.ledger.load_run(packet.run_id)
        lock = self.workspace.experiment_lock(
            _string(run.get("experiment_id"), "run experiment_id"),
            environment=environment,
        )
        session = _ExecutionSession(
            self,
            lock,
            environment if environment is not None else self.workspace.repository.environment(),
        )
        return self._run_packet(packet, lock, session, executor_id=executor_id)

    def submit_file(
        self,
        packet_id: str,
        result_file: Path,
        *,
        executor_id: str,
        worker_id: str,
        lease_id: str,
    ) -> StageResult:
        packet = self.ledger.load_packet(packet_id)
        if not result_file.is_file() or result_file.is_symlink():
            raise ResolutionError(f"stage result file does not exist: {result_file}")
        payload = json.loads(result_file.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ConfigurationError("stage result file must contain one JSON object")
        if executor_id != packet.executor.id:
            raise IntegrityError("submission executor does not match packet executor")
        output = self.ledger.put_json("result", payload)
        now = _timestamp()
        result = StageResult.create(
            packet=packet,
            status="completed",
            executor_version=None,
            worker_id=worker_id,
            outputs=(output,),
            usage={},
            started_at=now,
            completed_at=now,
            observed_model=packet.model.id,
        )
        self.ledger.submit(result, lease_id=lease_id)
        self.ledger.validate(packet.packet_id, self._validate_result)
        return self.ledger.persist(packet.packet_id)

    def _advance(
        self,
        run_id: str,
        lock: ExperimentLock,
        *,
        environment: Mapping[str, str],
        through: str | None,
        pilot: bool,
    ) -> RunProgress:
        if through is not None and through not in {item.value for item in StageKind}:
            raise ConfigurationError(f"unknown --through stage: {through}")
        session = _ExecutionSession(self, lock, environment)
        if through == StageKind.AUTHOR.value:
            author = self._author_packet(run_id, lock, campaign=None)
            if not self._ensure_executed(author, lock, session):
                return self._progress(
                    run_id, lock.configuration.id, "awaiting_execution", author.stage
                )
            return self._progress(run_id, lock.configuration.id, "paused", author.stage)
        calibration = self._calibration_packet(run_id, lock)
        if not self._ensure_executed(calibration, lock, session):
            return self._progress(run_id, lock.configuration.id, "awaiting_execution", calibration.stage)
        if not self._calibration_passed(calibration):
            return self._progress(run_id, lock.configuration.id, "calibration_failed", calibration.stage)
        if through == StageKind.CALIBRATE_JUDGE.value:
            return self._progress(run_id, lock.configuration.id, "paused", calibration.stage)

        program_dependency: StagePacket | None = None
        if lock.configuration.mode is ExperimentMode.OPTIMIZE:
            if pilot:
                raise IntegrityError("pilot execution cannot enter optimization")
            optimize = self._optimize_packet(run_id, lock, calibration)
            if not self._ensure_executed(optimize, lock, session):
                return self._progress(run_id, lock.configuration.id, "awaiting_execution", optimize.stage)
            if through == StageKind.OPTIMIZE.value:
                return self._progress(run_id, lock.configuration.id, "paused", optimize.stage)
            development_solves = self._solve_packets(
                run_id,
                lock,
                calibration,
                selected_split="development",
                program_dependency=optimize,
            )
            pending = self._execute_all(development_solves, lock, session)
            if pending is not None:
                return self._progress(run_id, lock.configuration.id, "awaiting_execution", pending.stage)
            if through == StageKind.SOLVE.value:
                return self._progress(run_id, lock.configuration.id, "paused", StageKind.SOLVE)
            development_judges = self._judge_packets(
                run_id, lock, calibration, development_solves
            )
            pending = self._execute_all(development_judges, lock, session)
            if pending is not None:
                return self._progress(run_id, lock.configuration.id, "awaiting_execution", pending.stage)
            if through == StageKind.JUDGE.value:
                return self._progress(run_id, lock.configuration.id, "paused", StageKind.JUDGE)
            final = self._final_packet(run_id, lock, optimize, development_judges)
            if not self._ensure_executed(final, lock, session):
                return self._progress(run_id, lock.configuration.id, "awaiting_execution", final.stage)
            if through == StageKind.FINAL_EVALUATE.value:
                return self._progress(run_id, lock.configuration.id, "paused", final.stage)
            program_dependency = final
            solves = self._solve_packets(
                run_id,
                lock,
                calibration,
                selected_split="test",
                program_dependency=program_dependency,
            )
        else:
            solves = self._solve_packets(
                run_id,
                lock,
                calibration,
                selected_splits=("train", "development") if pilot else None,
            )

        pending = self._execute_all(solves, lock, session)
        if pending is not None:
            return self._progress(run_id, lock.configuration.id, "awaiting_execution", pending.stage)
        if through == StageKind.SOLVE.value:
            return self._progress(run_id, lock.configuration.id, "paused", StageKind.SOLVE)
        judges = self._judge_packets(run_id, lock, calibration, solves)
        pending = self._execute_all(judges, lock, session)
        if pending is not None:
            return self._progress(run_id, lock.configuration.id, "awaiting_execution", pending.stage)
        if through == StageKind.JUDGE.value:
            return self._progress(run_id, lock.configuration.id, "paused", StageKind.JUDGE)
        all_judges = self._persisted_judges(run_id)
        aggregate = self._aggregate_packet(run_id, lock, calibration, all_judges)
        if not self._ensure_executed(aggregate, lock, session):
            return self._progress(run_id, lock.configuration.id, "awaiting_execution", aggregate.stage)
        aggregate_status = self.ledger.status(aggregate.packet_id)
        assert aggregate_status.result is not None
        result = self.ledger.read_output(aggregate_status.result)
        assert isinstance(result, Mapping)
        return self._progress(
            run_id,
            lock.configuration.id,
            "pilot_completed" if pilot else "completed",
            aggregate.stage,
            result=result,
        )

    def _execute_all(
        self,
        packets: Sequence[StagePacket],
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> StagePacket | None:
        for packet in packets:
            if not self._ensure_executed(packet, lock, session):
                return packet
        return None

    def _ensure_executed(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> bool:
        status = self.ledger.status(packet.packet_id)
        if status.state is StageState.PERSISTED:
            return True
        if status.state in {StageState.FAILED, StageState.REJECTED}:
            return False
        return self._run_packet(packet, lock, session) is not None

    def _run_packet(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
        *,
        executor_id: str | None = None,
    ) -> StageResult | None:
        configuration = self._executor(packet)
        selected = executor_id or configuration.id
        if selected != configuration.id:
            raise IntegrityError("executor override does not match immutable packet identity")
        if configuration.adapter == "artifact_exchange":
            return None
        claim = self.ledger.claim(
            packet.packet_id,
            executor_id=configuration.id,
            worker_id=f"coordinator-{uuid4()}",
            lease_seconds=packet.budget.wall_seconds,
        )
        started = _timestamp()
        try:
            output = self._automatic_output(packet, lock, session, configuration)
            artifact = self.ledger.put_json("result", output.value)
            result = StageResult.create(
                packet=packet,
                status="completed",
                executor_version=output.version or configuration.version,
                worker_id=claim.worker_id,
                outputs=(artifact,),
                usage=output.usage,
                provenance=output.provenance,
                started_at=started,
                completed_at=_timestamp(),
                observed_model=output.observed_model,
            )
        except HarnessExecutionPending:
            return None
        except AutomaticExecutionFailed as error:
            failed = StageResult.create(
                packet=packet,
                status="failed",
                executor_version=error.version or configuration.version,
                worker_id=claim.worker_id,
                usage=error.usage,
                provenance=error.provenance,
                started_at=started,
                completed_at=_timestamp(),
                observed_model=error.observed_model or packet.model.id,
                error=StageError(
                    error.kind,
                    self._redacted_error(error, session.environment),
                ),
            )
            self.ledger.submit(failed, lease_id=claim.lease_id)
            return None
        except Exception as error:
            failed = StageResult.create(
                packet=packet,
                status="failed",
                executor_version=configuration.version,
                worker_id=claim.worker_id,
                usage={},
                started_at=started,
                completed_at=_timestamp(),
                observed_model=packet.model.id,
                error=StageError(
                    type(error).__name__.lower(),
                    self._redacted_error(error, session.environment),
                ),
            )
            self.ledger.submit(failed, lease_id=claim.lease_id)
            return None
        self.ledger.submit(result, lease_id=claim.lease_id)
        try:
            self.ledger.validate(packet.packet_id, self._validate_result)
        except Exception:
            return None
        return self.ledger.persist(packet.packet_id)

    def _automatic_output(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
        executor: ExecutorConfiguration,
    ) -> ExecutorOutput:
        if executor.adapter == "codex_cli":
            from ms_agent_eval.adapters.codex_cli import CodexCliExecutor

            return CodexCliExecutor(self.workspace, lock, executor).execute(
                packet, self.ledger
            )
        if executor.adapter == "local":
            return ExecutorOutput(
                self._execute_local(packet, lock), {}, executor.version, None
            )
        if executor.adapter != "openai_compatible":
            raise ConfigurationError(f"unsupported executor adapter: {executor.adapter}")
        if packet.stage is StageKind.AUTHOR:
            return self._execute_author(packet, session)
        if packet.stage is StageKind.CALIBRATE_JUDGE:
            return self._execute_calibration(packet, lock, session)
        if packet.stage is StageKind.SOLVE:
            return self._execute_solve(packet, lock, session)
        if packet.stage is StageKind.JUDGE:
            return self._execute_judge(packet, lock, session)
        raise ConfigurationError(
            f"openai_compatible cannot automatically execute {packet.stage.value}"
        )

    def _execute_author(
        self, packet: StagePacket, session: _ExecutionSession
    ) -> ExecutorOutput:
        authoring_context = _mapping(self._input(packet, "authoring_context"), "author input")
        program = create_author_program()
        lm, observer = session.lm("author", StageKind.AUTHOR)
        result = self.dspy.execute(
            contract=DspyExecutionContract(
                role="author",
                program_hash=program_hash(program),
                inputs={
                    "authoring_context": json.dumps(
                        authoring_context, ensure_ascii=False, sort_keys=True
                    )
                },
                required_outputs=("candidate_packages",),
                primary_output=None,
            ),
            program=program,
            lm=lm,
            observer=observer,
        )
        if result.status != "completed":
            raise IntegrityError(f"author DSPy call failed: {result.error_kind}")
        packages = _list(result.outputs.get("candidate_packages"), "candidate_packages")
        return ExecutorOutput(
            {"candidate_packages": list(packages)},
            _usage(observer.records),
            None,
            packet.model.id,
        )

    def _execute_calibration(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> ExecutorOutput:
        judge, observer = self._judge(
            lock,
            session,
            StageKind.CALIBRATE_JUDGE,
            repetitions=self._judge_repetitions(packet.run_id),
        )
        start = len(observer.records)
        result = calibrate_judge(
            judge,
            self.workspace.calibration,
            self.workspace.case_bank,
            self.workspace.context.skill,
        )
        value = json_value(result)
        assert isinstance(value, Mapping)
        return ExecutorOutput(
            value,
            _usage(observer.records[start:]),
            None,
            lock.models["judge"].model,
        )

    def _execute_solve(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> ExecutorOutput:
        values = _mapping(self._input(packet, "case"), "solve input")
        program = create_solver_program()
        compiled = next((item for item in packet.inputs if item.id == "compiled_state"), None)
        if compiled is not None:
            reference = self.artifacts.reference(compiled.content_id, compiled.media_type)
            with tempfile.TemporaryDirectory() as directory:
                state_file = Path(directory) / "state.json"
                with self.artifacts.get_blob(reference) as source:
                    state_file.write_bytes(source.read())
                load_state_json(program, state_file)
        lm, observer = session.lm("solver", StageKind.SOLVE)
        start = len(observer.records)
        result = self.dspy.execute(
            contract=DspyExecutionContract(
                role="solver",
                program_hash=program_hash(program),
                inputs={
                    "global_context": values["global_context"],
                    "skill_context": values["skill_context"],
                    "task": values["task"],
                },
                required_outputs=("response",),
                primary_output="response",
            ),
            program=program,
            lm=lm,
            observer=observer,
        )
        if result.status != "completed" or result.primary_response is None:
            raise IntegrityError(f"solver DSPy call failed: {result.error_kind}")
        return ExecutorOutput(
            {
                "case_id": values["case_id"],
                "split": values["split"],
                "repetition": values["repetition"],
                "response": result.primary_response,
                "program_hash": program_hash(program),
                "call_ids": [item.call_id for item in result.calls],
            },
            _usage(observer.records[start:]),
            None,
            lock.models["solver"].model,
        )

    def _execute_judge(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> ExecutorOutput:
        values = _mapping(self._input(packet, "evaluation"), "judge input")
        case_id = _string(values.get("case_id"), "judge case_id")
        case = next((item for item in self.workspace.case_bank.cases if item.id == case_id), None)
        if case is None:
            raise IntegrityError(f"judge packet references unknown case {case_id!r}")
        judge, observer = self._judge(
            lock,
            session,
            StageKind.JUDGE,
            repetitions=self._judge_repetitions(packet.run_id),
        )
        start = len(observer.records)
        result = judge.evaluate(
            case,
            _string(values.get("candidate_response"), "candidate response"),
            skill_context=_string(values.get("skill_context"), "skill context"),
        )
        value = json_value(result)
        assert isinstance(value, Mapping)
        return ExecutorOutput(
            {**value, "split": values["split"], "repetition": values["repetition"]},
            _usage(observer.records[start:]),
            None,
            lock.models["judge"].model,
        )

    def _execute_local(
        self, packet: StagePacket, lock: ExperimentLock
    ) -> Mapping[str, object]:
        if packet.stage is StageKind.OPTIMIZE:
            experiment = lock.configuration
            if experiment.optimizer is None:
                raise IntegrityError("optimization stage has no optimizer")
            by_split = self._cases_by_split()
            for split in ("train", "development", "test"):
                if not by_split[split]:
                    raise PreflightError(f"optimization requires a non-empty {split} split")
            raw_k = experiment.optimizer.parameters.get("k", 2)
            raw_seed = experiment.optimizer.parameters.get("seed", 0)
            if not isinstance(raw_k, int) or isinstance(raw_k, bool) or raw_k < 1:
                raise PreflightError("LabeledFewShot k must be a positive integer")
            if not isinstance(raw_seed, int) or isinstance(raw_seed, bool):
                raise PreflightError("LabeledFewShot seed must be an integer")
            train = [
                dspy.Example(
                    global_context=self.workspace.context.global_context,
                    skill_context=self.workspace.context.skill(case.skill),
                    task=case.prompt,
                    response=case.expected_response,
                ).with_inputs("global_context", "skill_context", "task")
                for case in by_split["train"]
            ]
            previous = random.getstate()
            try:
                random.seed(raw_seed)
                compiled = dspy.LabeledFewShot(k=raw_k).compile(
                    create_solver_program(), trainset=train, sample=True
                )
            finally:
                random.setstate(previous)
            state = self.dspy.save_state_artifact(compiled)
            return {
                "state_artifact": json_value(state),
                "compiled_program_hash": program_hash(compiled),
                "train_case_hash": content_hash(
                    tuple((case.id, case.content_hash) for case in by_split["train"])
                ),
                "development_assignment_hash": content_hash(
                    tuple((case.id, case.content_hash) for case in by_split["development"])
                ),
                "state_format": "json",
                "published": True,
            }
        if packet.stage is StageKind.FINAL_EVALUATE:
            by_split = self._cases_by_split()
            return {
                "published": True,
                "compiled_packet": packet.dependencies[0],
                "test_assignment_hash": content_hash(
                    tuple((case.id, case.content_hash) for case in by_split["test"])
                ),
                "development_judgments": list(packet.dependencies[1:]),
            }
        if packet.stage is StageKind.AGGREGATE:
            cases = []
            role_usage: dict[str, Counter[str]] = {
                "solver": Counter(),
                "judge": Counter(),
            }
            for status in self.ledger.list(packet.run_id):
                if status.state is not StageState.PERSISTED or status.result is None:
                    continue
                role = None
                if status.packet.stage is StageKind.SOLVE:
                    role = "solver"
                elif status.packet.stage in {StageKind.CALIBRATE_JUDGE, StageKind.JUDGE}:
                    role = "judge"
                if status.packet.stage is StageKind.JUDGE:
                    evaluation = _mapping(
                        self.ledger.read_output(status.result), "judge output"
                    )
                    if (
                        lock.configuration.mode is not ExperimentMode.OPTIMIZE
                        or evaluation.get("split") == "test"
                    ):
                        cases.append(dict(evaluation))
                if role is not None:
                    for key, value in status.result.usage.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            role_usage[role][key] += value
            workspace = self.workspace.repository.workspace
            run = self.ledger.load_run(packet.run_id)
            execution_mode = _execution_mode(run)
            value: dict[str, object] = {
                "schema_version": 1,
                "run_id": packet.run_id,
                "experiment_id": packet.experiment_id,
                "lock_hash": lock.content_hash,
                "mode": lock.mode,
                "execution_mode": execution_mode,
                "benchmark_valid": execution_mode == "benchmark",
                "coverage_status": run.get("coverage_status", "unknown"),
                "evaluated_splits": sorted(
                    {
                        str(item["split"])
                        for item in cases
                        if isinstance(item.get("split"), str)
                    }
                ),
                "cases": cases,
                "case_count": len(cases),
                "role_usage": {
                    role: dict(values) for role, values in role_usage.items()
                },
                "independent": (
                    workspace.stages["solve"].executor
                    != workspace.stages["judge"].executor
                ),
            }
            if execution_mode == "benchmark":
                value["mean_score"] = (
                    sum(float(item["score"]) for item in cases) / len(cases)
                    if cases
                    else 0.0
                )
                value["pass_rate"] = (
                    sum(bool(item["passed"]) for item in cases) / len(cases)
                    if cases
                    else 0.0
                )
            else:
                value["diagnostic_only"] = True
                value["benchmark_metrics"] = None
            return value
        raise ConfigurationError(f"local executor cannot execute {packet.stage.value}")

    def _judge(
        self,
        lock: ExperimentLock,
        session: _ExecutionSession,
        stage: StageKind,
        *,
        repetitions: int | None = None,
    ) -> tuple[LlmJudge, ModelCallObserver]:
        lm, observer = session.lm("judge", stage)
        program = create_judge_program()

        def observed(**inputs: object) -> Mapping[str, object]:
            result = self.dspy.execute(
                contract=DspyExecutionContract(
                    role="judge",
                    program_hash=program_hash(program),
                    inputs=inputs,
                    required_outputs=("criterion_scores", "hard_failures", "feedback"),
                    primary_output=None,
                ),
                program=program,
                lm=lm,
                observer=observer,
            )
            if result.status != "completed":
                raise IntegrityError(f"judge DSPy call failed: {result.error_kind}")
            return {**result.outputs, "call_ids": [item.call_id for item in result.calls]}

        return (
            LlmJudge(
                observed,
                model_hash=lock.models["judge"].content_hash,
                program_hash=lock.judge_program_hash,
                repetitions=(
                    repetitions
                    if repetitions is not None
                    else self.workspace.repository.workspace.evaluation.judge.repetitions
                ),
            ),
            observer,
        )

    def _judge_repetitions(self, run_id: str) -> int:
        return (
            1
            if _execution_mode(self.ledger.load_run(run_id)) == "pilot"
            else self.workspace.repository.workspace.evaluation.judge.repetitions
        )

    def _author_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        *,
        campaign: str | None,
    ) -> StagePacket:
        value = {
            "authoring_instructions": self.workspace.repository.case_authoring_instructions(),
            "capability_catalog": json_value(self.workspace.capability_catalog),
            "global_context": self.workspace.context.global_context,
            "skill_contexts": dict(self.workspace.context.skill_contexts),
            "source_evidence": self.workspace.context.supporting_source(
                "case-authoring", campaign or "complete capability coverage"
            ),
            "source_snapshot_hash": self.workspace.context.snapshot_hash,
            "campaign": campaign or "Author the next complete, grounded case campaign.",
        }
        return self._packet(
            run_id,
            lock,
            StageKind.AUTHOR,
            inputs=(self.ledger.put_json("authoring_context", value),),
            instructions={
                "task": "Produce candidate case packages only; review and promotion remain explicit.",
                "output": {"candidate_packages": "list[case-package]"},
            },
            dependencies=(),
            output_schema="authored-cases-v1",
        )

    def _calibration_packet(self, run_id: str, lock: ExperimentLock) -> StagePacket:
        cases = {item.id: item for item in self.workspace.case_bank.cases}
        fixtures = []
        for fixture in self.workspace.calibration.fixtures:
            case = cases[fixture.case_id]
            fixtures.append(
                {
                    "id": fixture.id,
                    "candidate_response": fixture.candidate_path.read_text(encoding="utf-8"),
                    "case": self._judge_case_input(case),
                }
            )
        return self._packet(
            run_id,
            lock,
            StageKind.CALIBRATE_JUDGE,
            inputs=(
                self.ledger.put_json(
                    "calibration",
                    {
                        "corpus_hash": self.workspace.calibration.content_hash,
                        "fixtures": fixtures,
                    },
                ),
            ),
            instructions={
                "task": "Judge every locked fixture and report whether all calibration checks pass.",
                "output": "CalibrationResult",
            },
            dependencies=(),
            output_schema="judge-calibration-v1",
        )

    def _optimize_packet(
        self, run_id: str, lock: ExperimentLock, calibration: StagePacket
    ) -> StagePacket:
        train = [
            {
                "case_id": case.id,
                "split": "train",
                "global_context": self.workspace.context.global_context,
                "skill_context": self.workspace.context.skill(case.skill),
                "task": case.prompt,
                "expected_response": case.expected_response,
            }
            for case in self._cases_by_split()["train"]
        ]
        return self._packet(
            run_id,
            lock,
            StageKind.OPTIMIZE,
            inputs=(self.ledger.put_json("training_set", {"cases": train}),),
            instructions={
                "task": "Compile and publish a state-only DSPy artifact from train cases.",
                "optimizer": json_value(lock.configuration.optimizer),
                "forbidden_splits": ["test", "challenge"],
            },
            dependencies=(calibration.packet_id,),
            output_schema="compiled-program-v1",
        )

    def _solve_packets(
        self,
        run_id: str,
        lock: ExperimentLock,
        calibration: StagePacket,
        *,
        selected_split: str | None = None,
        selected_splits: Sequence[str] | None = None,
        program_dependency: StagePacket | None = None,
    ) -> tuple[StagePacket, ...]:
        if selected_split is not None and selected_splits is not None:
            raise IntegrityError("solve selection cannot use singular and plural splits")
        split_selection = (
            None
            if selected_split is None and selected_splits is None
            else frozenset(
                (selected_split,) if selected_split is not None else selected_splits or ()
            )
        )
        if split_selection is not None and not split_selection:
            raise IntegrityError("solve split selection must not be empty")
        if split_selection is not None and not split_selection <= {
            "train",
            "development",
            "test",
            "challenge",
        }:
            raise IntegrityError("solve split selection contains an unknown split")
        state_artifact = None
        solver_program_hash = lock.solver_program_hash
        dependencies = [calibration.packet_id]
        if program_dependency is not None:
            dependencies.append(program_dependency.packet_id)
            optimize = self._persisted_packet(run_id, StageKind.OPTIMIZE)
            if optimize is None:
                raise IntegrityError("compiled program dependency is missing")
            status = self.ledger.status(optimize.packet_id)
            assert status.result is not None
            output = _mapping(self.ledger.read_output(status.result), "optimize output")
            state = _mapping(output.get("state_artifact"), "compiled state artifact")
            solver_program_hash = _string(
                output.get("compiled_program_hash"), "compiled program hash"
            )
            state_artifact = StageArtifact(
                "compiled_state",
                _string(state.get("content_id"), "compiled state content_id"),
                _string(state.get("media_type"), "compiled state media_type"),
            )
        packets = []
        repetitions = 1 if selected_split is not None else lock.configuration.repetitions
        for case in self.workspace.case_bank.cases:
            split = self.workspace.splits.split_for(case)
            if split_selection is not None and split not in split_selection:
                continue
            for repetition in range(1, repetitions + 1):
                inputs = [
                    self.ledger.put_json(
                        "case",
                        {
                            "case_id": case.id,
                            "case_hash": case.content_hash,
                            "skill": case.skill,
                            "split": split,
                            "repetition": repetition,
                            "global_context": self.workspace.context.global_context,
                            "skill_context": self.workspace.context.skill(case.skill),
                            "task": case.prompt,
                            "solver_attachments": dict(case.solver_attachments()),
                            "program_hash": solver_program_hash,
                        },
                    )
                ]
                if state_artifact is not None:
                    inputs.append(state_artifact)
                packets.append(
                    self._packet(
                        run_id,
                        lock,
                        StageKind.SOLVE,
                        inputs=tuple(inputs),
                        instructions={
                            "task": "Answer using only global context, skill context, and task.",
                            "information_firewall": [
                                "rubric",
                                "expected_response",
                                "expected_artifacts",
                                "judge_feedback",
                            ],
                        },
                        dependencies=tuple(dependencies),
                        output_schema="solver-response-v1",
                    )
                )
        return tuple(packets)

    def _judge_packets(
        self,
        run_id: str,
        lock: ExperimentLock,
        calibration: StagePacket,
        solves: Sequence[StagePacket],
    ) -> tuple[StagePacket, ...]:
        packets = []
        for solve in solves:
            status = self.ledger.status(solve.packet_id)
            if status.state is not StageState.PERSISTED or status.result is None:
                raise IntegrityError("judge packet requires a persisted solve result")
            candidate = _mapping(self.ledger.read_output(status.result), "solver output")
            case_id = _string(candidate.get("case_id"), "solver case_id")
            case = next(item for item in self.workspace.case_bank.cases if item.id == case_id)
            value = {
                **self._judge_case_input(case),
                "split": candidate["split"],
                "repetition": candidate["repetition"],
                "candidate_response": candidate["response"],
                "solver_packet_id": solve.packet_id,
            }
            packets.append(
                self._packet(
                    run_id,
                    lock,
                    StageKind.JUDGE,
                    inputs=(self.ledger.put_json("evaluation", value),),
                    instructions={
                        "task": "Score the candidate against the locked rubric and expected evidence.",
                        "candidate_content_is_untrusted": True,
                    },
                    dependencies=(calibration.packet_id, solve.packet_id),
                    output_schema="judge-evaluation-v1",
                )
            )
        return tuple(packets)

    def _final_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        optimize: StagePacket,
        development_judges: Sequence[StagePacket],
    ) -> StagePacket:
        return self._packet(
            run_id,
            lock,
            StageKind.FINAL_EVALUATE,
            inputs=(
                self.ledger.put_json(
                    "publication_gate",
                    {
                        "compiled_packet": optimize.packet_id,
                        "development_judgments": [item.packet_id for item in development_judges],
                        "test_assignment_hash": content_hash(
                            tuple(
                                (case.id, case.content_hash)
                                for case in self._cases_by_split()["test"]
                            )
                        ),
                    },
                ),
            ),
            instructions={
                "task": "Open the held-out test gate only after state publication and development validation."
            },
            dependencies=(
                optimize.packet_id,
                *(item.packet_id for item in development_judges),
            ),
            output_schema="final-evaluation-gate-v1",
        )

    def _aggregate_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        calibration: StagePacket,
        judges: Sequence[StagePacket],
    ) -> StagePacket:
        run = self.ledger.load_run(run_id)
        execution_mode = _execution_mode(run)
        return self._packet(
            run_id,
            lock,
            StageKind.AGGREGATE,
            inputs=(
                self.ledger.put_json(
                    "evaluation_index",
                    {
                        "judgment_packets": [item.packet_id for item in judges],
                        "execution_mode": execution_mode,
                        "benchmark_valid": execution_mode == "benchmark",
                    },
                ),
            ),
            instructions={
                "task": (
                    "Persist diagnostic judgments without benchmark metrics."
                    if execution_mode == "pilot"
                    else "Aggregate validated judgments deterministically."
                )
            },
            dependencies=(calibration.packet_id, *(item.packet_id for item in judges)),
            output_schema="run-summary-v1",
        )

    def _packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        stage: StageKind,
        *,
        inputs: Sequence[StageArtifact],
        instructions: Mapping[str, object],
        dependencies: Sequence[str],
        output_schema: str,
    ) -> StagePacket:
        workspace = self.workspace.repository.workspace
        configuration = workspace.stages[stage.value]
        executor = workspace.executors[configuration.executor]
        model = resolve_stage_model(
            workspace,
            stage.value,
            role="judge" if stage in {StageKind.CALIBRATE_JUDGE, StageKind.JUDGE} else None,
        )
        packet = StagePacket.create(
            run_id=run_id,
            experiment_id=lock.configuration.id,
            stage=stage,
            attempt=1,
            dependencies=dependencies,
            inputs=inputs,
            instructions=self.ledger.put_json("instructions", instructions),
            output_schema=output_schema,
            executor=StageExecutorIdentity(
                executor.id, executor.adapter, content_hash(executor)
            ),
            model=StageModelIdentity(
                None if model is None else model.model,
                None if model is None else model.content_hash,
            ),
            budget=StageBudget(
                configuration.budget.model_calls,
                configuration.budget.tokens,
                configuration.budget.wall_seconds,
            ),
        )
        self._validate_packet(packet)
        matching_attempts = [
            item.packet
            for item in self.ledger.list(run_id)
            if self._same_stage_work(item.packet, packet)
        ]
        if matching_attempts:
            return max(matching_attempts, key=lambda item: item.attempt)
        return self.ledger.prepare(packet)

    @staticmethod
    def _same_stage_work(left: StagePacket, right: StagePacket) -> bool:
        """Match retry attempts while excluding their attempt-derived packet ids."""

        return (
            left.run_id == right.run_id
            and left.experiment_id == right.experiment_id
            and left.stage is right.stage
            and left.dependencies == right.dependencies
            and left.inputs == right.inputs
            and left.instructions == right.instructions
            and left.output_schema == right.output_schema
            and left.executor == right.executor
            and left.model == right.model
            and left.budget == right.budget
        )

    def _validate_packet(self, packet: StagePacket) -> None:
        if packet.stage is StageKind.SOLVE:
            if {item.id for item in packet.inputs} - {"case", "compiled_state"}:
                raise IntegrityError("solve packet contains an unauthorized input")
            forbidden = {
                "rubric",
                "expected_response",
                "expected_artifacts",
                "candidate_response",
                "judge_feedback",
            }
            if _contains_key(self._input(packet, "case"), forbidden):
                raise IntegrityError("solve packet crosses the hidden judging-material firewall")
        if packet.stage is StageKind.CALIBRATE_JUDGE:
            forbidden = {"label", "score_range", "minimum_score", "maximum_score"}
            if _contains_key(self._input(packet, "calibration"), forbidden):
                raise IntegrityError(
                    "calibration packet exposes coordinator-only labels or score ranges"
                )
        if packet.stage is StageKind.OPTIMIZE:
            if _contains_value(self._input(packet, "training_set"), {"test", "challenge"}):
                raise IntegrityError("optimization packet contains a held-out split")

    def _validate_result(self, packet: StagePacket, result: StageResult) -> None:
        if result.status != "completed":
            raise IntegrityError("failed result cannot be validated")
        self._validate_result_budget(packet, result)
        if len(result.outputs) != 1 or result.outputs[0].id != "result":
            raise IntegrityError("stage result must contain exactly one result output")
        payload = _mapping(self.ledger.read_output(result), f"{packet.output_schema} output")
        if packet.output_schema == "authored-cases-v1":
            _list(payload.get("candidate_packages"), "candidate_packages")
        elif packet.output_schema == "judge-calibration-v1":
            if not isinstance(payload.get("passed"), bool):
                raise IntegrityError("judge calibration output requires boolean passed")
            scores = _mapping(payload.get("fixture_scores"), "fixture_scores")
            passes = _mapping(payload.get("fixture_passes"), "fixture_passes")
            calibration = _mapping(self._input(packet, "calibration"), "calibration input")
            corpus_hash = _string(calibration.get("corpus_hash"), "calibration corpus_hash")
            if payload.get("corpus_hash") != corpus_hash:
                raise IntegrityError("judge calibration output has the wrong corpus hash")
            expected_ids = {item.id for item in self.workspace.calibration.fixtures}
            if set(scores) != expected_ids or set(passes) != expected_ids:
                raise IntegrityError("judge calibration must score every fixture exactly")
            for fixture_id, score in scores.items():
                if not isinstance(score, (int, float)) or isinstance(score, bool):
                    raise IntegrityError("judge calibration scores must be numeric")
                if not 0 <= float(score) <= 1:
                    raise IntegrityError("judge calibration scores must be in [0, 1]")
                if not isinstance(passes[fixture_id], bool):
                    raise IntegrityError("judge calibration pass decisions must be boolean")
            expected_result = calibration_result_from_scores(
                self.workspace.calibration,
                {key: float(value) for key, value in scores.items()},
                fixture_passes={key: bool(value) for key, value in passes.items()},
            )
            if payload.get("passed") is not expected_result.passed:
                raise IntegrityError("judge calibration pass decision was not deterministic")
            if payload.get("content_hash") != expected_result.content_hash:
                raise IntegrityError("judge calibration result identity hash is invalid")
        elif packet.output_schema == "solver-response-v1":
            for field in ("case_id", "split", "response", "program_hash"):
                _string(payload.get(field), f"solver response {field}")
            if not isinstance(payload.get("repetition"), int):
                raise IntegrityError("solver response repetition must be an integer")
            case_input = _mapping(self._input(packet, "case"), "solve case input")
            for field in ("case_id", "split", "repetition", "program_hash"):
                if payload.get(field) != case_input.get(field):
                    raise IntegrityError(f"solver response {field} does not match its packet")
        elif packet.output_schema == "judge-evaluation-v1":
            _string(payload.get("case_id"), "judge evaluation case_id")
            score = payload.get("score")
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not 0 <= score <= 1
            ):
                raise IntegrityError("judge evaluation score must be in [0, 1]")
            if not isinstance(payload.get("passed"), bool):
                raise IntegrityError("judge evaluation passed must be boolean")
            _list(payload.get("votes"), "judge evaluation votes")
        elif packet.output_schema == "compiled-program-v1":
            if payload.get("state_format") != "json" or payload.get("published") is not True:
                raise IntegrityError("compiled program must be published state-only JSON")
            state = _mapping(payload.get("state_artifact"), "state_artifact")
            self.artifacts.reference(
                _string(state.get("content_id"), "state artifact content_id"),
                _string(state.get("media_type"), "state artifact media_type"),
            )
        elif packet.output_schema == "final-evaluation-gate-v1":
            if payload.get("published") is not True:
                raise IntegrityError("final evaluation gate must be published")
            _string(payload.get("test_assignment_hash"), "test assignment hash")
        elif packet.output_schema == "run-summary-v1":
            _list(payload.get("cases"), "run summary cases")
            if payload.get("run_id") != packet.run_id:
                raise IntegrityError("run summary is bound to another run")
        else:
            raise IntegrityError(f"unknown output schema: {packet.output_schema}")

    @staticmethod
    def _validate_result_budget(packet: StagePacket, result: StageResult) -> None:
        for key, limit in (
            ("model_calls", packet.budget.model_calls),
            ("tokens", packet.budget.tokens),
        ):
            value = result.usage.get(key)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise IntegrityError(f"stage result usage {key} must be non-negative")
            if value > limit:
                raise IntegrityError(f"stage result exceeds its {key} budget")
        try:
            started = datetime.fromisoformat(_string(result.timing.get("started_at"), "started_at"))
            completed = datetime.fromisoformat(
                _string(result.timing.get("completed_at"), "completed_at")
            )
        except ValueError as error:
            raise IntegrityError("stage result timing must use ISO-8601 timestamps") from error
        duration = (completed - started).total_seconds()
        if duration < 0:
            raise IntegrityError("stage result completion precedes its start")
        if duration > packet.budget.wall_seconds:
            raise IntegrityError("stage result exceeds its wall_seconds budget")

    def _judge_case_input(self, case) -> Mapping[str, object]:  # type: ignore[no-untyped-def]
        return {
            "case_id": case.id,
            "case_hash": case.content_hash,
            "task": case.prompt,
            "skill_context": self.workspace.context.skill(case.skill),
            "rubric": case.rubric.prompt_text(),
            "expected_response": case.expected_response,
            "expected_artifacts": json.dumps(
                case.expected_artifacts(), ensure_ascii=False, sort_keys=True
            ),
        }

    def _input(self, packet: StagePacket, input_id: str) -> object:
        artifact = next((item for item in packet.inputs if item.id == input_id), None)
        if artifact is None:
            raise IntegrityError(f"packet has no {input_id!r} input")
        reference = self.artifacts.reference(artifact.content_id, artifact.media_type)
        with self.artifacts.get_blob(reference) as handle:
            return json.load(handle)

    def _executor(self, packet: StagePacket) -> ExecutorConfiguration:
        workspace = self.workspace.repository.workspace
        try:
            executor = workspace.executors[packet.executor.id]
        except KeyError as error:
            raise IntegrityError("packet executor is no longer configured") from error
        if content_hash(executor) != packet.executor.configuration_hash:
            raise IntegrityError("packet executor configuration has changed")
        return executor

    def _budget_ledger(self, stage: StageKind) -> BudgetLedger:
        budget: RoleBudget = self.workspace.repository.workspace.stages[stage.value].budget
        return BudgetLedger(
            BudgetLimits(
                model_calls=budget.model_calls,
                configured_cost=float("inf"),
                tokens=budget.tokens,
                wall_seconds=budget.wall_seconds,
                concurrency=1,
            )
        )

    def _persisted_packet(self, run_id: str, stage: StageKind) -> StagePacket | None:
        return next(
            (
                item.packet
                for item in self.ledger.list(run_id)
                if item.packet.stage is stage and item.state is StageState.PERSISTED
            ),
            None,
        )

    def _persisted_judges(
        self, run_id: str, *, split: str | None = None
    ) -> tuple[StagePacket, ...]:
        values = []
        for item in self.ledger.list(run_id):
            if item.packet.stage is not StageKind.JUDGE or item.state is not StageState.PERSISTED:
                continue
            if split is not None:
                assert item.result is not None
                output = _mapping(self.ledger.read_output(item.result), "judge output")
                if output.get("split") != split:
                    continue
            values.append(item.packet)
        return tuple(values)

    def _calibration_passed(self, packet: StagePacket) -> bool:
        status = self.ledger.status(packet.packet_id)
        if status.state is not StageState.PERSISTED or status.result is None:
            return False
        result = _mapping(self.ledger.read_output(status.result), "calibration output")
        return result.get("passed") is True

    def _cases_by_split(self):  # type: ignore[no-untyped-def]
        values = {key: [] for key in ("train", "development", "test", "challenge")}
        for case in self.workspace.case_bank.cases:
            values[self.workspace.splits.split_for(case)].append(case)
        return {key: tuple(items) for key, items in values.items()}

    def _progress(
        self,
        run_id: str,
        experiment_id: str,
        status: str,
        stage: StageKind | None,
        *,
        result: Mapping[str, object] | None = None,
    ) -> RunProgress:
        run = self.ledger.load_run(run_id)
        execution_mode = _execution_mode(run)
        packets = tuple(
            {
                "packet_id": item.packet.packet_id,
                "stage": item.packet.stage.value,
                "attempt": item.packet.attempt,
                "state": item.state.value,
                "executor": item.packet.executor.id,
                "model": item.packet.model.id,
            }
            for item in self.ledger.list(run_id)
        )
        return RunProgress(
            2,
            run_id,
            experiment_id,
            execution_mode,
            execution_mode == "benchmark",
            str(run.get("coverage_status", "unknown")),
            status,
            None if stage is None else stage.value,
            packets,
            result,
        )

    def _validate_calibration_split(
        self, lock: ExperimentLock, *, pilot: bool = False
    ) -> None:
        if lock.configuration.mode is not ExperimentMode.OPTIMIZE and not pilot:
            return
        cases = {case.id: case for case in self.workspace.case_bank.cases}
        held_out = sorted(
            fixture.id
            for fixture in self.workspace.calibration.fixtures
            if self.workspace.splits.split_for(cases[fixture.case_id])
            in {"test", "challenge"}
        )
        if held_out:
            raise PreflightError(
                "optimization and pilot judge calibration cannot use held-out "
                "test/challenge cases: "
                f"{held_out}"
            )

    @staticmethod
    def _redacted_error(error: Exception, environment: Mapping[str, str]) -> str:
        message = str(error)
        for key, value in environment.items():
            if ("KEY" in key or "TOKEN" in key or "SECRET" in key) and value:
                message = message.replace(value, "[REDACTED]")
        return message


def _usage(records: Sequence) -> Mapping[str, object]:  # type: ignore[type-arg]
    completed = [item for item in records if item.status == "completed"]
    failed = [item for item in records if item.status == "failed"]
    return {
        "model_calls": len(records),
        "completed_calls": len(completed),
        "failed_calls": len(failed),
        "tokens": sum(
            int(item.usage.get("total_tokens", 0) or 0)
            for item in completed
            if isinstance(item.usage.get("total_tokens", 0), int)
        ),
        "configured_cost": round(sum(item.configured_cost for item in completed), 10),
        "error_kinds": dict(Counter(item.error_kind for item in failed)),
    }


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _execution_mode(run: Mapping[str, object]) -> str:
    value = run.get("execution_mode")
    if value is None and run.get("schema_version") == 1:
        return "benchmark"
    if value not in {"benchmark", "pilot"}:
        raise IntegrityError("run execution_mode must be benchmark or pilot")
    expected_validity = value == "benchmark"
    if run.get("benchmark_valid") is not expected_validity:
        raise IntegrityError("run benchmark_valid contradicts its execution_mode")
    if value == "pilot" and run.get("selected_splits") != ["train", "development"]:
        raise IntegrityError("pilot run must be restricted to train/development splits")
    if value == "pilot" and run.get("judge_repetitions") != 1:
        raise IntegrityError("pilot run must use exactly one diagnostic judge repetition")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IntegrityError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise IntegrityError(f"{field} must be a list")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrityError(f"{field} must be a non-empty string")
    return value


def _contains_key(value: object, keys: set[str]) -> bool:
    if isinstance(value, Mapping):
        return bool(set(map(str, value)) & keys) or any(
            _contains_key(item, keys) for item in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_key(item, keys) for item in value)
    return False


def _contains_value(value: object, forbidden: set[str]) -> bool:
    if isinstance(value, str):
        return value in forbidden
    if isinstance(value, Mapping):
        return any(_contains_value(item, forbidden) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_value(item, forbidden) for item in value)
    return False
