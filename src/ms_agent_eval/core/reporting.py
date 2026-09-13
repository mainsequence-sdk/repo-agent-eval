from __future__ import annotations

import csv
import io
import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import IntegrityError
from .evaluation import CaseBank
from .hashing import content_hash, json_value
from .models import ArtifactReference
from .stages import StageArtifact, StageKind, StageLedger, StageState, StageStatus
from .workspace import WorkspaceRepository

if TYPE_CHECKING:
    from .planning import CompiledWorkspace


REPORT_SCHEMA_VERSION = 2
CSV_COLUMNS = (
    "report_schema_version",
    "workspace_id",
    "run_id",
    "experiment_id",
    "execution_mode",
    "benchmark_valid",
    "coverage_status",
    "source_commit",
    "split",
    "skill_name",
    "case_id",
    "case_title",
    "case_hash",
    "repetition",
    "author_model_details",
    "solver_model_details",
    "judge_model_details",
    "score",
    "passing_score",
    "passed",
    "scores_detail",
    "hard_failures",
    "judge_feedback",
    "usage_detail",
    "provenance_detail",
)
_JSON_CSV_COLUMNS = {
    "author_model_details",
    "solver_model_details",
    "judge_model_details",
    "scores_detail",
    "hard_failures",
    "usage_detail",
    "provenance_detail",
}


@dataclass(frozen=True)
class RunReport:
    """One canonical, versioned projection over persisted run evidence."""

    value: Mapping[str, object]

    @property
    def run_id(self) -> str:
        return _string(self.value.get("run_id"), "report run_id")

    @property
    def content_hash(self) -> str:
        return _string(self.value.get("content_hash"), "report content_hash")


@dataclass(frozen=True)
class PublishedReport:
    run_id: str
    directory: Path
    summary: Path
    csv: Path
    json: Path
    case_reports: tuple[Path, ...]
    content_hash: str
    changed: bool

    def as_dict(self) -> Mapping[str, object]:
        return {
            "run_id": self.run_id,
            "directory": self.directory.as_posix(),
            "summary": self.summary.as_posix(),
            "csv": self.csv.as_posix(),
            "json": self.json.as_posix(),
            "case_reports": [path.as_posix() for path in self.case_reports],
            "content_hash": self.content_hash,
            "changed": self.changed,
        }


