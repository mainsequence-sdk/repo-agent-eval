from __future__ import annotations

import csv
import io
import json
import os
import random
import shutil
import statistics
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .endpoint_pool import endpoint_provenance_routes
from .errors import IntegrityError
from .evaluation import CaseBank
from .hashing import content_hash, json_value
from .models import ArtifactReference
from .stages import StageArtifact, StageKind, StageLedger, StageState, StageStatus
from .workspace import ExecutorType, WorkspaceRepository

if TYPE_CHECKING:
    from .planning import CompiledWorkspace


REPORT_SCHEMA_VERSION = 4
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
    "leakage_group",
    "repetition",
    "comparison_arm",
    "candidate_id",
    "strategy",
    "expected_response_tokens",
    "expected_response_length_band",
    "author_model_details",
    "solver_model_details",
    "judge_model_details",
    "score",
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
        locked_models = _mapping(lock.get("models", {}), "experiment lock models")
        report_roles = (
            ("author", "optimizer", "solver", "judge")
            if "optimizer" in locked_models
            else ("author", "solver", "judge")
        )
        models = {role: self._model_details(role, lock, statuses) for role in report_roles}
        aggregate = self._aggregate(statuses)
        token_measurements = self._token_measurements(aggregate)
        case_results = []
        for status in statuses:
            if status.packet.stage is not StageKind.JUDGE:
                continue
            if status.state is not StageState.PERSISTED or status.result is None:
                continue
            evaluation = _mapping(self.ledger.read_output(status.result), "judge evaluation output")
            if evaluation.get("evaluation_phase") not in {"test", "pilot"}:
                continue
            judge_input = _mapping(self._read_input(status, "evaluation"), "judge evaluation input")
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
                    token_measurements.get(case_id),
                )
            )
        case_results.sort(
            key=lambda item: (
                str(item["skill_name"]),
                str(item["case_id"]),
                int(item["repetition"]),
                str(item["comparison_arm"]),
            )
        )
        self._validate_aggregate(run, lock, aggregate, case_results)
        stage_states = [self._stage_state(status) for status in statuses]
        execution_summary = self._execution_summary(statuses)
        report_status = self._report_status(statuses, aggregate)
        if aggregate is not None and run.get("execution_mode") == "pilot":
            report_status = "pilot_completed"
        usage_by_role = self._usage_by_role(statuses)
        endpoint_execution = self._endpoint_summary(statuses)
        scored_cases = self._primary_scored_cases(case_results)
        skill_summary = self._skill_summary(scored_cases)
        criterion_summary = self._criterion_summary(scored_cases)
        test_comparison = self._test_comparison(case_results)
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
                "judge_validation_hash": lock.get("judge_validation_hash"),
                "authoring_instructions_hash": lock.get("authoring_instructions_hash"),
                "capability_catalog_hash": lock.get("capability_catalog_hash"),
                "solver_program_hash": lock.get("solver_program_hash"),
                "judge_program_hash": lock.get("judge_program_hash"),
                "benchmark_revision_id": lock.get("benchmark_revision_id"),
                "comparison_group_id": lock.get("comparison_group_id"),
                "comparison_id": lock.get("comparison_id"),
                "arm_id": lock.get("arm_id"),
                "run_graph_id": (None if aggregate is None else aggregate.get("run_graph_id")),
            },
            "models": models,
            "usage_by_role": usage_by_role,
            "endpoint_execution": endpoint_execution,
            "execution": execution_summary,
            "case_count": len(case_results),
            "cases": case_results,
            "expected_response_token_measurements": (
                None if aggregate is None else aggregate.get("expected_response_token_measurements")
            ),
            "test_comparison": test_comparison,
            "skills": skill_summary,
            "criteria": criterion_summary,
            "stages": stage_states,
            "failures": [
                item
                for item in stage_states
                if item["state"] in {StageState.FAILED.value, StageState.REJECTED.value}
            ],
            "benchmark_metrics": None,
            "cross_validation": self._cross_validation(statuses),
            "optimization": self._optimization(statuses, lock),
        }
        if benchmark_valid and aggregate is not None:
            identity["benchmark_metrics"] = {
                "mean_score": aggregate.get("mean_score"),
                **(
                    {}
                    if test_comparison is None
                    else {
                        "baseline_mean_score": test_comparison["baseline_mean_score"],
                        "optimized_mean_score": test_comparison["optimized_mean_score"],
                        "micro_case_delta": test_comparison["micro_case_delta"],
                        "macro_skill_delta": test_comparison["macro_skill_delta"],
                    }
                ),
            }
        return RunReport({**identity, "content_hash": content_hash(identity)})

    def _load_lock(self, run_id: str) -> Mapping[str, object]:
        run = self.ledger.load_run(run_id)
        artifact = _mapping(run.get("experiment_lock_artifact"), "experiment lock artifact")
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
            "judge": {StageKind.VALIDATE_JUDGE, StageKind.JUDGE},
            "author": {StageKind.AUTHOR},
            "optimizer": {StageKind.OPTIMIZE},
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
        profile_id = model.get("profile_id")
        deployment_id = model.get("deployment_id")
        deployment_revision = model.get("deployment_revision")
        provider = model.get("provider")
        configuration_hash = model.get("content_hash")
        parameters = _public_parameters(_mapping(model.get("parameters", {}), f"{role} parameters"))
        reasoning = model.get("reasoning_effort")
        if reasoning is None:
            reasoning = parameters.pop("reasoning_effort", None)
        executor_ids = sorted({item.packet.executor.id for item in role_statuses})
        executor_adapters = sorted({item.packet.executor.adapter for item in role_statuses})
        authoring_agent = None
        if role == "author":
            agents = {case.provenance.authoring_agent for case in self.case_bank.cases}
            authoring_agent = next(iter(agents)) if len(agents) == 1 else None
            if not model:
                configured = self.repository.workspace.models.get("author")
                stage = self.repository.workspace.stages.get("author")
                if configured is not None:
                    deployment = self.repository.workspace.deployments[configured.deployment]
                    profile_id = "author"
                    deployment_id = deployment.id
                    configured_name = deployment.model
                    deployment_revision = deployment.revision
                    parameters = _public_parameters(configured.parameters)
                    reasoning = configured.reasoning_effort
                if stage is not None and stage.executor is not None:
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
            "profile_id": profile_id,
            "deployment_id": deployment_id,
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
        token_measurement: Mapping[str, object] | None,
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
            "leakage_group": _string(locked.get("group"), "locked case leakage group"),
            "case_id": case_id,
            "case_title": current_title or case_id,
            "case_hash": _string(locked.get("content_hash"), "locked case hash"),
            "split": split,
            "repetition": repetition,
            "candidate_id": _string(
                evaluation.get("candidate_id", "baseline"), "evaluation candidate_id"
            ),
            "strategy": _string(evaluation.get("strategy", "baseline"), "evaluation strategy"),
            "comparison_arm": (
                "optimized" if evaluation.get("strategy") == "copro" else "baseline"
            ),
            "expected_response_tokens": (
                None if token_measurement is None else token_measurement.get("token_count")
            ),
            "expected_response_length_band": (
                None if token_measurement is None else token_measurement.get("length_band")
            ),
            "prompt": _string(judge_input.get("task"), "judge task"),
            "solver_response": _string(solver_output.get("response"), "solver response"),
            "expected_response": _string(
                judge_input.get("expected_response"), "judge expected response"
            ),
            "expected_artifacts": expected_artifacts,
            "rubric": rubric_map,
            "criterion_scores": details,
            "score": score,
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
        artifact = next((item for item in status.packet.inputs if item.id == input_id), None)
        if artifact is None:
            raise IntegrityError(f"packet has no {input_id!r} input")
        return self._read_artifact(artifact)

    def _read_artifact(self, artifact: StageArtifact) -> object:
        reference = self.ledger.store.reference(artifact.content_id, artifact.media_type)
        with self.ledger.store.get_blob(reference) as handle:
            return json.load(handle)

    def _aggregate(self, statuses: Sequence[StageStatus]) -> Mapping[str, object] | None:
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
    def _token_measurements(
        aggregate: Mapping[str, object] | None,
    ) -> Mapping[str, Mapping[str, object]]:
        if aggregate is None:
            return {}
        raw = aggregate.get("expected_response_token_measurements")
        if not isinstance(raw, Mapping):
            return {}
        cases = raw.get("cases", [])
        if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes, bytearray)):
            raise IntegrityError("expected-response token measurements cases must be a list")
        values: dict[str, Mapping[str, object]] = {}
        for item in cases:
            measurement = _mapping(item, "expected-response token measurement")
            case_id = _string(measurement.get("case_id"), "token measurement case id")
            if case_id in values:
                raise IntegrityError("expected-response token measurements contain duplicates")
            values[case_id] = measurement
        return values

    @staticmethod
    def _primary_scored_cases(
        cases: Sequence[Mapping[str, object]],
    ) -> list[Mapping[str, object]]:
        if any(item.get("comparison_arm") == "optimized" for item in cases):
            return [item for item in cases if item.get("comparison_arm") == "optimized"]
        return list(cases)

    @staticmethod
    def _test_comparison(
        cases: Sequence[Mapping[str, object]],
    ) -> Mapping[str, object] | None:
        if not any(item.get("comparison_arm") == "optimized" for item in cases):
            return None
        pairs: dict[tuple[str, int], dict[str, Mapping[str, object]]] = defaultdict(dict)
        for item in cases:
            arm = _string(item.get("comparison_arm"), "comparison arm")
            if arm not in {"baseline", "optimized"}:
                raise IntegrityError(f"unknown comparison arm {arm!r}")
            key = (
                _string(item.get("case_id"), "comparison case id"),
                _integer(item.get("repetition"), "comparison repetition"),
            )
            if arm in pairs[key]:
                raise IntegrityError(f"duplicate {arm} result for comparison pair {key!r}")
            pairs[key][arm] = item
        incomplete = sorted(
            key for key, arms in pairs.items() if set(arms) != {"baseline", "optimized"}
        )
        if incomplete:
            raise IntegrityError(f"COPRO test comparison has incomplete pairs: {incomplete[:5]}")
        pair_rows = []
        for (case_id, repetition), arms in sorted(pairs.items()):
            baseline = arms["baseline"]
            optimized = arms["optimized"]
            for field in (
                "skill_name",
                "leakage_group",
                "expected_response_tokens",
                "expected_response_length_band",
            ):
                if baseline.get(field) != optimized.get(field):
                    raise IntegrityError(f"COPRO comparison pair {case_id!r} disagrees on {field}")
            baseline_score = float(baseline["score"])
            optimized_score = float(optimized["score"])
            pair_rows.append(
                {
                    "case_id": case_id,
                    "case_title": baseline.get("case_title"),
                    "skill_name": baseline["skill_name"],
                    "leakage_group": baseline["leakage_group"],
                    "repetition": repetition,
                    "expected_response_tokens": baseline.get("expected_response_tokens"),
                    "expected_response_length_band": baseline.get("expected_response_length_band"),
                    "baseline_candidate_id": baseline["candidate_id"],
                    "optimized_candidate_id": optimized["candidate_id"],
                    "baseline_score": baseline_score,
                    "optimized_score": optimized_score,
                    "delta": optimized_score - baseline_score,
                }
            )
        by_skill = _paired_summary(pair_rows, "skill_name")
        by_length_band = _paired_summary(
            [row for row in pair_rows if row.get("expected_response_length_band") is not None],
            "expected_response_length_band",
            band_order=True,
        )
        criterion_rows = []
        for arms in pairs.values():
            baseline_scores = {
                str(item["id"]): float(item["score"])
                for item in _mapping_sequence(
                    arms["baseline"]["criterion_scores"], "baseline criterion scores"
                )
            }
            optimized_scores = {
                str(item["id"]): float(item["score"])
                for item in _mapping_sequence(
                    arms["optimized"]["criterion_scores"], "optimized criterion scores"
                )
            }
            if set(baseline_scores) != set(optimized_scores):
                raise IntegrityError("COPRO comparison criterion sets disagree")
            for criterion_id in sorted(baseline_scores):
                criterion_rows.append(
                    {
                        "criterion_id": criterion_id,
                        "baseline_score": baseline_scores[criterion_id],
                        "optimized_score": optimized_scores[criterion_id],
                        "delta": optimized_scores[criterion_id] - baseline_scores[criterion_id],
                    }
                )
        by_criterion = _paired_summary(criterion_rows, "criterion_id")
        deltas = [float(row["delta"]) for row in pair_rows]
        baseline_mean = statistics.fmean(float(row["baseline_score"]) for row in pair_rows)
        optimized_mean = statistics.fmean(float(row["optimized_score"]) for row in pair_rows)
        ci_low, ci_high = _cluster_bootstrap_interval(pair_rows)
        return {
            "schema_version": 1,
            "pairing_unit": "case_id+solver_repetition",
            "paired_case_count": len(pair_rows),
            "leakage_group_count": len({str(row["leakage_group"]) for row in pair_rows}),
            "baseline_mean_score": baseline_mean,
            "optimized_mean_score": optimized_mean,
            "micro_case_delta": statistics.fmean(deltas),
            "macro_skill_delta": statistics.fmean(float(row["delta"]) for row in by_skill),
            "delta_ci95": {"lower": ci_low, "upper": ci_high},
            "positive_skill_count": sum(float(row["delta"]) > 1e-12 for row in by_skill),
            "negative_skill_count": sum(float(row["delta"]) < -1e-12 for row in by_skill),
            "unchanged_skill_count": sum(abs(float(row["delta"])) <= 1e-12 for row in by_skill),
            "cases": pair_rows,
            "skills": by_skill,
            "length_bands": by_length_band,
            "criteria": by_criterion,
        }

    def _cross_validation(self, statuses: Sequence[StageStatus]) -> Mapping[str, object] | None:
        values = [
            status
            for status in statuses
            if status.packet.stage is StageKind.SELECT
            and status.state is StageState.PERSISTED
            and status.result is not None
            and (status.packet.output_schema == "cross-validation-selection-v1")
        ]
        if len(values) > 1:
            raise IntegrityError("run contains multiple persisted selection results")
        if not values:
            return None
        assert values[0].result is not None
        return _mapping(self.ledger.read_output(values[0].result), "cross-validation selection")

    def _optimization(
        self,
        statuses: Sequence[StageStatus],
        lock: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        configuration = _mapping(lock.get("configuration", {}), "lock configuration")
        if configuration.get("mode") != "optimize":
            return None
        optimizer = _mapping(configuration.get("optimizer"), "optimizer configuration")
        strategy = _string(optimizer.get("strategy"), "optimizer strategy")
        final_fits = [
            status
            for status in statuses
            if status.packet.stage is StageKind.FIT
            and status.state is StageState.PERSISTED
            and status.result is not None
            and status.packet.work_spec.get("phase") == "final_fit"
        ]
        final_program = None
        if final_fits:
            selected = final_fits[-1]
            assert selected.result is not None
            output = _mapping(self.ledger.read_output(selected.result), "final fitted program")
            final_program = {
                "candidate_id": output.get("candidate_id"),
                "program_hash": output.get("fitted_program_hash"),
                "state_artifact": output.get("state_artifact"),
                "compiled_manifest_artifact": output.get("compiled_manifest_artifact"),
                "candidate_lineage_artifact": output.get("candidate_lineage_artifact"),
                "search_summary_artifact": output.get("search_summary_artifact"),
                "demonstration_count": output.get("demonstration_count"),
                "solver_instruction": output.get("solver_instruction"),
                "solver_instruction_artifact": output.get("solver_instruction_artifact"),
                "output_prefix": output.get("output_prefix"),
                "output_prefix_artifact": output.get("output_prefix_artifact"),
                "skill_overlays": output.get("skill_overlay_artifacts"),
            }
        rounds = []
        if strategy == "copro":
            for status in statuses:
                if (
                    status.packet.output_schema != "copro-selection-v1"
                    or status.state is not StageState.PERSISTED
                    or status.result is None
                ):
                    continue
                output = _mapping(self.ledger.read_output(status.result), "COPRO selection")
                rounds.append(
                    {
                        key: output.get(key)
                        for key in (
                            "scope",
                            "round",
                            "target_kind",
                            "target_id",
                            "selected_candidate_id",
                            "selected_mean_score",
                            "candidates",
                            "proposal_accounting",
                        )
                    }
                )
            rounds.sort(
                key=lambda item: (
                    str(item.get("scope")),
                    int(item.get("round") or 0),
                    0 if item.get("target_kind") == "solver_prompt" else 1,
                    str(item.get("target_id")),
                )
            )
        return {
            "strategy": strategy,
            "configuration": optimizer,
            "final_program": final_program,
            "rounds": rounds,
        }

    @staticmethod
    def _validate_aggregate(
        run: Mapping[str, object],
        lock: Mapping[str, object],
        aggregate: Mapping[str, object] | None,
        cases: Sequence[Mapping[str, object]],
    ) -> None:
        if aggregate is None:
            return
        if aggregate.get("schema_version") != 4:
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
            scored_cases = RunReportBuilder._primary_scored_cases(aggregate_report_cases)
            mean_score = (
                sum(float(item["score"]) for item in scored_cases) / len(scored_cases)
                if scored_cases
                else 0.0
            )
            if (
                abs(_number(aggregate.get("mean_score"), "aggregate mean_score") - mean_score)
                > 1e-9
            ):
                raise IntegrityError("aggregate mean score is inconsistent")
        elif aggregate.get("benchmark_metrics") is not None:
            raise IntegrityError("pilot aggregate must not contain benchmark metrics")

    def _report_status(
        self, statuses: Sequence[StageStatus], aggregate: Mapping[str, object] | None
    ) -> str:
        if aggregate is not None:
            return "completed"
        judge_validation = next(
            (
                status
                for status in statuses
                if status.packet.stage is StageKind.VALIDATE_JUDGE
                and status.state is StageState.PERSISTED
                and status.result is not None
            ),
            None,
        )
        if judge_validation is not None and judge_validation.result is not None:
            output = _mapping(
                self.ledger.read_output(judge_validation.result), "judge validation output"
            )
            if output.get("ordering_consistent") is False:
                return "judge_validation_failed"
        if any(status.state in {StageState.FAILED, StageState.REJECTED} for status in statuses):
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
                status.result is not None and status.result.provenance.get("cache_hit") is True
            ),
            "source_result_id": (
                None if status.result is None else status.result.provenance.get("source_result_id")
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
            "optimizer": Counter(),
            "solver": Counter(),
            "judge": Counter(),
        }
        for status in statuses:
            if status.result is None:
                continue
            role = {
                StageKind.AUTHOR: "author",
                StageKind.OPTIMIZE: "optimizer",
                StageKind.SOLVE: "solver",
                StageKind.VALIDATE_JUDGE: "judge",
                StageKind.JUDGE: "judge",
            }.get(status.packet.stage)
            if role is None:
                continue
            for key, value in status.result.usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values[role][str(key)] += value
        return {role: dict(sorted(counter.items())) for role, counter in values.items() if counter}

    def _endpoint_summary(self, statuses: Sequence[StageStatus]) -> Mapping[str, object]:
        endpoints: dict[str, dict[str, object]] = {}
        for executor in self.repository.workspace.executors.values():
            if executor.type is not ExecutorType.ENDPOINT:
                continue
            for configured in executor.endpoints:
                key = f"{executor.id}/{configured.id}"
                endpoints[key] = {
                    "executor_id": executor.id,
                    "endpoint_id": configured.id,
                    "base_url": configured.base_url,
                    "pool_hash": executor.pool_hash,
                    "endpoint_configuration_hash": configured.configuration_hash,
                    "configured_capacity": configured.max_concurrent_requests,
                    "packets_succeeded": 0,
                    "packets_failed": 0,
                    "preflight_rejected_packets": 0,
                    "requests": 0,
                    "preflight_checks": 0,
                    "preflight_rejections": 0,
                    "preflight_skips": 0,
                    "model_calls": 0,
                    "completed_calls": 0,
                    "failed_calls": 0,
                    "latency_seconds": 0.0,
                    "queue_wait_seconds": 0.0,
                    "peak_observed_endpoint_concurrency": 0,
                    "peak_observed_pool_concurrency": 0,
                }
        for status in statuses:
            if status.result is None:
                continue
            raw = status.result.provenance.get("endpoint_pool")
            if not isinstance(raw, Mapping):
                continue
            for route in endpoint_provenance_routes(raw, status.result.usage):
                endpoint_id = route.get("endpoint_id")
                if not isinstance(endpoint_id, str):
                    continue
                key = f"{status.packet.executor.id}/{endpoint_id}"
                endpoint = endpoints.get(key)
                if endpoint is None:
                    continue
                requests = route.get("requests", 0)
                if isinstance(requests, int) and not isinstance(requests, bool):
                    endpoint["requests"] = int(endpoint["requests"]) + requests
                    if requests > 0:
                        outcome = (
                            "packets_failed"
                            if status.result.error is not None
                            else "packets_succeeded"
                        )
                        endpoint[outcome] = int(endpoint[outcome]) + 1
                for preflight_key in (
                    "preflight_checks",
                    "preflight_rejections",
                    "preflight_skips",
                ):
                    value = route.get(preflight_key, 0)
                    if isinstance(value, int) and not isinstance(value, bool):
                        endpoint[preflight_key] = int(endpoint[preflight_key]) + value
                route_rejections = route.get("preflight_rejections", 0)
                if (
                    isinstance(route_rejections, int)
                    and route_rejections > 0
                    and isinstance(requests, int)
                    and requests == 0
                ):
                    endpoint["preflight_rejected_packets"] = (
                        int(endpoint["preflight_rejected_packets"]) + 1
                    )
                for usage_key in ("model_calls", "completed_calls", "failed_calls"):
                    value = route.get(usage_key, 0)
                    if isinstance(value, int) and not isinstance(value, bool):
                        endpoint[usage_key] = int(endpoint[usage_key]) + value
                latency = route.get("latency_seconds", 0.0)
                if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                    endpoint["latency_seconds"] = float(endpoint["latency_seconds"]) + float(
                        latency
                    )
                queue_wait = route.get("queue_wait_seconds", 0.0)
                if isinstance(queue_wait, (int, float)) and not isinstance(queue_wait, bool):
                    endpoint["queue_wait_seconds"] = float(endpoint["queue_wait_seconds"]) + float(
                        queue_wait
                    )
                for summary_key in (
                    "peak_observed_endpoint_concurrency",
                    "peak_observed_pool_concurrency",
                ):
                    value = route.get(summary_key, 0)
                    if isinstance(value, int) and not isinstance(value, bool):
                        endpoint[summary_key] = max(int(endpoint[summary_key]), value)
        return {
            "configured_capacity": sum(
                int(endpoint["configured_capacity"]) for endpoint in endpoints.values()
            ),
            "endpoints": [endpoints[key] for key in sorted(endpoints)],
        }

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
                "critical_violations": sum(bool(item["hard_failures"]) for item in items),
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
            for criterion in _mapping_sequence(case["criterion_scores"], "case criterion scores"):
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
    preserved: dict[str, bytes] = {}
    if destination.exists():
        existing = _directory_bytes(destination)
        preserved = {
            relative: payload
            for relative, payload in existing.items()
            if _is_local_visualization_asset(relative)
        }
        existing_report = {
            relative: payload
            for relative, payload in existing.items()
            if not _is_local_visualization_asset(relative)
        }
        if existing_report == files:
            return _published(report, destination, files, changed=False)
        if existing_report and not replace:
            raise IntegrityError(
                f"report output already exists with different bytes: {destination}; "
                "use --replace to rebuild it"
            )
    temporary = Path(tempfile.mkdtemp(prefix=f".{report.run_id}.", dir=runs_root))
    try:
        for relative, payload in {**preserved, **files}.items():
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