class RunReportBuilder:
    """Normalize current staged evidence without invoking a model."""

    def __init__(
        self,
        workspace: CompiledWorkspace | WorkspaceRepository,
        ledger: StageLedger,
    ) -> None:
        self.workspace = workspace
        self.ledger = ledger

    @property
    def repository(self) -> WorkspaceRepository:
        repository = getattr(self.workspace, "repository", self.workspace)
        if not isinstance(repository, WorkspaceRepository):
            raise IntegrityError("report workspace has no repository configuration")
        return repository

    @property
    def case_bank(self) -> CaseBank:
        compiled_case_bank = getattr(self.workspace, "case_bank", None)
        if isinstance(compiled_case_bank, CaseBank):
            return compiled_case_bank
        return CaseBank.discover(self.repository.cases_root)

    def build(self, run_id: str) -> RunReport:
        run = self.ledger.load_run(run_id)
        lock = self._load_lock(run_id)
        self._validate_run_lock(run, lock, run_id)
        statuses = self.ledger.list(run_id)
        current_cases = {case.id: case for case in self.case_bank.cases}
        locked_cases = {
            _string(item.get("id"), "locked case id"): item
            for item in _mapping_sequence(lock.get("cases"), "experiment lock cases")
        }
        source = self._source_details(lock)
        models = {
            role: self._model_details(role, lock, statuses)
            for role in ("author", "solver", "judge")
        }
        case_results = []
        for status in statuses:
            if status.packet.stage is not StageKind.JUDGE:
                continue
            if status.state is not StageState.PERSISTED or status.result is None:
                continue
            evaluation = _mapping(
                self.ledger.read_output(status.result), "judge evaluation output"
            )
            judge_input = _mapping(
                self._read_input(status, "evaluation"), "judge evaluation input"
            )
            case_id = _string(evaluation.get("case_id"), "judge evaluation case_id")
            if judge_input.get("case_id") != case_id:
                raise IntegrityError(f"judge input and output disagree for case {case_id!r}")
            locked = locked_cases.get(case_id)
            if locked is None:
                raise IntegrityError(f"judge evaluation references unlocked case {case_id!r}")
            case_hash = _string(judge_input.get("case_hash"), "judge input case_hash")
            if locked.get("content_hash") != case_hash:
                raise IntegrityError(f"locked case hash disagrees for {case_id!r}")
            current = current_cases.get(case_id)
            if current is not None and current.content_hash != case_hash:
                current = None
            case_results.append(
                self._case_result(
                    status,
                    evaluation,
                    judge_input,
                    locked,
                    current,
                    models,
                )
            )
        case_results.sort(
            key=lambda item: (
                str(item["skill_name"]),
                str(item["case_id"]),
                int(item["repetition"]),
            )
        )
        aggregate = self._aggregate(statuses)
        self._validate_aggregate(run, lock, aggregate, case_results)
        stage_states = [self._stage_state(status) for status in statuses]
        execution_summary = self._execution_summary(statuses)
        report_status = self._report_status(statuses, aggregate)
        if aggregate is not None and run.get("execution_mode") == "pilot":
            report_status = "pilot_completed"
        usage_by_role = self._usage_by_role(statuses)
        skill_summary = self._skill_summary(case_results)
        criterion_summary = self._criterion_summary(case_results)
        benchmark_valid = run.get("benchmark_valid") is True
        identity: dict[str, object] = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "run_id": run_id,
            "experiment_id": _string(run.get("experiment_id"), "run experiment_id"),
            "workspace_id": _string(run.get("workspace_id"), "run workspace_id"),
            "created_at": _string(run.get("created_at"), "run created_at"),
            "status": report_status,
            "execution_mode": _string(run.get("execution_mode"), "run execution_mode"),
            "benchmark_valid": benchmark_valid,
            "diagnostic_only": not benchmark_valid,
            "coverage_status": str(run.get("coverage_status", "unknown")),
            "evaluated_splits": sorted(
                {str(item["split"]) for item in case_results if item.get("split")}
            ),
            "source": source,
            "identity": {
                "experiment_lock_hash": _string(
                    run.get("experiment_lock_hash"), "run experiment_lock_hash"
                ),
                "source_snapshot_hash": lock.get("source_snapshot_hash"),
                "case_bank_hash": lock.get("case_bank_hash"),
                "split_hash": lock.get("split_hash"),
                "calibration_hash": lock.get("calibration_hash"),
                "authoring_instructions_hash": lock.get("authoring_instructions_hash"),
                "capability_catalog_hash": lock.get("capability_catalog_hash"),
                "solver_program_hash": lock.get("solver_program_hash"),
                "judge_program_hash": lock.get("judge_program_hash"),
                "run_graph_id": (
                    None if aggregate is None else aggregate.get("run_graph_id")
                ),
            },
            "models": models,
            "usage_by_role": usage_by_role,
            "execution": execution_summary,
            "case_count": len(case_results),
            "cases": case_results,
            "skills": skill_summary,
            "criteria": criterion_summary,
            "stages": stage_states,
            "failures": [
                item
                for item in stage_states
                if item["state"] in {StageState.FAILED.value, StageState.REJECTED.value}
            ],
            "benchmark_metrics": None,
        }
        if benchmark_valid and aggregate is not None:
            identity["benchmark_metrics"] = {
                "mean_score": aggregate.get("mean_score"),
                "pass_rate": aggregate.get("pass_rate"),
            }
        return RunReport({**identity, "content_hash": content_hash(identity)})

    def _load_lock(self, run_id: str) -> Mapping[str, object]:
        run = self.ledger.load_run(run_id)
        artifact = _mapping(
            run.get("experiment_lock_artifact"), "experiment lock artifact"
        )
        try:
            reference = ArtifactReference(
                _string(artifact.get("content_id"), "experiment lock content_id"),
                _string(artifact.get("media_type"), "experiment lock media_type"),
                int(artifact.get("size_bytes", -1)),
                _string(artifact.get("relative_path"), "experiment lock relative_path"),
            )
            with self.ledger.store.get_blob(reference) as handle:
                value = json.load(handle)
        except (OSError, ValueError) as error:
            raise IntegrityError(f"experiment lock is invalid for run {run_id}") from error
        return _mapping(value, "experiment lock")

    @staticmethod
    def _validate_run_lock(
        run: Mapping[str, object], lock: Mapping[str, object], run_id: str
    ) -> None:
        if run.get("schema_version") != 4:
            raise IntegrityError("reporting requires run manifest schema version 4")
        if run.get("run_id") != run_id:
            raise IntegrityError("run manifest id disagrees with requested run")
        if run.get("workspace_id") != lock.get("workspace_id"):
            raise IntegrityError("run and experiment lock workspace ids disagree")
        if run.get("experiment_id") != _configuration_id(lock):
            raise IntegrityError("run and experiment lock experiment ids disagree")
        if run.get("experiment_lock_hash") != lock.get("content_hash"):
            raise IntegrityError("run manifest references a different experiment lock")
        if run.get("experiment_revision_id") != lock.get("content_hash"):
            raise IntegrityError("run manifest references a different experiment revision")

    def _source_details(self, lock: Mapping[str, object]) -> Mapping[str, object]:
        commit = _string(lock.get("source_commit"), "experiment lock source_commit")
        matches: list[Mapping[str, object]] = []
        snapshot_root = self.ledger.store.operations_root / "snapshots"
        for path in sorted(snapshot_root.glob("*/snapshot.lock.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(value, Mapping) and value.get("resolved_commit") == commit:
                matches.append({str(key): item for key, item in value.items()})
        snapshot = matches[0] if len(matches) == 1 else {}
        requested = snapshot.get("requested_ref")
        return {
            "repository_url": snapshot.get("repository_url"),
            "requested_ref": requested if isinstance(requested, Mapping) else None,
            "resolved_commit": commit,
            "snapshot_hash": lock.get("source_snapshot_hash"),
            "provenance_status": "resolved" if snapshot else "commit_only",
        }

    def _model_details(
        self,
        role: str,
        lock: Mapping[str, object],
        statuses: Sequence[StageStatus],
    ) -> Mapping[str, object]:
        raw_models = _mapping(lock.get("models", {}), "experiment lock models")
        raw = raw_models.get(role)
        model = _mapping(raw, f"experiment lock {role} model") if raw is not None else {}
        relevant = {
            "solver": {StageKind.SOLVE},
            "judge": {StageKind.CALIBRATE_JUDGE, StageKind.JUDGE},
            "author": {StageKind.AUTHOR},
        }[role]
        role_statuses = [item for item in statuses if item.packet.stage in relevant]
        observed = sorted(
            {
                value
                for item in role_statuses
                if item.result is not None
                and isinstance((value := item.result.model.get("observed_id")), str)
                and value
            }
        )
        configured_name = model.get("model") or model.get("name")
        deployment_revision = model.get("deployment_revision")
        provider = model.get("provider")
        configuration_hash = model.get("content_hash")
        parameters = _public_parameters(
            _mapping(model.get("parameters", {}), f"{role} parameters")
        )
        reasoning = model.get("reasoning_effort")
        if reasoning is None:
            reasoning = parameters.pop("reasoning_effort", None)
        executor_ids = sorted({item.packet.executor.id for item in role_statuses})
        executor_adapters = sorted({item.packet.executor.adapter for item in role_statuses})
        authoring_agent = None
        if role == "author":
            agents = {
                case.provenance.authoring_agent for case in self.case_bank.cases
            }
            authoring_agent = next(iter(agents)) if len(agents) == 1 else None
            if not model:
                configured = self.repository.workspace.models.get("author")
                stage = self.repository.workspace.stages.get("author")
                if configured is not None:
                    configured_name = configured.model
                    deployment_revision = configured.deployment_revision
                    parameters = _public_parameters(configured.parameters)
                    reasoning = configured.reasoning_effort
                if stage is not None:
                    executor_ids = [stage.executor]
                    configured_executor = self.repository.workspace.executors.get(stage.executor)
                    if configured_executor is not None:
                        executor_adapters = [configured_executor.adapter]
                        provider = configured_executor.adapter
                        configuration_hash = content_hash(
                            {
                                "role": role,
                                "model": configured,
                                "executor": configured_executor,
                            }
                        )
        observed_name: object = observed[0] if len(observed) == 1 else (observed or None)
        return {
            "role": role,
            **({"authoring_agent": authoring_agent} if role == "author" else {}),
            "configured_name": configured_name,
            "deployment_revision": deployment_revision,
            "observed_name": observed_name,
            "provider": provider,
            "reasoning_effort": reasoning,
            "parameters": parameters,
            "executor": _one_or_many(executor_ids),
            "executor_adapter": _one_or_many(executor_adapters),
            "configuration_hash": configuration_hash,
            "provenance_status": (
                "observed"
                if observed
                else (
                    "configured_only_observed_model_not_recorded"
                    if configured_name is not None
                    else "not_recorded"
                )
            ),
        }

    def _case_result(
        self,
        judge_status: StageStatus,
        evaluation: Mapping[str, object],
        judge_input: Mapping[str, object],
        locked: Mapping[str, object],
        current: object | None,
        models: Mapping[str, Mapping[str, object]],
    ) -> Mapping[str, object]:
        assert judge_status.result is not None
        case_id = _string(evaluation.get("case_id"), "evaluation case_id")
        solver_packet_id = _string(
            judge_input.get("solver_packet_id"), "judge input solver_packet_id"
        )
        solver_status = self.ledger.status(solver_packet_id)
        if (
            solver_status.packet.stage is not StageKind.SOLVE
            or solver_status.state is not StageState.PERSISTED
            or solver_status.result is None
        ):
            raise IntegrityError(f"judge case {case_id!r} lacks its persisted solver result")
        solver_output = _mapping(
            self.ledger.read_output(solver_status.result), "solver result output"
        )
        for field in ("case_id", "split", "repetition"):
            if solver_output.get(field) != judge_input.get(field):
                raise IntegrityError(f"solver and judge {field} disagree for {case_id!r}")
        raw_rubric = judge_input.get("rubric")
        try:
            rubric = json.loads(_string(raw_rubric, "judge input rubric"))
        except ValueError as error:
            raise IntegrityError(f"judge rubric is invalid JSON for case {case_id!r}") from error
        rubric_map = _mapping(rubric, "judge rubric")
        criteria = _mapping_sequence(rubric_map.get("criteria"), "judge rubric criteria")
        scores = {
            _string(item.get("id"), "criterion score id"): _number(
                item.get("score"), "criterion score"
            )
            for item in _mapping_sequence(
                evaluation.get("criterion_scores"), "evaluation criterion_scores"
            )
        }
        details = []
        for criterion in criteria:
            criterion_id = _string(criterion.get("id"), "rubric criterion id")
            if criterion_id not in scores:
                raise IntegrityError(f"evaluation lacks criterion {criterion_id!r}")
            weight = _number(criterion.get("weight"), "rubric criterion weight")
            details.append(
                {
                    "id": criterion_id,
                    "description": criterion.get("description"),
                    "weight": weight,
                    "score": scores[criterion_id],
                    "weighted_score": weight * scores[criterion_id],
                }
            )
        if set(scores) != {str(item["id"]) for item in details}:
            raise IntegrityError(f"evaluation has unknown criterion scores for {case_id!r}")
        score = _number(evaluation.get("score"), "evaluation score")
        calculated = sum(float(item["weighted_score"]) for item in details)
        if abs(score - calculated) > 1e-9:
            raise IntegrityError(f"weighted criterion score disagrees for {case_id!r}")
        hard_failures = _string_sequence(
            evaluation.get("hard_failures", []), "evaluation hard_failures"
        )
        votes = _mapping_sequence(evaluation.get("votes", []), "evaluation votes")
        repetition = _integer(evaluation.get("repetition"), "evaluation repetition")
        split = _string(evaluation.get("split"), "evaluation split")
        if locked.get("split") != split:
            raise IntegrityError(f"locked split disagrees for case {case_id!r}")
        current_title = getattr(current, "title", None)
        current_provenance = getattr(current, "provenance", None)
        raw_expected_artifacts = judge_input.get("expected_artifacts", "{}")
        if isinstance(raw_expected_artifacts, str):
            try:
                expected_artifacts = json.loads(raw_expected_artifacts)
            except ValueError as error:
                raise IntegrityError(
                    f"expected artifacts are invalid JSON for case {case_id!r}"
                ) from error
        else:
            expected_artifacts = raw_expected_artifacts
        return {
            "skill_name": _string(locked.get("skill"), "locked case skill"),
            "case_id": case_id,
            "case_title": current_title or case_id,
            "case_hash": _string(locked.get("content_hash"), "locked case hash"),
            "split": split,
            "repetition": repetition,
            "prompt": _string(judge_input.get("task"), "judge task"),
            "solver_response": _string(
                solver_output.get("response"), "solver response"
            ),
            "expected_response": _string(
                judge_input.get("expected_response"), "judge expected response"
            ),
            "expected_artifacts": expected_artifacts,
            "rubric": rubric_map,
            "criterion_scores": details,
            "score": score,
            "passing_score": _number(
                evaluation.get("passing_score"), "evaluation passing_score"
            ),
            "passed": _boolean(evaluation.get("passed"), "evaluation passed"),
            "hard_failures": hard_failures,
            "votes": votes,
            "judge_feedback": str(evaluation.get("feedback", "")),
            "models": models,
            "usage": {
                "solver": dict(solver_status.result.usage),
                "judge": dict(judge_status.result.usage),
            },
            "hashes": {
                "prompt": content_hash(judge_input.get("task")),
                "rubric": content_hash(rubric_map),
                "expected_response": content_hash(judge_input.get("expected_response")),
                "expected_artifacts": content_hash(expected_artifacts),
                "solver_result": solver_status.result.outputs[0].content_id,
                "judge_result": judge_status.result.outputs[0].content_id,
                "evaluation_record": evaluation.get("content_hash"),
            },
            "provenance": {
                "solver_packet_id": solver_packet_id,
                "solver_work_id": solver_status.packet.work_id,
                "solver_attempt_id": solver_status.packet.attempt_id,
                "solver_stage_result_id": solver_status.result.result_id,
                "solver_reused": solver_status.result.provenance.get("cache_hit") is True,
                "judge_packet_id": judge_status.packet.packet_id,
                "judge_work_id": judge_status.packet.work_id,
                "judge_attempt_id": judge_status.packet.attempt_id,
                "judge_stage_result_id": judge_status.result.result_id,
                "judge_reused": judge_status.result.provenance.get("cache_hit") is True,
                "authoring_agent": getattr(current_provenance, "authoring_agent", None),
                "case_workspace_match": current is not None,
            },
        }

    def _read_input(self, status: StageStatus, input_id: str) -> object:
        artifact = next(
            (item for item in status.packet.inputs if item.id == input_id), None
        )
        if artifact is None:
            raise IntegrityError(f"packet has no {input_id!r} input")
        return self._read_artifact(artifact)

    def _read_artifact(self, artifact: StageArtifact) -> object:
        reference = self.ledger.store.reference(artifact.content_id, artifact.media_type)
        with self.ledger.store.get_blob(reference) as handle:
            return json.load(handle)

    def _aggregate(
        self, statuses: Sequence[StageStatus]
    ) -> Mapping[str, object] | None:
        values = [
            status
            for status in statuses
            if status.packet.stage is StageKind.AGGREGATE
            and status.state is StageState.PERSISTED
            and status.result is not None
        ]
        if len(values) > 1:
            raise IntegrityError("run contains multiple persisted aggregate results")
        if not values:
            return None
        assert values[0].result is not None
        return _mapping(self.ledger.read_output(values[0].result), "aggregate output")

    @staticmethod
    def _validate_aggregate(
        run: Mapping[str, object],
        lock: Mapping[str, object],
        aggregate: Mapping[str, object] | None,
        cases: Sequence[Mapping[str, object]],
    ) -> None:
        if aggregate is None:
            return
        if aggregate.get("schema_version") != 2:
            raise IntegrityError("aggregate output schema version is not supported")
        if aggregate.get("run_id") != run.get("run_id"):
            raise IntegrityError("aggregate belongs to another run")
        if aggregate.get("lock_hash") != lock.get("content_hash"):
            raise IntegrityError("aggregate belongs to another experiment lock")
        if aggregate.get("benchmark_valid") is not run.get("benchmark_valid"):
            raise IntegrityError("aggregate benchmark validity disagrees with run")
        raw_cases = _mapping_sequence(aggregate.get("cases"), "aggregate cases")
        aggregate_identity = sorted(
            (
                str(item.get("case_id")),
                int(item.get("repetition", 0)),
                item.get("content_hash"),
            )
            for item in raw_cases
        )
        report_by_identity = {
            (
                str(item.get("case_id")),
                int(item.get("repetition", 0)),
                _mapping(item.get("hashes"), "report hashes").get("evaluation_record"),
            ): item
            for item in cases
        }
        if any(identity not in report_by_identity for identity in aggregate_identity):
            raise IntegrityError("aggregate cases disagree with persisted judge results")
        aggregate_report_cases = [report_by_identity[identity] for identity in aggregate_identity]
        if run.get("benchmark_valid") is True:
            mean_score = (
                sum(float(item["score"]) for item in aggregate_report_cases)
                / len(aggregate_report_cases)
                if aggregate_report_cases
                else 0.0
            )
            pass_rate = (
                sum(bool(item["passed"]) for item in aggregate_report_cases)
                / len(aggregate_report_cases)
                if aggregate_report_cases
                else 0.0
            )
            if (
                abs(_number(aggregate.get("mean_score"), "aggregate mean_score") - mean_score)
                > 1e-9
            ):
                raise IntegrityError("aggregate mean score is inconsistent")
            if (
                abs(_number(aggregate.get("pass_rate"), "aggregate pass_rate") - pass_rate)
                > 1e-9
            ):
                raise IntegrityError("aggregate pass rate is inconsistent")
        elif aggregate.get("benchmark_metrics") is not None:
            raise IntegrityError("pilot aggregate must not contain benchmark metrics")

    def _report_status(
        self,
        statuses: Sequence[StageStatus], aggregate: Mapping[str, object] | None
    ) -> str:
        if aggregate is not None:
            return "completed"
        calibration = next(
            (
                status
                for status in statuses
                if status.packet.stage is StageKind.CALIBRATE_JUDGE
                and status.state is StageState.PERSISTED
                and status.result is not None
            ),
            None,
        )
        if calibration is not None and calibration.result is not None:
            output = _mapping(
                self.ledger.read_output(calibration.result), "calibration output"
            )
            if output.get("passed") is False:
                return "calibration_failed"
        if any(
            status.state in {StageState.FAILED, StageState.REJECTED}
            for status in statuses
        ):
            return "failed"
        return "partial"

    @staticmethod
    def _stage_state(status: StageStatus) -> Mapping[str, object]:
        error = None
        if status.result is not None and status.result.error is not None:
            error = {
                "kind": status.result.error.kind,
                "message": status.result.error.message,
            }
        return {
            "stage": status.packet.stage.value,
            "work_id": status.packet.work_id,
            "attempt_id": status.packet.attempt_id,
            "packet_id": status.packet.packet_id,
            "attempt": status.packet.attempt,
            "state": status.state.value,
            "executor": status.packet.executor.id,
            "model": status.packet.model.id,
            "result_id": None if status.result is None else status.result.result_id,
            "reused": (
                status.result is not None
                and status.result.provenance.get("cache_hit") is True
            ),
            "source_result_id": (
                None
                if status.result is None
                else status.result.provenance.get("source_result_id")
            ),
            "error": error,
        }

    @staticmethod
    def _execution_summary(statuses: Sequence[StageStatus]) -> Mapping[str, object]:
        by_stage: dict[str, Counter[str]] = defaultdict(Counter)
        for status in statuses:
            if status.state is not StageState.PERSISTED or status.result is None:
                continue
            disposition = (
                "reused_this_run"
                if status.result.provenance.get("cache_hit") is True
                else "executed_this_run"
            )
            by_stage[status.packet.stage.value][disposition] += 1
        totals: Counter[str] = Counter()
        stages = []
        for stage in StageKind:
            values = by_stage.get(stage.value, Counter())
            executed = values["executed_this_run"]
            reused = values["reused_this_run"]
            if executed + reused == 0:
                continue
            totals["executed_this_run"] += executed
            totals["reused_this_run"] += reused
            stages.append(
                {
                    "stage": stage.value,
                    "executed_this_run": executed,
                    "reused_this_run": reused,
                }
            )
        return {**dict(totals), "stages": stages}

    @staticmethod
    def _usage_by_role(statuses: Sequence[StageStatus]) -> Mapping[str, object]:
        values: dict[str, Counter[str]] = {
            "author": Counter(),
            "solver": Counter(),
            "judge": Counter(),
        }
        for status in statuses:
            if status.result is None:
                continue
            role = {
                StageKind.AUTHOR: "author",
                StageKind.SOLVE: "solver",
                StageKind.CALIBRATE_JUDGE: "judge",
                StageKind.JUDGE: "judge",
            }.get(status.packet.stage)
            if role is None:
                continue
            for key, value in status.result.usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values[role][str(key)] += value
        return {role: dict(sorted(counter.items())) for role, counter in values.items()}

    @staticmethod
    def _skill_summary(
        cases: Sequence[Mapping[str, object]],
    ) -> list[Mapping[str, object]]:
        by_skill: dict[str, list[Mapping[str, object]]] = defaultdict(list)
        for item in cases:
            by_skill[str(item["skill_name"])].append(item)
        return [
            {
                "skill_name": skill,
                "case_count": len(items),
                "passed": sum(bool(item["passed"]) for item in items),
                "failed": sum(not bool(item["passed"]) for item in items),
                "mean_score": sum(float(item["score"]) for item in items) / len(items),
            }
            for skill, items in sorted(by_skill.items())
        ]

    @staticmethod
    def _criterion_summary(
        cases: Sequence[Mapping[str, object]],
    ) -> list[Mapping[str, object]]:
        values: dict[tuple[str, str], list[float]] = defaultdict(list)
        for case in cases:
            skill = str(case["skill_name"])
            for criterion in _mapping_sequence(
                case["criterion_scores"], "case criterion scores"
            ):
                values[(skill, str(criterion["id"]))].append(float(criterion["score"]))
        return [
            {
                "skill_name": skill,
                "criterion_id": criterion_id,
                "case_count": len(scores),
                "mean_score": sum(scores) / len(scores),
            }
            for (skill, criterion_id), scores in sorted(
                values.items(),
                key=lambda item: (sum(item[1]) / len(item[1]), item[0]),
            )
        ]


def publish_run_report(
    report: RunReport,
    summary_root: Path,
    *,
    replace: bool = False,
) -> PublishedReport:
    """Render and atomically publish all derived formats for one run."""

    files = _render_bundle(report)
    runs_root = summary_root / "runs"
    destination = runs_root / report.run_id
    runs_root.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing = _directory_bytes(destination)
        if existing == files:
            return _published(report, destination, files, changed=False)
        if not replace:
            raise IntegrityError(
                f"report output already exists with different bytes: {destination}; "
                "use --replace to rebuild it"
            )
    temporary = Path(tempfile.mkdtemp(prefix=f".{report.run_id}.", dir=runs_root))
    try:
        for relative, payload in files.items():
            path = temporary / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        if destination.exists():
            backup = Path(tempfile.mkdtemp(prefix=f".{report.run_id}.old.", dir=runs_root))
            backup.rmdir()
            os.replace(destination, backup)
            try:
                os.replace(temporary, destination)
            except BaseException:
                os.replace(backup, destination)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(temporary, destination)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return _published(report, destination, files, changed=True)


def build_and_publish_run_report(
    workspace: CompiledWorkspace | WorkspaceRepository,
    ledger: StageLedger,
    run_id: str,
    *,
    replace: bool = False,
) -> PublishedReport:
    report = RunReportBuilder(workspace, ledger).build(run_id)
    return publish_run_report(
        report,
        ledger.store.summary_root,
        replace=replace,
    )


def list_run_ids(ledger: StageLedger) -> tuple[str, ...]:
    root = ledger.store.manifest_root / "runs"
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            path.name
            for path in root.iterdir()
            if path.is_dir() and not path.is_symlink() and (path / "run.json").is_file()
        )
    )


def render_report_json(report: RunReport) -> bytes:
    return (
        json.dumps(report.value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )


def render_report_csv(report: RunReport) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for case in _case_sequence(report):
        models = _mapping(case.get("models"), "case models")
        row: dict[str, object] = {
            "report_schema_version": report.value["schema_version"],
            "workspace_id": report.value["workspace_id"],
            "run_id": report.value["run_id"],
            "experiment_id": report.value["experiment_id"],
            "execution_mode": report.value["execution_mode"],
            "benchmark_valid": str(report.value["benchmark_valid"]).lower(),
            "coverage_status": report.value["coverage_status"],
            "source_commit": _mapping(report.value["source"], "report source").get(
                "resolved_commit"
            ),
            "split": case["split"],
            "skill_name": case["skill_name"],
            "case_id": case["case_id"],
            "case_title": case["case_title"],
            "case_hash": case["case_hash"],
            "repetition": case["repetition"],
            "author_model_details": models.get("author"),
            "solver_model_details": models.get("solver"),
            "judge_model_details": models.get("judge"),
            "score": case["score"],
            "passing_score": case["passing_score"],
            "passed": str(case["passed"]).lower(),
            "scores_detail": {
                "criteria": case["criterion_scores"],
                "hard_failure_applied": bool(case["hard_failures"]),
                "judge_vote_count": len(_sequence(case["votes"], "case votes")),
            },
            "hard_failures": case["hard_failures"],
            "judge_feedback": case["judge_feedback"],
            "usage_detail": case["usage"],
            "provenance_detail": {
                **_mapping(case["provenance"], "case provenance"),
                "hashes": case["hashes"],
            },
        }
        encoded = {
            key: _compact_json(value) if key in _JSON_CSV_COLUMNS else _safe_cell(value)
            for key, value in row.items()
        }
        writer.writerow(encoded)
    return output.getvalue().encode("utf-8")


def render_report_markdown(report: RunReport) -> bytes:
    value = report.value
    source = _mapping(value.get("source"), "report source")
    metrics = value.get("benchmark_metrics")
    lines = [f"# Experiment run {value['run_id']}", ""]
    if value.get("benchmark_valid") is not True:
        lines.extend(
            [
                "> **Diagnostic pilot — not a valid benchmark.** Per-case scores are shown, "
                "but no benchmark mean or pass rate is reported.",
                "",
            ]
        )
    lines.extend(
        [
            "## Run",
            "",
            f"- Status: `{value['status']}`",
            f"- Experiment: `{value['experiment_id']}`",
            f"- Revision: `{_mapping(value['identity'], 'report identity').get('experiment_lock_hash')}`",
            f"- Run graph: `{_mapping(value['identity'], 'report identity').get('run_graph_id')}`",
            f"- Workspace: `{value['workspace_id']}`",
            f"- Execution mode: `{value['execution_mode']}`",
            f"- Benchmark valid: `{str(value['benchmark_valid']).lower()}`",
            f"- Coverage: `{value['coverage_status']}`",
            f"- Created: `{value['created_at']}`",
            f"- Source commit: `{source.get('resolved_commit')}`",
            f"- Evaluated splits: `{', '.join(value['evaluated_splits']) or 'none'}`",
            f"- Report hash: `{value['content_hash']}`",
            "",
        ]
    )
    if isinstance(metrics, Mapping):
        lines.extend(
            [
                "## Benchmark metrics",
                "",
                f"- Mean score: `{_format_score(metrics.get('mean_score'))}`",
                f"- Pass rate: `{_format_score(metrics.get('pass_rate'))}`",
                "",
            ]
        )
    lines.extend(["## Models", ""])
    for role in ("author", "solver", "judge"):
        model = _mapping(_mapping(value["models"], "report models")[role], f"{role} model")
        lines.append(
            f"- {role.title()}: configured `{model.get('configured_name')}`, observed "
            f"`{model.get('observed_name')}`, executor `{model.get('executor')}`, "
            f"reasoning `{model.get('reasoning_effort')}`, provenance "
            f"`{model.get('provenance_status')}`"
        )
    execution = _mapping(value["execution"], "report execution")
    lines.extend(
        [
            "",
            "## Execution reuse",
            "",
            f"- Executed this run: `{execution.get('executed_this_run', 0)}`",
            f"- Reused this run: `{execution.get('reused_this_run', 0)}`",
            "",
            "| Stage | Executed | Reused |",
            "| --- | ---: | ---: |",
        ]
    )
    for stage in _mapping_sequence(execution.get("stages", []), "execution stages"):
        lines.append(
            f"| {stage['stage']} | {stage['executed_this_run']} | {stage['reused_this_run']} |"
        )
    lines.extend(["", "## Incremental usage", ""])
    for role, usage in _mapping(value["usage_by_role"], "report usage").items():
        lines.append(f"- {str(role).title()}: `{_compact_json(usage)}`")
    lines.extend(
        [
            "",
            "## Skills",
            "",
            "| Skill | Cases | Passed | Failed | Mean score |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for skill in _mapping_sequence(value["skills"], "report skills"):
        lines.append(
            f"| {_table(skill['skill_name'])} | {skill['case_count']} | {skill['passed']} | "
            f"{skill['failed']} | {_format_score(skill['mean_score'])} |"
        )
    lines.extend(
        [
            "",
            "## Cases",
            "",
            "| Skill | Case | Split | Repetition | Score | Threshold | Result |",
            "| --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    filenames = _case_filenames(_case_sequence(report))
    for case in _case_sequence(report):
        key = (str(case["case_id"]), int(case["repetition"]))
        result = "PASS" if case["passed"] is True else "FAIL"
        lines.append(
            f"| {_table(case['skill_name'])} | "
            f"[{_table(case['case_title'])}](cases/{filenames[key]}) | "
            f"{_table(case['split'])} | {case['repetition']} | "
            f"{_format_score(case['score'])} | {_format_score(case['passing_score'])} | "
            f"{result} |"
        )
    failures = [case for case in _case_sequence(report) if case["passed"] is not True]
    lines.extend(["", "## Failed cases", ""])
    if not failures:
        lines.append("None.")
    else:
        for case in failures:
            lines.append(
                f"- `{case['case_id']}` repetition {case['repetition']}: "
                f"score {_format_score(case['score'])}; hard failures "
                f"`{_compact_json(case['hard_failures'])}`"
            )
    borderline = [
        case
        for case in _case_sequence(report)
        if abs(float(case["score"]) - float(case["passing_score"])) <= 0.05
    ]
    lines.extend(["", "## Borderline cases", ""])
    if not borderline:
        lines.append("None.")
    else:
        for case in borderline:
            lines.append(
                f"- `{case['case_id']}` repetition {case['repetition']}: "
                f"score {_format_score(case['score'])}, threshold "
                f"{_format_score(case['passing_score'])}."
            )
    lines.extend(
        [
            "",
            "## Criterion diagnostics",
            "",
            "Lowest arithmetic mean criterion scores appear first.",
            "",
            "| Skill | Criterion | Cases | Mean score |",
            "| --- | --- | ---: | ---: |",
        ]
    )
    for criterion in _mapping_sequence(value["criteria"], "report criteria"):
        lines.append(
            f"| {_table(criterion['skill_name'])} | `{_table(criterion['criterion_id'])}` | "
            f"{criterion['case_count']} | {_format_score(criterion['mean_score'])} |"
        )
    stage_failures = _mapping_sequence(value["failures"], "report failures")
    lines.extend(["", "## Execution failures", ""])
    if not stage_failures:
        lines.append("None.")
    else:
        for failure in stage_failures:
            lines.append(
                f"- `{failure['stage']}` / `{failure['packet_id']}`: "
                f"`{_compact_json(failure.get('error'))}`"
            )
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_case_markdown(report: RunReport, case: Mapping[str, object]) -> bytes:
    models = _mapping(case["models"], "case models")
    lines = [
        f"# {case['case_title']}",
        "",
        f"- Case: `{case['case_id']}`",
        f"- Skill: `{case['skill_name']}`",
        f"- Split: `{case['split']}`",
        f"- Repetition: `{case['repetition']}`",
        f"- Result: `{'PASS' if case['passed'] else 'FAIL'}`",
        f"- Score: `{_format_score(case['score'])}`",
        f"- Passing score: `{_format_score(case['passing_score'])}`",
        "",
        "## Prompt",
        "",
        str(case["prompt"]),
        "",
        "## Solver response",
        "",
        str(case["solver_response"]),
        "",
        "## Expected response",
        "",
        str(case["expected_response"]),
        "",
        "## Expected artifacts",
        "",
        f"```json\n{json.dumps(case['expected_artifacts'], ensure_ascii=False, indent=2, sort_keys=True)}\n```",
        "",
        "## Rubric scores",
        "",
        "| Criterion | Weight | Score | Weighted score | Description |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for criterion in _mapping_sequence(case["criterion_scores"], "criterion scores"):
        lines.append(
            f"| `{_table(criterion['id'])}` | {_format_score(criterion['weight'])} | "
            f"{_format_score(criterion['score'])} | "
            f"{_format_score(criterion['weighted_score'])} | "
            f"{_table(criterion.get('description'))} |"
        )
    lines.extend(
        [
            "",
            "## Judge feedback",
            "",
            str(case["judge_feedback"]) or "No feedback was recorded.",
            "",
            "## Hard failures",
            "",
            f"```json\n{json.dumps(case['hard_failures'], ensure_ascii=False, indent=2, sort_keys=True)}\n```",
            "",
            "## Judge votes",
            "",
            f"```json\n{json.dumps(case['votes'], ensure_ascii=False, indent=2, sort_keys=True)}\n```",
            "",
            "## Model identities",
            "",
            f"```json\n{json.dumps(models, ensure_ascii=False, indent=2, sort_keys=True)}\n```",
            "",
            "## Usage",
            "",
            f"```json\n{json.dumps(case['usage'], ensure_ascii=False, indent=2, sort_keys=True)}\n```",
            "",
            "## Provenance and hashes",
            "",
            f"```json\n{json.dumps({'provenance': case['provenance'], 'hashes': case['hashes']}, ensure_ascii=False, indent=2, sort_keys=True)}\n```",
        ]
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _render_bundle(report: RunReport) -> dict[str, bytes]:
    cases = _case_sequence(report)
    filenames = _case_filenames(cases)
    files = {
        "summary.md": render_report_markdown(report),
        "results.csv": render_report_csv(report),
        "results.json": render_report_json(report),
    }
    for case in cases:
        key = (str(case["case_id"]), int(case["repetition"]))
        files[f"cases/{filenames[key]}"] = render_case_markdown(report, case)
    return dict(sorted(files.items()))


def _case_filenames(
    cases: Sequence[Mapping[str, object]],
) -> Mapping[tuple[str, int], str]:
    counts = Counter(str(case["case_id"]) for case in cases)
    return {
        (str(case["case_id"]), int(case["repetition"])): (
            f"{case['case_id']}.md"
            if counts[str(case["case_id"])] == 1
            else f"{case['case_id']}-repetition-{case['repetition']}.md"
        )
        for case in cases
    }


def _published(
    report: RunReport,
    destination: Path,
    files: Mapping[str, bytes],
    *,
    changed: bool,
) -> PublishedReport:
    case_paths = tuple(
        destination / relative
        for relative in sorted(files)
        if relative.startswith("cases/")
    )
    return PublishedReport(
        report.run_id,
        destination,
        destination / "summary.md",
        destination / "results.csv",
        destination / "results.json",
        case_paths,
        report.content_hash,
        changed,
    )


def _directory_bytes(root: Path) -> dict[str, bytes]:
    if not root.is_dir() or root.is_symlink():
        raise IntegrityError(f"report destination is not a safe directory: {root}")
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _case_sequence(report: RunReport) -> list[Mapping[str, object]]:
    return _mapping_sequence(report.value.get("cases"), "report cases")


def _compact_json(value: object) -> str:
    return json.dumps(
        json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _safe_cell(value: object) -> object:
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _table(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _format_score(value: object) -> str:
    if value is None:
        return "null"
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def _configuration_id(lock: Mapping[str, object]) -> object:
    configuration = lock.get("configuration")
    if isinstance(configuration, Mapping):
        return configuration.get("id")
    return lock.get("id")


def _one_or_many(values: Sequence[str]) -> object:
    if not values:
        return None
    return values[0] if len(values) == 1 else list(values)


def _public_parameters(values: Mapping[str, object]) -> dict[str, object]:
    forbidden = ("api_key", "token", "secret", "password", "credential", "authorization")
    return {
        str(key): item
        for key, item in values.items()
        if not any(part in str(key).lower() for part in forbidden)
    }


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IntegrityError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _mapping_sequence(value: object, field: str) -> list[Mapping[str, object]]:
    return [_mapping(item, f"{field}[]") for item in _sequence(value, field)]


def _sequence(value: object, field: str) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise IntegrityError(f"{field} must be a sequence")
    return list(value)


def _string_sequence(value: object, field: str) -> list[str]:
    return [_string(item, f"{field}[]") for item in _sequence(value, field)]


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrityError(f"{field} must be a non-empty string")
    return value


def _number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise IntegrityError(f"{field} must be numeric")
    return float(value)


def _integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise IntegrityError(f"{field} must be an integer")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise IntegrityError(f"{field} must be boolean")
    return value