def _is_local_visualization_asset(relative: str) -> bool:
    return (
        relative == "index.html"
        or relative == "tables/index.html"
        or relative.startswith("graphs/")
    )


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
            "leakage_group": case["leakage_group"],
            "repetition": case["repetition"],
            "comparison_arm": case["comparison_arm"],
            "candidate_id": case["candidate_id"],
            "strategy": case["strategy"],
            "expected_response_tokens": case["expected_response_tokens"],
            "expected_response_length_band": case["expected_response_length_band"],
            "author_model_details": models.get("author"),
            "solver_model_details": models.get("solver"),
            "judge_model_details": models.get("judge"),
            "score": case["score"],
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
                "but no aggregate benchmark metric is reported.",
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
                "",
            ]
        )
    comparison = value.get("test_comparison")
    if isinstance(comparison, Mapping):
        interval = _mapping(comparison.get("delta_ci95", {}), "comparison interval")
        lines.extend(
            [
                "## COPRO paired test comparison",
                "",
                "The original and optimized programs were judged on the same untouched test cases.",
                "",
                f"- Paired test results: `{comparison.get('paired_case_count')}`",
                f"- Baseline mean: `{_format_score(comparison.get('baseline_mean_score'))}`",
                f"- Optimized mean: `{_format_score(comparison.get('optimized_mean_score'))}`",
                f"- Mean paired delta: `{_format_score(comparison.get('micro_case_delta'))}`",
                f"- Macro skill delta: `{_format_score(comparison.get('macro_skill_delta'))}`",
                f"- Cluster-bootstrap 95% interval: `[{_format_score(interval.get('lower'))}, {_format_score(interval.get('upper'))}]`",
                f"- Skills improved / regressed / unchanged: `{comparison.get('positive_skill_count')} / {comparison.get('negative_skill_count')} / {comparison.get('unchanged_skill_count')}`",
                "",
                "| Skill | Pairs | Baseline | Optimized | Delta |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for skill in _mapping_sequence(comparison.get("skills", []), "comparison skills"):
            lines.append(
                f"| {_table(skill['skill_name'])} | {skill['pair_count']} | "
                f"{_format_score(skill['baseline_mean_score'])} | "
                f"{_format_score(skill['optimized_mean_score'])} | "
                f"{_format_signed_score(skill['delta'])} |"
            )
        lines.extend(
            [
                "",
                "### Expected-response token bands",
                "",
                "Token bands use the exact configured solver tokenizer over the hidden expected response, without a chat template or special tokens.",
                "",
                "| Expected-response tokens | Pairs | Baseline | Optimized | Delta |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        token_rows = _mapping_sequence(comparison.get("length_bands", []), "token bands")
        if token_rows:
            for band in token_rows:
                lines.append(
                    f"| `{band['expected_response_length_band']}` | {band['pair_count']} | "
                    f"{_format_score(band['baseline_mean_score'])} | "
                    f"{_format_score(band['optimized_mean_score'])} | "
                    f"{_format_signed_score(band['delta'])} |"
                )
        else:
            lines.append("| unavailable | 0 | — | — | — |")
        lines.append("")
    cross_validation = value.get("cross_validation")
    if isinstance(cross_validation, Mapping):
        lines.extend(
            [
                "## Cross-validation selection",
                "",
                f"- Metric: `{cross_validation.get('metric')}`",
                f"- Selected candidate: `{cross_validation.get('selected_candidate_id')}`",
                f"- Selected k: `{cross_validation.get('selected_k')}`",
                f"- Mean out-of-fold score: `{_format_score(cross_validation.get('selected_mean_score'))}`",
                "",
                "| Candidate | k | Cases | Mean out-of-fold score |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for candidate in _mapping_sequence(
            cross_validation.get("candidates", []), "cross-validation candidates"
        ):
            lines.append(
                f"| `{_table(candidate['candidate_id'])}` | {candidate['k']} | "
                f"{candidate['case_count']} | {_format_score(candidate['mean_score'])} |"
            )
        lines.append("")
    optimization = value.get("optimization")
    if isinstance(optimization, Mapping):
        final_program = optimization.get("final_program")
        final_mapping = (
            _mapping(final_program, "final optimized program")
            if isinstance(final_program, Mapping)
            else {}
        )
        rounds = optimization.get("rounds", [])
        degraded_rounds = (
            sum(
                1
                for raw_round in rounds
                if isinstance(raw_round, Mapping)
                and isinstance(raw_round.get("proposal_accounting"), Mapping)
                and raw_round["proposal_accounting"].get("degraded") is True
            )
            if isinstance(rounds, Sequence)
            and not isinstance(rounds, (str, bytes, bytearray))
            else 0
        )
        lines.extend(
            [
                "## Optimization",
                "",
                f"- Strategy: `{optimization.get('strategy')}`",
                f"- Final candidate: `{final_mapping.get('candidate_id')}`",
                f"- Final program hash: `{final_mapping.get('program_hash')}`",
                f"- Demonstrations: `{final_mapping.get('demonstration_count')}`",
                f"- Search rounds: `{len(rounds) if isinstance(rounds, Sequence) else 0}`",
                f"- Degraded proposal rounds: `{degraded_rounds}`",
                f"- Compiled manifest: `{_artifact_id(final_mapping.get('compiled_manifest_artifact'))}`",
                f"- Candidate lineage: `{_artifact_id(final_mapping.get('candidate_lineage_artifact'))}`",
                f"- Search summary: `{_artifact_id(final_mapping.get('search_summary_artifact'))}`",
                "",
            ]
        )
    lines.extend(["## Models", ""])
    for role, raw_model in _mapping(value["models"], "report models").items():
        model = _mapping(raw_model, f"{role} model")
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
    endpoint_execution = _mapping(value.get("endpoint_execution", {}), "endpoint execution")
    endpoint_rows = _mapping_sequence(
        endpoint_execution.get("endpoints", []), "endpoint execution endpoints"
    )
    if endpoint_rows:
        lines.extend(
            [
                "",
                "## Endpoint execution",
                "",
                f"- Configured pool capacity: `{endpoint_execution.get('configured_capacity', 0)}`",
                "",
                "| Executor / endpoint | URL | Capacity | Calls | Preflight checks | Rejected | Skipped | Succeeded packets | Failed packets | Peak endpoint | Peak pool | Queue wait (s) | Model latency (s) |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for endpoint in endpoint_rows:
            lines.append(
                f"| `{endpoint['executor_id']}/{endpoint['endpoint_id']}` | "
                f"`{endpoint['base_url']}` | {endpoint['configured_capacity']} | "
                f"{endpoint['model_calls']} | {endpoint['preflight_checks']} | "
                f"{endpoint['preflight_rejections']} | {endpoint['preflight_skips']} | "
                f"{endpoint['packets_succeeded']} | "
                f"{endpoint['packets_failed']} | "
                f"{endpoint['peak_observed_endpoint_concurrency']} | "
                f"{endpoint['peak_observed_pool_concurrency']} | "
                f"{float(endpoint['queue_wait_seconds']):.3f} | "
                f"{float(endpoint['latency_seconds']):.3f} |"
            )
    lines.extend(["", "## Incremental usage", ""])
    for role, usage in _mapping(value["usage_by_role"], "report usage").items():
        lines.append(f"- {str(role).title()}: `{_compact_json(usage)}`")
    lines.extend(
        [
            "",
            "## Skills",
            "",
            "| Skill | Cases | Critical violations | Mean score |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for skill in _mapping_sequence(value["skills"], "report skills"):
        lines.append(
            f"| {_table(skill['skill_name'])} | {skill['case_count']} | "
            f"{skill['critical_violations']} | {_format_score(skill['mean_score'])} |"
        )
    lines.extend(
        [
            "",
            "## Cases",
            "",
            "| Skill | Case | Arm | Tokens | Split | Repetition | Score | Critical violations |",
            "| --- | --- | --- | ---: | --- | ---: | ---: | ---: |",
        ]
    )
    filenames = _case_filenames(_case_sequence(report))
    for case in _case_sequence(report):
        key = (
            str(case["case_id"]),
            int(case["repetition"]),
            str(case["comparison_arm"]),
        )
        lines.append(
            f"| {_table(case['skill_name'])} | "
            f"[{_table(case['case_title'])}](cases/{filenames[key]}) | "
            f"{case['comparison_arm']} | {case.get('expected_response_tokens') or '—'} | "
            f"{_table(case['split'])} | {case['repetition']} | "
            f"{_format_score(case['score'])} | {len(case['hard_failures'])} |"
        )
    violations = [case for case in _case_sequence(report) if case["hard_failures"]]
    lines.extend(["", "## Critical violations", ""])
    if not violations:
        lines.append("None.")
    else:
        for case in violations:
            lines.append(
                f"- `{case['case_id']}` repetition {case['repetition']}: "
                f"score {_format_score(case['score'])}; hard failures "
                f"`{_compact_json(case['hard_failures'])}`"
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
        f"- Comparison arm: `{case['comparison_arm']}`",
        f"- Candidate: `{case['candidate_id']}`",
        f"- Strategy: `{case['strategy']}`",
        f"- Expected-response tokens: `{case.get('expected_response_tokens')}`",
        f"- Expected-response token band: `{case.get('expected_response_length_band')}`",
        f"- Score: `{_format_score(case['score'])}`",
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
    comparison = report.value.get("test_comparison")
    if isinstance(comparison, Mapping):
        for field, filename in (
            ("cases", "copro-case-comparison.csv"),
            ("skills", "copro-skill-comparison.csv"),
            ("length_bands", "copro-token-band-comparison.csv"),
            ("criteria", "copro-criterion-comparison.csv"),
        ):
            rows = _mapping_sequence(comparison.get(field, []), f"comparison {field}")
            if rows:
                files[f"tables/{filename}"] = _render_rows_csv(rows)
    token_measurements = report.value.get("expected_response_token_measurements")
    if isinstance(token_measurements, Mapping):
        files["tables/expected-response-token-counts.json"] = (
            json.dumps(
                json_value(token_measurements),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    for case in cases:
        key = (
            str(case["case_id"]),
            int(case["repetition"]),
            str(case["comparison_arm"]),
        )
        files[f"cases/{filenames[key]}"] = render_case_markdown(report, case)
    return dict(sorted(files.items()))


def _render_rows_csv(rows: Sequence[Mapping[str, object]]) -> bytes:
    columns = tuple(sorted({str(key) for row in rows for key in row}))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                column: (
                    _compact_json(row.get(column))
                    if isinstance(row.get(column), (Mapping, Sequence))
                    and not isinstance(row.get(column), str)
                    else _safe_cell(row.get(column))
                )
                for column in columns
            }
        )
    return output.getvalue().encode("utf-8")


def _case_filenames(
    cases: Sequence[Mapping[str, object]],
) -> Mapping[tuple[str, int, str], str]:
    counts = Counter(str(case["case_id"]) for case in cases)
    return {
        (
            str(case["case_id"]),
            int(case["repetition"]),
            str(case["comparison_arm"]),
        ): (
            f"{case['case_id']}.md"
            if counts[str(case["case_id"])] == 1
            else (f"{case['case_id']}-{case['comparison_arm']}-repetition-{case['repetition']}.md")
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
        destination / relative for relative in sorted(files) if relative.startswith("cases/")
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
    return json.dumps(json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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


def _format_signed_score(value: object) -> str:
    if value is None:
        return "null"
    return f"{float(value):+.6f}".rstrip("0").rstrip(".")


def _artifact_id(value: object) -> object:
    if isinstance(value, Mapping):
        return value.get("content_id")
    return None


def _paired_summary(
    rows: Sequence[Mapping[str, object]],
    key: str,
    *,
    band_order: bool = False,
) -> list[Mapping[str, object]]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[_string(row.get(key), f"paired summary {key}")].append(row)
    result = [
        {
            key: name,
            "pair_count": len(items),
            "baseline_mean_score": statistics.fmean(
                float(item["baseline_score"]) for item in items
            ),
            "optimized_mean_score": statistics.fmean(
                float(item["optimized_score"]) for item in items
            ),
            "delta": statistics.fmean(float(item["delta"]) for item in items),
        }
        for name, items in grouped.items()
    ]
    if band_order:
        result.sort(key=lambda item: _token_band_lower(str(item[key])))
    else:
        result.sort(key=lambda item: (-float(item["delta"]), str(item[key])))
    return result


def _token_band_lower(value: str) -> int:
    prefix = value.split("-", 1)[0].removesuffix("+")
    try:
        return int(prefix)
    except ValueError as error:
        raise IntegrityError(f"invalid expected-response token band {value!r}") from error


def _cluster_bootstrap_interval(
    rows: Sequence[Mapping[str, object]],
    *,
    samples: int = 2000,
) -> tuple[float | None, float | None]:
    by_group: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_group[_string(row.get("leakage_group"), "comparison leakage group")].append(
            float(row["delta"])
        )
    groups = sorted(by_group)
    if len(groups) < 2:
        return None, None
    seed = int(content_hash({"groups": by_group, "samples": samples})[-16:], 16)
    generator = random.Random(seed)
    estimates = []
    for _ in range(samples):
        sampled = [generator.choice(groups) for _ in groups]
        deltas = [delta for group in sampled for delta in by_group[group]]
        estimates.append(statistics.fmean(deltas))
    estimates.sort()
    lower_index = max(0, int(samples * 0.025))
    upper_index = min(samples - 1, int(samples * 0.975) - 1)
    return estimates[lower_index], estimates[upper_index]


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
