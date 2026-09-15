from __future__ import annotations

import html
import json
import math
import os
import re
import statistics
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import plotly.graph_objects as go
import plotly.io as pio
from plotly.offline import get_plotlyjs_version

from .errors import IntegrityError, RecordingError
from .hashing import canonical_json_bytes, content_hash
from .lifecycle import RunLifecycleEvent, RunLifecycleLedger, RunLifecycleState
from .models import ArtifactReference
from .recorder import (
    ArtifactDescriptor,
    ExperimentDescriptor,
    MetricPoint,
    RecordingPlan,
    RunDescriptor,
    ScopeDescriptor,
    SpanDescriptor,
    TableArtifact,
    TraceDescriptor,
)
from .stages import StageArtifact, StageKind, StageLedger, StageState, StageStatus
from .storage import FilesystemArtifactStore
from .workspace import (
    WORKSPACE_SCHEMA_VERSION,
    RecorderConfiguration,
    RecordingArtifactPolicy,
    RecordingConfiguration,
    RecordingContentPolicy,
)


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{16,}"
    ),
)


class RecorderProjector:
    """Build a deterministic, recorder-neutral plan from canonical evidence."""

    def __init__(
        self,
        store: FilesystemArtifactStore,
        *,
        recorder: RecorderConfiguration,
        recording: RecordingConfiguration,
    ) -> None:
        self.store = store
        self.ledger = StageLedger(store)
        self.lifecycle = RunLifecycleLedger(store)
        self.recorder = recorder
        self.recording = recording

    def build(self, run_id: str) -> RecordingPlan:
        run = self.ledger.load_run(run_id)
        statuses = self.ledger.list(run_id)
        lifecycle = self.lifecycle.list(run_id)
        report = self._load_report(run_id)
        model_calls = self._model_calls(run_id)
        transition_rows = self._transition_rows(run_id)
        status = _effective_lifecycle_state(lifecycle, statuses)
        created_at = _string(run.get("created_at"), "run created_at")
        start_time_ms = _timestamp_ms(created_at)
        end_time_ms = _terminal_time_ms(lifecycle, status)
        experiment_id = _string(run.get("experiment_id"), "run experiment_id")
        workspace_id = _string(run.get("workspace_id"), "run workspace_id")
        revision = _string(run.get("experiment_revision_id"), "run experiment_revision_id")
        experiment = ExperimentDescriptor(
            name=self.recorder.resolved_experiment_name(workspace_id, experiment_id),
            tags={
                "agent_eval.workspace_id": workspace_id,
                "agent_eval.experiment_id": experiment_id,
            },
        )
        run_descriptor = RunDescriptor(
            local_run_id=run_id,
            name=(
                f"{experiment_id} · {run.get('execution_mode', 'run')} · "
                f"{revision.removeprefix('sha256:')[:12]}"
            ),
            status=status.value,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            tags=self._run_tags(run, report, status),
            parameters=self._run_parameters(run, report),
        )
        scopes, scope_by_packet = self._scopes(statuses)
        metrics = self._metrics(
            run,
            statuses,
            lifecycle,
            transition_rows,
            model_calls,
            report,
            start_time_ms,
        )
        available_tables = self._tables(statuses, model_calls, report, scope_by_packet)
        tables = (
            available_tables
            if status
            in {
                RunLifecycleState.COMPLETED,
                RunLifecycleState.FAILED,
                RunLifecycleState.CANCELLED,
            }
            else ()
        )
        traces = self._traces(statuses, model_calls, scope_by_packet)
        artifacts: list[ArtifactDescriptor] = []
        omissions: list[Mapping[str, object]] = []

        dag = self._dag(statuses, scope_by_packet)
        graph_artifacts = {
            "graphs/execution-dag.json": (
                canonical_json_bytes(dag),
                "application/json",
            ),
            "graphs/execution-dag.svg": (
                _dag_svg(dag),
                "image/svg+xml",
            ),
        }
        charts, chart_omissions = self._charts(report, statuses, model_calls)
        graph_artifacts.update(charts)
        omissions.extend(chart_omissions)
        chart_manifest = _chart_manifest(graph_artifacts, omissions)
        graph_artifacts["graphs/manifest.json"] = (
            canonical_json_bytes(chart_manifest),
            "application/json",
        )
        graph_artifacts["graphs/index.html"] = (
            _chart_index_html(
                chart_manifest,
                dag,
                tuple(self._stage_row(item) for item in statuses),
                tuple(self._model_call_row(item) for item in model_calls),
                self._endpoint_rows(statuses),
            ),
            "text/html",
        )

        if self.recording.artifact_policy is RecordingArtifactPolicy.FULL:
            for path, (payload, media_type) in sorted(graph_artifacts.items()):
                artifacts.append(self._artifact(path, payload, media_type))
            table_index = _table_index_html(available_tables)
            recording_index = _recording_index_html(
                run_descriptor, chart_manifest, available_tables
            )
            artifacts.append(
                self._artifact(
                    "tables/index.html",
                    table_index,
                    "text/html",
                )
            )
            artifacts.append(
                self._artifact(
                    "index.html",
                    recording_index,
                    "text/html",
                )
            )
            artifacts.extend(self._identity_artifacts(run))
            artifacts.extend(self._report_artifacts(run_id))
            if self.recording.content_policy is RecordingContentPolicy.FULL:
                artifacts.extend(self._full_evidence_artifacts(statuses, model_calls))
        else:
            table_index = _table_index_html(available_tables)
            recording_index = _recording_index_html(
                run_descriptor, chart_manifest, available_tables
            )

        self._materialize_local_run_assets(
            run_id,
            {
                **graph_artifacts,
                "tables/index.html": (table_index, "text/html"),
                "index.html": (recording_index, "text/html"),
            },
        )

        artifact_index = self._artifact_index(run_id, statuses, model_calls, artifacts)
        artifacts.append(
            self._artifact(
                "evidence/artifact-index.json",
                canonical_json_bytes(artifact_index),
                "application/json",
            )
        )
        recorder_manifest = {
            "schema_version": 1,
            "recorder": self.recorder.id,
            "recorder_type": self.recorder.type,
            "recorder_config_hash": self.recorder.configuration_hash,
            "content_policy": self.recording.content_policy.value,
            "artifact_policy": self.recording.artifact_policy.value,
            "canonical_run_id": run_id,
            "canonical_experiment_revision_id": revision,
        }
        artifacts.append(
            self._artifact(
                "identity/recorder-manifest.json",
                canonical_json_bytes(recorder_manifest),
                "application/json",
            )
        )

        source_sequence = (
            len(lifecycle)
            + len(transition_rows)
            + len(model_calls)
            + (1 if report is not None else 0)
        )
        return RecordingPlan.create(
            recorder_config_hash=self.recorder.configuration_hash,
            source_sequence=source_sequence,
            experiment=experiment,
            run=run_descriptor,
            scopes=scopes,
            metrics=metrics,
            tables=tables,
            artifacts=tuple(sorted(artifacts, key=lambda item: item.path)),
            traces=traces,
            omissions=tuple(sorted(omissions, key=lambda item: str(item.get("artifact")))),
        )

    def build_comparison(self, run_id: str) -> RecordingPlan | None:
        """Build an immutable cross-arm projection for the run's comparison group."""

        current = self._load_report(run_id)
        if current is None or current.get("benchmark_valid") is not True:
            return None
        if current.get("status") != "completed":
            return None
        current_identity = _mapping(current.get("identity", {}), "report identity")
        comparison_group_id = current_identity.get("comparison_group_id")
        benchmark_revision_id = current_identity.get("benchmark_revision_id")
        comparison_id = current_identity.get("comparison_id", "all-compatible-arms")
        workspace_id = current.get("workspace_id")
        if not all(
            isinstance(value, str) and value
            for value in (
                comparison_group_id,
                benchmark_revision_id,
                comparison_id,
                workspace_id,
            )
        ):
            return None

        reports = self._comparison_reports(
            workspace_id=str(workspace_id),
            benchmark_revision_id=str(benchmark_revision_id),
            comparison_group_id=str(comparison_group_id),
            comparison_id=str(comparison_id),
        )
        if not reports:
            return None
        arm_rows = tuple(_comparison_arm_row(report) for report in reports)
        skill_rows = tuple(
            row
            for report in reports
            for row in _comparison_skill_rows(report, _comparison_arm_row(report))
        )
        token_band_rows = tuple(
            row
            for report in reports
            for row in _comparison_token_band_rows(report, _comparison_arm_row(report))
        )
        input_runs = tuple(
            {
                "run_id": report.get("run_id"),
                "experiment_id": report.get("experiment_id"),
                "experiment_revision_id": _nested_mapping_value(
                    report, "identity", "experiment_lock_hash"
                ),
                "arm_id": _nested_mapping_value(report, "identity", "arm_id"),
                "aggregate_result_id": _aggregate_result_id(report),
                "run_graph_id": _nested_mapping_value(report, "identity", "run_graph_id"),
                "report_hash": report.get("content_hash") or content_hash(report),
            }
            for report in reports
        )
        comparison_manifest = {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "benchmark_revision_id": benchmark_revision_id,
            "comparison_group_id": comparison_group_id,
            "comparison_id": comparison_id,
            "selection_policy": "latest_completed_report_per_arm",
            "projection_revision": 1,
            "input_runs": input_runs,
        }
        manifest_hash = content_hash(comparison_manifest)
        local_run_id = f"model-comparison-{manifest_hash.removeprefix('sha256:')[:24]}"
        comparison_artifacts: list[ArtifactDescriptor] = [
            self._artifact(
                "comparison/comparison-manifest.json",
                canonical_json_bytes({**comparison_manifest, "content_hash": manifest_hash}),
                "application/json",
            )
        ]
        chart_artifacts, chart_manifest = _comparison_chart_artifacts(
            arm_rows,
            skill_rows,
            token_band_rows,
        )
        if self.recording.artifact_policy is RecordingArtifactPolicy.FULL:
            for path, (payload, media_type) in sorted(chart_artifacts.items()):
                comparison_artifacts.append(self._artifact(path, payload, media_type))
            comparison_artifacts.append(
                self._artifact(
                    "comparison/graphs/manifest.json",
                    canonical_json_bytes(chart_manifest),
                    "application/json",
                )
            )

        tables = tuple(
            table
            for table in (
                _table("model-arm-comparison", arm_rows),
                _table("model-skill-comparison", skill_rows),
                _table("model-token-band-comparison", token_band_rows),
            )
            if table.rows
        )
        latest = max(
            (_string(report.get("created_at"), "report created_at") for report in reports),
            key=_parse_timestamp,
        )
        timestamp_ms = _timestamp_ms(latest)
        scores = [
            _number_or(row.get("score"), math.nan)
            for row in arm_rows
            if isinstance(row.get("score"), (int, float))
        ]
        scores = [value for value in scores if math.isfinite(value)]
        metrics = [_metric("comparison.arm_count", float(len(arm_rows)), 1, timestamp_ms)]
        if scores:
            metrics.append(_metric("comparison.best_score", max(scores), 1, timestamp_ms))
        judge_profiles = sorted(
            {
                str(row["judge_profile"])
                for row in arm_rows
                if isinstance(row.get("judge_profile"), str)
            }
        )
        experiment = ExperimentDescriptor(
            name=self.recorder.resolved_experiment_name(str(workspace_id), "model-comparison"),
            tags={"agent_eval.workspace_id": workspace_id},
        )
        descriptor = RunDescriptor(
            local_run_id=local_run_id,
            name=f"Model comparison · {str(comparison_group_id).removeprefix('sha256:')[:12]}",
            status=RunLifecycleState.COMPLETED.value,
            start_time_ms=timestamp_ms,
            end_time_ms=timestamp_ms,
            tags={
                "agent_eval.local_run_id": local_run_id,
                "agent_eval.workspace_id": workspace_id,
                "agent_eval.run_kind": "model_comparison",
                "agent_eval.benchmark_revision_id": benchmark_revision_id,
                "agent_eval.comparison_group_id": comparison_group_id,
                "agent_eval.comparison_id": comparison_id,
                "agent_eval.comparison_manifest_hash": manifest_hash,
                "agent_eval.benchmark_valid": True,
            },
            parameters={
                "comparison.arm_count": len(arm_rows),
                "comparison.selection_policy": "latest_completed_report_per_arm",
                "judge.profiles": ",".join(judge_profiles),
            },
        )
        if self.recording.artifact_policy is RecordingArtifactPolicy.FULL:
            comparison_artifacts.extend(
                (
                    self._artifact(
                        "tables/index.html",
                        _table_index_html(tables),
                        "text/html",
                    ),
                    self._artifact(
                        "index.html",
                        _comparison_index_html(descriptor, chart_manifest, tables),
                        "text/html",
                    ),
                )
            )
        return RecordingPlan.create(
            recorder_config_hash=self.recorder.configuration_hash,
            source_sequence=len(reports),
            experiment=experiment,
            run=descriptor,
            metrics=tuple(metrics),
            tables=tables,
            artifacts=tuple(sorted(comparison_artifacts, key=lambda item: item.path)),
        )

    def _comparison_reports(
        self,
        *,
        workspace_id: str,
        benchmark_revision_id: str,
        comparison_group_id: str,
        comparison_id: str,
    ) -> tuple[Mapping[str, object], ...]:
        """Select one latest valid completed report for every compatible arm."""

        selected: dict[str, Mapping[str, object]] = {}
        root = self.store.summary_root / "runs"
        if not root.is_dir() or root.is_symlink():
            return ()
        for path in sorted(root.glob("*/results.json")):
            try:
                report = _load_mapping(path, "run report")
                identity = _mapping(report.get("identity", {}), "report identity")
            except (IntegrityError, OSError, ValueError):
                continue
            arm_id = identity.get("arm_id")
            if (
                report.get("workspace_id") != workspace_id
                or report.get("benchmark_valid") is not True
                or report.get("status") != "completed"
                or identity.get("benchmark_revision_id") != benchmark_revision_id
                or identity.get("comparison_group_id") != comparison_group_id
                or identity.get("comparison_id", "all-compatible-arms") != comparison_id
                or not isinstance(arm_id, str)
                or not arm_id
            ):
                continue
            existing = selected.get(arm_id)
            if existing is None or _report_order_key(report) > _report_order_key(existing):
                selected[arm_id] = report
        return tuple(
            selected[key]
            for key in sorted(
                selected,
                key=lambda arm: (
                    str(selected[arm].get("experiment_id")),
                    arm,
                ),
            )
        )

    def _load_report(self, run_id: str) -> Mapping[str, object] | None:
        path = self.store.summary_root / "runs" / run_id / "results.json"
        if not path.is_file() or path.is_symlink():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise IntegrityError(f"run report is invalid: {path}") from error
        return _mapping(payload, "run report")

    def _run_tags(
        self,
        run: Mapping[str, object],
        report: Mapping[str, object] | None,
        status: RunLifecycleState,
    ) -> Mapping[str, object]:
        identity = _mapping(report.get("identity", {}), "report identity") if report else {}
        return {
            "agent_eval.local_run_id": run.get("run_id"),
            "agent_eval.workspace_id": run.get("workspace_id"),
            "agent_eval.workspace_hash": run.get("workspace_hash"),
            "agent_eval.experiment_id": run.get("experiment_id"),
            "agent_eval.experiment_revision_id": run.get("experiment_revision_id"),
            "agent_eval.benchmark_revision_id": run.get("benchmark_revision_id"),
            "agent_eval.comparison_group_id": run.get("comparison_group_id"),
            "agent_eval.comparison_id": run.get("comparison_id"),
            "agent_eval.arm_id": run.get("arm_id"),
            "agent_eval.run_kind": "arm_execution",
            "agent_eval.execution_mode": run.get("execution_mode"),
            "agent_eval.benchmark_valid": run.get("benchmark_valid"),
            "agent_eval.coverage_status": run.get("coverage_status"),
            "agent_eval.lifecycle": status.value,
            "agent_eval.case_bank_hash": identity.get("case_bank_hash"),
            "agent_eval.split_hash": identity.get("split_hash"),
            "agent_eval.judge_validation_hash": identity.get("judge_validation_hash"),
            "agent_eval.recorder_config_hash": self.recorder.configuration_hash,
            "agent_eval.content_policy": self.recording.content_policy.value,
            "agent_eval.artifact_policy": self.recording.artifact_policy.value,
        }

    @staticmethod
    def _run_parameters(
        run: Mapping[str, object], report: Mapping[str, object] | None
    ) -> Mapping[str, object]:
        parameters: dict[str, object] = {
            "execution_mode": run.get("execution_mode"),
            "judge_repetitions": run.get("judge_repetitions"),
        }
        if report is not None:
            models = _mapping(report.get("models", {}), "report models")
            for role in ("author", "optimizer", "solver", "judge"):
                model = models.get(role)
                if isinstance(model, Mapping):
                    parameters[f"{role}.profile"] = model.get("profile_id")
                    parameters[f"{role}.deployment"] = model.get("deployment_id")
                    parameters[f"{role}.model"] = model.get("configured_name")
                    parameters[f"{role}.deployment_revision"] = model.get("deployment_revision")
                    parameters[f"{role}.reasoning_effort"] = model.get("reasoning_effort")
        return parameters

    def _scopes(
        self, statuses: Sequence[StageStatus]
    ) -> tuple[tuple[ScopeDescriptor, ...], Mapping[str, str]]:
        values: dict[str, ScopeDescriptor] = {}
        packet_scopes: dict[str, str] = {}

        def add(scope_id: str, name: str, parent: str | None, **tags: object) -> None:
            descriptor = ScopeDescriptor(scope_id, name, parent, tags)
            existing = values.get(scope_id)
            if existing is not None and existing != descriptor:
                raise IntegrityError(f"recording scope {scope_id!r} is inconsistent")
            values[scope_id] = descriptor

        for status in statuses:
            packet = status.packet
            stage = packet.stage
            if stage is StageKind.VALIDATE_JUDGE:
                scope = "validate_judge"
                add(scope, "Validate judge", None, stage=stage.value)
            elif stage is StageKind.OPTIMIZE:
                add("optimize", "Optimize", None, stage="optimize")
                context = self._failure_context(status)
                target_id = str(context.get("target_id", "unknown"))
                round_number = context.get("round", "unknown")
                search_scope = str(context.get("scope", "optimization"))
                partition_match = re.fullmatch(r"optimization/partition-(\d+)", search_scope)
                parent = "optimize"
                if partition_match is not None:
                    partition = int(partition_match.group(1))
                    parent = f"optimize/partition-{partition}"
                    add(
                        parent,
                        f"Partition {partition}",
                        "optimize",
                        stage="partition",
                        partition=partition,
                    )
                scope = f"{parent}/propose/{target_id}/round-{round_number}"
                add(
                    scope,
                    f"Propose {target_id} round {round_number}",
                    parent,
                    stage=stage.value,
                    target_id=target_id,
                    round=round_number,
                )
            elif stage is StageKind.AGGREGATE:
                scope = "aggregate"
                add(scope, "Aggregate", None, stage=stage.value)
            elif stage is StageKind.SELECT:
                add("optimize", "Optimize", None, stage="optimize")
                context = self._failure_context(status)
                selection_scope = str(context.get("scope", "validation"))
                round_number = context.get("round")
                if selection_scope.startswith("optimization/partition-"):
                    partition = int(selection_scope.removeprefix("optimization/partition-"))
                    parent = f"optimize/partition-{partition}"
                    add(
                        parent,
                        f"Partition {partition}",
                        "optimize",
                        stage="partition",
                        partition=partition,
                    )
                    target_id = str(context.get("target_id", "unknown"))
                    scope = f"{parent}/select/{target_id}/round-{round_number}"
                    add(
                        scope,
                        f"Select {target_id} round {round_number}",
                        parent,
                        stage=stage.value,
                    )
                else:
                    scope = "optimize/select-validation"
                    add(scope, "Select validation winner", "optimize", stage=stage.value)
            elif stage is StageKind.FIT:
                phase, candidate, partition = self._stage_coordinates(status)
                add("optimize", "Optimize", None, stage="optimize")
                if phase == "final_fit":
                    scope = "optimize/final_fit"
                    add(scope, "Final fit", "optimize", stage=stage.value)
                else:
                    candidate_scope = f"optimize/{candidate}"
                    add(
                        candidate_scope,
                        str(candidate),
                        "optimize",
                        stage="candidate",
                        candidate_id=candidate,
                    )
                    if partition is None:
                        scope = f"{candidate_scope}/fit"
                        add(scope, "Fit", candidate_scope, stage=stage.value)
                    else:
                        partition_scope = f"{candidate_scope}/partition-{partition}"
                        add(
                            partition_scope,
                            f"Partition {partition}",
                            candidate_scope,
                            stage="partition",
                            partition=partition,
                        )
                        scope = f"{partition_scope}/fit"
                        add(scope, "Fit", partition_scope, stage=stage.value)
            elif stage in {StageKind.SOLVE, StageKind.JUDGE}:
                phase, candidate, partition = self._stage_coordinates(status)
                if phase == "validation":
                    add("optimize", "Optimize", None, stage="optimize")
                    candidate_scope = f"optimize/{candidate}"
                    add(
                        candidate_scope,
                        str(candidate),
                        "optimize",
                        stage="candidate",
                        candidate_id=candidate,
                    )
                    if partition is None:
                        scope = f"{candidate_scope}/{stage.value}"
                        add(scope, stage.value.title(), candidate_scope, stage=stage.value)
                    else:
                        partition_scope = f"{candidate_scope}/partition-{partition}"
                        add(
                            partition_scope,
                            f"Partition {partition}",
                            candidate_scope,
                            stage="partition",
                            partition=partition,
                        )
                        scope = f"{partition_scope}/{stage.value}"
                        add(scope, stage.value.title(), partition_scope, stage=stage.value)
                else:
                    scope = f"{stage.value}.{phase or 'test'}"
                    add(
                        scope,
                        f"{stage.value.title()} {phase or 'test'}",
                        None,
                        stage=stage.value,
                        phase=phase or "test",
                    )
            else:
                scope = stage.value
                add(scope, stage.value.replace("_", " ").title(), None, stage=stage.value)
            packet_scopes[packet.packet_id] = scope
        ordered = _topological_scopes(tuple(values.values()))
        return ordered, packet_scopes

    def _stage_coordinates(self, status: StageStatus) -> tuple[str | None, str, int | None]:
        packet = status.packet
        phase: str | None = None
        candidate = "baseline"
        partition: int | None = None
        if status.result is not None:
            try:
                output = _mapping(self.ledger.read_output(status.result), "stage output")
            except (IntegrityError, OSError, ValueError):
                output = {}
            if isinstance(output.get("phase"), str):
                phase = str(output["phase"])
            if isinstance(output.get("evaluation_phase"), str):
                phase = str(output["evaluation_phase"])
            if isinstance(output.get("candidate_id"), str):
                candidate = str(output["candidate_id"])
            if isinstance(output.get("partition"), int) and not isinstance(
                output.get("partition"), bool
            ):
                partition = int(output["partition"])
        configuration = packet.work_spec.get("configuration")
        if isinstance(configuration, Mapping) and isinstance(configuration.get("id"), str):
            candidate = str(configuration["id"])
        return phase, candidate, partition

    def _metrics(
        self,
        run: Mapping[str, object],
        statuses: Sequence[StageStatus],
        lifecycle: Sequence[RunLifecycleEvent],
        transitions: Sequence[Mapping[str, object]],
        model_calls: Sequence[Mapping[str, object]],
        report: Mapping[str, object] | None,
        start_time_ms: int,
    ) -> tuple[MetricPoint, ...]:
        points: list[MetricPoint] = []
        completed = failed = reused = 0
        status_by_packet = {item.packet.packet_id: item for item in statuses}
        for event in transitions:
            state = event.get("state")
            if state == StageState.PERSISTED.value:
                completed += 1
                packet_status = status_by_packet.get(str(event.get("packet_id")))
                if (
                    packet_status is not None
                    and packet_status.result is not None
                    and packet_status.result.provenance.get("cache_hit") is True
                ):
                    reused += 1
            elif state in {StageState.FAILED.value, StageState.REJECTED.value}:
                failed += 1
            sequence = _int_or(event.get("sequence"), len(points) + 1)
            timestamp = _timestamp_ms_or(event.get("at"), start_time_ms)
            for key, value in (
                ("progress.completed_packets", completed),
                ("progress.failed_packets", failed),
                ("progress.reused_packets", reused),
            ):
                points.append(_metric(key, float(value), sequence, timestamp))

        calls = len(model_calls)
        completed_calls = [item for item in model_calls if item.get("status") == "completed"]
        input_tokens = sum(
            _usage_token(item, "prompt_tokens", "input_tokens") for item in completed_calls
        )
        output_tokens = sum(
            _usage_token(item, "completion_tokens", "output_tokens") for item in completed_calls
        )
        total_tokens = sum(_usage_token(item, "total_tokens") for item in completed_calls)
        if total_tokens == 0:
            total_tokens = input_tokens + output_tokens
        configured_cost = sum(
            _number_or(item.get("configured_cost"), 0.0) for item in completed_calls
        )
        latency = sum(_number_or(item.get("latency_seconds"), 0.0) for item in model_calls)
        preflight_rejected = 0
        for status_item in statuses:
            if status_item.result is None:
                continue
            pool = status_item.result.provenance.get("endpoint_pool")
            if isinstance(pool, Mapping):
                preflight_rejected += _int_or(pool.get("preflight_rejections"), 0)
                preflight = pool.get("last_preflight")
                if isinstance(preflight, Mapping) and preflight.get("status") == "exceeds":
                    preflight_rejected += 1

        scalar: dict[str, float] = {
            "cases.total": float(len(_report_cases(report))),
            "cases.succeeded": float(len(_report_cases(report))),
            "cases.failed": float(
                sum(
                    item.state in {StageState.FAILED, StageState.REJECTED}
                    for item in statuses
                    if item.packet.stage in {StageKind.SOLVE, StageKind.JUDGE}
                )
            ),
            "cases.reused": float(
                sum(
                    item.result is not None and item.result.provenance.get("cache_hit") is True
                    for item in statuses
                    if item.packet.stage in {StageKind.SOLVE, StageKind.JUDGE}
                )
            ),
            "calls.total": float(calls),
            "tokens.input": float(input_tokens),
            "tokens.output": float(output_tokens),
            "tokens.total": float(total_tokens),
            "cost.configured": float(configured_cost),
            "latency.total_seconds": float(latency),
            "preflight.rejected": float(preflight_rejected),
            "judge.calibration_passed": float(self._judge_validation_passed(statuses)),
        }
        report_cases = _report_cases(report)
        if any(item.get("comparison_arm") == "optimized" for item in report_cases):
            report_cases = tuple(
                item for item in report_cases if item.get("comparison_arm") == "optimized"
            )
        scores = [_number_or(item.get("score"), math.nan) for item in report_cases]
        scores = [value for value in scores if math.isfinite(value)]
        if scores:
            scalar.update(
                {
                    "score.mean": statistics.fmean(scores),
                    "score.stddev": statistics.pstdev(scores),
                    "score.minimum": min(scores),
                    "score.maximum": max(scores),
                }
            )
        comparison = report.get("test_comparison") if report is not None else None
        if isinstance(comparison, Mapping):
            scalar.update(
                {
                    "copro.baseline_mean_score": _number_or(
                        comparison.get("baseline_mean_score"), 0.0
                    ),
                    "copro.optimized_mean_score": _number_or(
                        comparison.get("optimized_mean_score"), 0.0
                    ),
                    "copro.micro_case_delta": _number_or(comparison.get("micro_case_delta"), 0.0),
                    "copro.macro_skill_delta": _number_or(comparison.get("macro_skill_delta"), 0.0),
                    "copro.skills_improved": float(
                        _int_or(comparison.get("positive_skill_count"), 0)
                    ),
                    "copro.skills_regressed": float(
                        _int_or(comparison.get("negative_skill_count"), 0)
                    ),
                    "copro.skills_unchanged": float(
                        _int_or(comparison.get("unchanged_skill_count"), 0)
                    ),
                }
            )
        for item in _latency_summary_rows(statuses):
            stage = str(item["stage"])
            scalar.update(
                {
                    f"latency.stage.{stage}.p50_seconds": _number_or(item.get("p50_seconds"), 0.0),
                    f"latency.stage.{stage}.p95_seconds": _number_or(item.get("p95_seconds"), 0.0),
                    f"latency.stage.{stage}.maximum_seconds": _number_or(
                        item.get("maximum_seconds"), 0.0
                    ),
                }
            )
        for item in _token_usage_rows(model_calls):
            stage = str(item["stage"])
            input_tokens = _int_or(item.get("input_tokens"), 0)
            output_tokens = _int_or(item.get("output_tokens"), 0)
            unclassified_tokens = _int_or(item.get("unclassified_tokens"), 0)
            scalar.update(
                {
                    f"tokens.stage.{stage}.input": float(input_tokens),
                    f"tokens.stage.{stage}.output": float(output_tokens),
                    f"tokens.stage.{stage}.unclassified": float(unclassified_tokens),
                    f"tokens.stage.{stage}.total": float(
                        input_tokens + output_tokens + unclassified_tokens
                    ),
                }
            )
        for item in self._endpoint_rows(statuses):
            endpoint = str(item["endpoint_id"])
            capacity = _int_or(item.get("configured_capacity"), 0)
            peak = _int_or(item.get("peak_observed_concurrency"), 0)
            scalar.update(
                {
                    f"endpoint.{endpoint}.requests": float(_int_or(item.get("requests"), 0)),
                    f"endpoint.{endpoint}.failed_requests": float(
                        _int_or(item.get("failed_requests"), 0)
                    ),
                    f"endpoint.{endpoint}.queue_wait_seconds": _number_or(
                        item.get("queue_wait_seconds"), 0.0
                    ),
                    f"endpoint.{endpoint}.peak_saturation": (
                        float(peak / capacity) if capacity else 0.0
                    ),
                }
            )
        for item in _cache_reuse_rows(statuses):
            stage = str(item["stage"])
            total = _int_or(item.get("total"), 0)
            reused_count = _int_or(item.get("reused"), 0)
            scalar.update(
                {
                    f"cache.stage.{stage}.executed": float(_int_or(item.get("executed"), 0)),
                    f"cache.stage.{stage}.reused": float(reused_count),
                    f"cache.stage.{stage}.reuse_ratio": (
                        float(reused_count / total) if total else 0.0
                    ),
                }
            )
        terminal_step = len(transitions) + len(lifecycle) + 1
        terminal_time = _timestamp_ms(lifecycle[-1].at) if lifecycle else start_time_ms
        for key, value in sorted(scalar.items()):
            points.append(_metric(key, value, terminal_step, terminal_time))
        return tuple(points)

    def _judge_validation_passed(self, statuses: Sequence[StageStatus]) -> bool:
        for status in statuses:
            if status.packet.stage is not StageKind.VALIDATE_JUDGE or status.result is None:
                continue
            output = self.ledger.read_output(status.result)
            if isinstance(output, Mapping):
                return output.get("ordering_consistent") is True
        return False

    def _tables(
        self,
        statuses: Sequence[StageStatus],
        model_calls: Sequence[Mapping[str, object]],
        report: Mapping[str, object] | None,
        scope_by_packet: Mapping[str, str],
    ) -> tuple[TableArtifact, ...]:
        stage_rows = tuple(self._stage_row(item) for item in statuses)
        call_rows = tuple(self._model_call_row(item) for item in model_calls)
        stage_failure_rows = self._stage_failure_rows(statuses, model_calls, scope_by_packet)
        call_failure_rows = self._model_call_failure_rows(statuses, model_calls, scope_by_packet)
        failure_summary_rows = _failure_summary_rows(stage_failure_rows, call_failure_rows)
        cases = tuple(self._case_row(item) for item in _report_cases(report))
        criteria = tuple(self._criterion_rows(_report_cases(report)))
        endpoint_rows = self._endpoint_rows(statuses)
        calibration_rows = self._calibration_rows(statuses)
        optimization_rows, validation_rows = self._optimization_rows(report)
        comparison = report.get("test_comparison") if report is not None else None
        comparison_cases = _comparison_rows(comparison, "cases")
        comparison_skills = _comparison_rows(comparison, "skills")
        comparison_bands = _comparison_rows(comparison, "length_bands")
        comparison_criteria = _comparison_rows(comparison, "criteria")
        token_counts = _expected_response_token_rows(report)
        values = {
            "failure-summary": failure_summary_rows,
            "stage-failures": stage_failure_rows,
            "model-call-failures": call_failure_rows,
            "stage-attempts": stage_rows,
            "model-calls": call_rows,
            "case-results": cases,
            "criterion-results": criteria,
            "endpoint-usage": endpoint_rows,
            "judge-calibration": calibration_rows,
            "optimization-candidates": optimization_rows,
            "optimization-validation": validation_rows,
            "copro-case-comparison": comparison_cases,
            "copro-skill-comparison": comparison_skills,
            "copro-token-band-comparison": comparison_bands,
            "copro-criterion-comparison": comparison_criteria,
            "expected-response-token-counts": token_counts,
        }
        return tuple(_table(name, rows) for name, rows in values.items() if rows)

    def _stage_row(self, status: StageStatus) -> Mapping[str, object]:
        result = status.result
        error = result.error if result is not None else None
        endpoint_ids: tuple[str, ...] = ()
        if result is not None:
            pool = result.provenance.get("endpoint_pool")
            if isinstance(pool, Mapping):
                routes = pool.get("routes")
                raw_routes = (
                    routes
                    if isinstance(routes, Sequence) and not isinstance(routes, (str, bytes))
                    else (pool,)
                )
                endpoint_ids = tuple(
                    sorted(
                        {
                            str(route.get("endpoint_id"))
                            for route in raw_routes
                            if isinstance(route, Mapping)
                            and isinstance(route.get("endpoint_id"), str)
                        }
                    )
                )
        return {
            "packet_id": status.packet.packet_id,
            "work_id": status.packet.work_id,
            "attempt_id": status.packet.attempt_id,
            "result_id": None if result is None else result.result_id,
            "stage": status.packet.stage.value,
            "state": status.state.value,
            "attempt": status.packet.attempt,
            "replicate_slot": status.packet.replicate_slot,
            "executor": status.packet.executor.id,
            "model": status.packet.model.id,
            "endpoint_ids": endpoint_ids,
            "cache_hit": bool(result is not None and result.provenance.get("cache_hit") is True),
            "source_run_id": (None if result is None else result.provenance.get("source_run_id")),
            "source_result_id": (
                None if result is None else result.provenance.get("source_result_id")
            ),
            "model_calls": 0 if result is None else result.usage.get("model_calls", 0),
            "tokens": 0 if result is None else result.usage.get("tokens", 0),
            "configured_cost": (
                0.0 if result is None else result.usage.get("configured_cost", 0.0)
            ),
            "duration_seconds": 0.0 if result is None else _duration(status),
            "started_at": None if result is None else result.timing.get("started_at"),
            "completed_at": None if result is None else result.timing.get("completed_at"),
            "error_kind": None if error is None else error.kind,
            "error": None if error is None else error.message,
        }

    def _model_call_row(self, item: Mapping[str, object]) -> Mapping[str, object]:
        usage = _mapping(item.get("usage", {}), "model call usage")
        error = self._model_call_error(item)
        parameters = _mapping(item.get("parameters", {}), "model call parameters")
        error_kind = item.get("error_kind")
        return {
            "call_id": item.get("call_id"),
            "packet_id": item.get("packet_id"),
            "work_id": item.get("work_id"),
            "attempt_id": item.get("attempt_id"),
            "stage": item.get("stage"),
            "role": item.get("role"),
            "provider": item.get("provider_id"),
            "model": item.get("model"),
            "status": item.get("status"),
            "error_kind": error_kind,
            "error_message": error.get("message"),
            "root_exception_type": error.get("root_exception_type"),
            "root_error_message": error.get("root_message") or error.get("message"),
            "http_status": error.get("status_code"),
            "error_code": error.get("error_code"),
            "errno": error.get("errno"),
            "retryable": (
                error.get("retryable")
                if isinstance(error.get("retryable"), bool)
                else _retryable_failure(error_kind if isinstance(error_kind, str) else None)
            ),
            "endpoint_base_url": error.get("endpoint_base_url") or parameters.get("api_base"),
            "recorded_at": item.get("recorded_at"),
            "input_tokens": _usage_token(item, "prompt_tokens", "input_tokens"),
            "output_tokens": _usage_token(item, "completion_tokens", "output_tokens"),
            "total_tokens": _usage_token(item, "total_tokens"),
            "latency_seconds": item.get("latency_seconds"),
            "configured_cost": item.get("configured_cost"),
            "request_content_id": _nested_value(item, "request_artifact", "content_id"),
            "response_content_id": _nested_value(item, "response_artifact", "content_id"),
            "usage": dict(usage),
        }

    def _model_call_error(self, item: Mapping[str, object]) -> Mapping[str, object]:
        raw = item.get("error")
        if isinstance(raw, Mapping):
            return raw
        if item.get("status") != "failed":
            return {}
        try:
            reference = self._stored_reference(item.get("response_artifact"))
            with self.store.get_blob(reference) as handle:
                payload = json.load(handle)
        except (IntegrityError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, Mapping):
            return {}
        nested = payload.get("error")
        return nested if isinstance(nested, Mapping) else payload

    def _stage_failure_rows(
        self,
        statuses: Sequence[StageStatus],
        model_calls: Sequence[Mapping[str, object]],
        scope_by_packet: Mapping[str, str],
    ) -> tuple[Mapping[str, object], ...]:
        calls_by_packet: dict[str, list[Mapping[str, object]]] = defaultdict(list)
        for call in model_calls:
            packet_id = call.get("packet_id")
            if isinstance(packet_id, str):
                calls_by_packet[packet_id].append(call)
        attempts_by_work: dict[str, list[StageStatus]] = defaultdict(list)
        for status in statuses:
            attempts_by_work[status.packet.work_id].append(status)

        rows: list[Mapping[str, object]] = []
        for status in statuses:
            if status.state not in {StageState.FAILED, StageState.REJECTED}:
                continue
            result = status.result
            stage_error = None if result is None else result.error
            failed_calls = [
                call
                for call in calls_by_packet.get(status.packet.packet_id, ())
                if call.get("status") == "failed"
            ]
            call_errors = [self._model_call_error(call) for call in failed_calls]
            call_errors = [value for value in call_errors if value]
            root = call_errors[-1] if call_errors else {}
            parameters = [
                _mapping(call.get("parameters", {}), "model call parameters")
                for call in failed_calls
            ]
            base_urls = tuple(
                sorted(
                    {
                        str(value)
                        for value in (
                            *(item.get("api_base") for item in parameters),
                            *(item.get("endpoint_base_url") for item in call_errors),
                        )
                        if isinstance(value, str) and value
                    }
                )
            )
            later = [
                item
                for item in attempts_by_work[status.packet.work_id]
                if item.packet.attempt > status.packet.attempt
            ]
            if any(item.state is StageState.PERSISTED for item in later):
                retry_outcome = "recovered"
            elif any(item.state in {StageState.PREPARED, StageState.CLAIMED} for item in later):
                retry_outcome = "pending"
            elif later:
                retry_outcome = "failed"
            else:
                retry_outcome = "not_retried"
            context = self._failure_context(status)
            base = self._stage_row(status)
            rows.append(
                {
                    "stage": status.packet.stage.value,
                    "scope": scope_by_packet.get(status.packet.packet_id),
                    **context,
                    "state": status.state.value,
                    "error_kind": None if stage_error is None else stage_error.kind,
                    "stage_error_message": (None if stage_error is None else stage_error.message),
                    "root_exception_type": root.get("root_exception_type")
                    or root.get("exception_type"),
                    "root_error_message": root.get("root_message") or root.get("message"),
                    "http_status": root.get("status_code"),
                    "error_code": root.get("error_code"),
                    "errno": root.get("errno"),
                    "retryable": root.get("retryable")
                    if isinstance(root.get("retryable"), bool)
                    else _retryable_failure(None if stage_error is None else stage_error.kind),
                    "retry_outcome": retry_outcome,
                    "endpoint_ids": base.get("endpoint_ids"),
                    "endpoint_base_urls": base_urls,
                    "failed_model_calls": len(failed_calls),
                    "model_call_ids": tuple(str(call.get("call_id")) for call in failed_calls),
                    "attempt": status.packet.attempt,
                    "worker_id": None if result is None else result.executor.get("worker_id"),
                    "started_at": base.get("started_at"),
                    "failed_at": base.get("completed_at"),
                    "duration_seconds": base.get("duration_seconds"),
                    "packet_id": status.packet.packet_id,
                    "work_id": status.packet.work_id,
                    "result_id": base.get("result_id"),
                }
            )
        return tuple(
            sorted(
                rows,
                key=lambda item: (
                    str(item.get("stage")),
                    str(item.get("scope")),
                    str(item.get("failed_at")),
                ),
            )
        )

    def _model_call_failure_rows(
        self,
        statuses: Sequence[StageStatus],
        model_calls: Sequence[Mapping[str, object]],
        scope_by_packet: Mapping[str, str],
    ) -> tuple[Mapping[str, object], ...]:
        status_by_packet = {item.packet.packet_id: item for item in statuses}
        persisted_work = {
            item.packet.work_id for item in statuses if item.state is StageState.PERSISTED
        }
        rows: list[Mapping[str, object]] = []
        for call in model_calls:
            if call.get("status") != "failed":
                continue
            packet_id = call.get("packet_id")
            status = status_by_packet.get(str(packet_id))
            error = self._model_call_error(call)
            parameters = _mapping(call.get("parameters", {}), "model call parameters")
            context = self._failure_context(status) if status is not None else {}
            endpoint_ids = self._stage_row(status).get("endpoint_ids") if status is not None else ()
            work_id = call.get("work_id")
            recovered = isinstance(work_id, str) and work_id in persisted_work
            error_kind = call.get("error_kind") or error.get("category")
            rows.append(
                {
                    "stage": call.get("stage"),
                    "scope": scope_by_packet.get(str(packet_id)),
                    **context,
                    "role": call.get("role"),
                    "provider": call.get("provider_id"),
                    "model": call.get("model"),
                    "endpoint_ids": endpoint_ids,
                    "endpoint_base_url": error.get("endpoint_base_url")
                    or parameters.get("api_base"),
                    "error_kind": error_kind,
                    "exception_type": error.get("exception_type"),
                    "error_message": error.get("message"),
                    "root_exception_type": error.get("root_exception_type"),
                    "root_error_message": error.get("root_message") or error.get("message"),
                    "http_status": error.get("status_code"),
                    "error_code": error.get("error_code"),
                    "errno": error.get("errno"),
                    "retryable": (
                        error.get("retryable")
                        if isinstance(error.get("retryable"), bool)
                        else _retryable_failure(error_kind if isinstance(error_kind, str) else None)
                    ),
                    "recovered_by_retry": recovered,
                    "recorded_at": call.get("recorded_at"),
                    "latency_seconds": call.get("latency_seconds"),
                    "call_id": call.get("call_id"),
                    "packet_id": packet_id,
                    "work_id": work_id,
                    "attempt_id": call.get("attempt_id"),
                }
            )
        return tuple(
            sorted(
                rows,
                key=lambda item: (
                    str(item.get("stage")),
                    str(item.get("endpoint_base_url")),
                    str(item.get("recorded_at")),
                    str(item.get("call_id")),
                ),
            )
        )

    def _failure_context(self, status: StageStatus) -> Mapping[str, object]:
        packet = status.packet
        values: list[Mapping[str, object]] = [packet.work_spec]
        for artifact in packet.inputs:
            if artifact.media_type != "application/json":
                continue
            try:
                reference = self.store.reference(artifact.content_id, artifact.media_type)
                with self.store.get_blob(reference) as handle:
                    value = json.load(handle)
            except (IntegrityError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, Mapping):
                values.append(value)
        context: dict[str, object] = {}
        for value in values:
            for field in (
                "case_id",
                "skill",
                "split",
                "repetition",
                "evaluation_phase",
                "partition",
                "candidate_id",
                "round",
                "scope",
                "strategy",
                "target_kind",
                "target_id",
            ):
                if context.get(field) is None and value.get(field) is not None:
                    context[field] = value[field]
            target = value.get("target")
            if isinstance(target, Mapping):
                context.setdefault("target_kind", target.get("kind"))
                context.setdefault("target_id", target.get("id"))
        return context

    def _case_row(self, item: Mapping[str, object]) -> Mapping[str, object]:
        base: dict[str, object] = {
            "case_id": item.get("case_id"),
            "case_hash": item.get("case_hash"),
            "skill": item.get("skill_name"),
            "split": item.get("split"),
            "repetition": item.get("repetition"),
            "comparison_arm": item.get("comparison_arm"),
            "candidate_id": item.get("candidate_id"),
            "strategy": item.get("strategy"),
            "expected_response_tokens": item.get("expected_response_tokens"),
            "expected_response_length_band": item.get("expected_response_length_band"),
            "score": item.get("score"),
            "hard_failure_count": len(
                item.get("hard_failures", []) if isinstance(item.get("hard_failures"), list) else []
            ),
        }
        if self.recording.content_policy is not RecordingContentPolicy.HASHES_ONLY:
            base.update(
                {
                    "title": item.get("case_title"),
                    "hard_failures": item.get("hard_failures"),
                    "judge_feedback": item.get("judge_feedback"),
                }
            )
        return base

    def _criterion_rows(
        self, cases: Sequence[Mapping[str, object]]
    ) -> Sequence[Mapping[str, object]]:
        rows: list[Mapping[str, object]] = []
        for case in cases:
            raw = case.get("criterion_scores", [])
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
                continue
            for criterion in raw:
                if not isinstance(criterion, Mapping):
                    continue
                rows.append(
                    {
                        "case_id": case.get("case_id"),
                        "skill": case.get("skill_name"),
                        "repetition": case.get("repetition"),
                        "comparison_arm": case.get("comparison_arm"),
                        "candidate_id": case.get("candidate_id"),
                        "strategy": case.get("strategy"),
                        "criterion_id": criterion.get("id"),
                        "weight": criterion.get("weight"),
                        "score": criterion.get("score"),
                        "weighted_score": criterion.get("weighted_score"),
                        **(
                            {"description": criterion.get("description")}
                            if self.recording.content_policy
                            is not RecordingContentPolicy.HASHES_ONLY
                            else {}
                        ),
                    }
                )
        return tuple(rows)

    def _endpoint_rows(self, statuses: Sequence[StageStatus]) -> tuple[Mapping[str, object], ...]:
        aggregates: dict[str, Counter[str]] = defaultdict(Counter)
        durations: dict[str, Counter[str]] = defaultdict(Counter)
        peaks: dict[str, int] = defaultdict(int)
        metadata: dict[str, dict[str, object]] = {}
        for status in statuses:
            if status.result is None:
                continue
            pool = status.result.provenance.get("endpoint_pool")
            if not isinstance(pool, Mapping):
                continue
            routes = pool.get("routes")
            if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
                routes = [pool]
            for route in routes:
                if not isinstance(route, Mapping) or not isinstance(route.get("endpoint_id"), str):
                    continue
                endpoint = str(route["endpoint_id"])
                metadata[endpoint] = {
                    "endpoint_id": endpoint,
                    "pool_hash": route.get("pool_hash"),
                    "endpoint_configuration_hash": route.get("endpoint_configuration_hash"),
                    "configured_capacity": route.get("max_concurrent_requests"),
                }
                for key in (
                    "requests",
                    "completed_requests",
                    "failed_requests",
                    "preflight_checks",
                    "preflight_rejections",
                    "preflight_skips",
                    "model_calls",
                    "completed_calls",
                    "failed_calls",
                    "tokens",
                ):
                    aggregates[endpoint][key] += _int_or(route.get(key), 0)
                for key in ("latency_seconds", "queue_wait_seconds", "configured_cost"):
                    durations[endpoint][key] += _number_or(route.get(key), 0.0)
                peaks[endpoint] = max(
                    peaks[endpoint],
                    _int_or(route.get("peak_observed_endpoint_concurrency"), 0),
                    _int_or(route.get("endpoint_active_at_start"), 0),
                )
        return tuple(
            {
                **metadata[key],
                **dict(aggregates[key]),
                **dict(durations[key]),
                "peak_observed_concurrency": peaks[key],
            }
            for key in sorted(metadata)
        )

    def _calibration_rows(
        self, statuses: Sequence[StageStatus]
    ) -> tuple[Mapping[str, object], ...]:
        for status in statuses:
            if status.packet.stage is not StageKind.VALIDATE_JUDGE or status.result is None:
                continue
            output = self.ledger.read_output(status.result)
            if not isinstance(output, Mapping):
                return ()
            scores = output.get("fixture_scores")
            if not isinstance(scores, Mapping):
                return ()
            fixture_metadata = self._judge_validation_fixture_metadata(status)
            repetitions = _int_or(status.packet.work_spec.get("repetitions"), 1)
            return tuple(
                {
                    "fixture_id": fixture_id,
                    **fixture_metadata.get(str(fixture_id), {}),
                    "repetitions": repetitions,
                    "score_aggregation": (
                        "median score per rubric criterion across repetitions, "
                        "then rubric-weighted sum"
                    ),
                    "aggregate_score": score,
                    "ordering_consistent": output.get("ordering_consistent"),
                }
                for fixture_id, score in sorted(scores.items(), key=lambda item: str(item[0]))
            )
        return ()

    def _judge_validation_fixture_metadata(
        self, status: StageStatus
    ) -> Mapping[str, Mapping[str, object]]:
        """Read reporting-only calibration labels from the frozen experiment definition.

        Quality labels deliberately do not appear in the judge work packet because they
        would reveal the expected ranking to the model. The immutable experiment
        definition retains those labels for provenance and reporting after execution.
        """

        try:
            run = self.ledger.load_run(status.packet.run_id)
            reference = self._stored_reference(run.get("experiment_definition_artifact"))
            with self.store.get_blob(reference) as handle:
                definition = json.load(handle)
        except (IntegrityError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(definition, Mapping):
            return {}
        raw_cases = definition.get("cases", ())
        cases: dict[str, Mapping[str, object]] = {}
        if isinstance(raw_cases, Sequence) and not isinstance(raw_cases, (str, bytes)):
            cases = {
                str(item["id"]): item
                for item in raw_cases
                if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            }
        validation = definition.get("judge_validation")
        if not isinstance(validation, Mapping):
            return {}
        raw_fixtures = validation.get("fixtures", ())
        if not isinstance(raw_fixtures, Sequence) or isinstance(raw_fixtures, (str, bytes)):
            return {}
        metadata: dict[str, Mapping[str, object]] = {}
        for raw_fixture in raw_fixtures:
            if not isinstance(raw_fixture, Mapping):
                continue
            fixture_id = raw_fixture.get("id")
            case_id = raw_fixture.get("case_id")
            label = raw_fixture.get("label")
            if not isinstance(fixture_id, str):
                continue
            case = cases.get(str(case_id), {})
            rubric = case.get("rubric") if isinstance(case, Mapping) else None
            criteria = rubric.get("criteria") if isinstance(rubric, Mapping) else None
            criterion_ids: tuple[str, ...] = ()
            if isinstance(criteria, Sequence) and not isinstance(criteria, (str, bytes)):
                criterion_ids = tuple(
                    str(item["id"])
                    for item in criteria
                    if isinstance(item, Mapping) and isinstance(item.get("id"), str)
                )
            metadata[fixture_id] = {
                "case_id": case_id,
                "case_title": case.get("title") if isinstance(case, Mapping) else None,
                "skill": case.get("skill") if isinstance(case, Mapping) else None,
                "quality_label": label,
                "rubric_hash": content_hash(rubric) if isinstance(rubric, Mapping) else None,
                "rubric_criteria": criterion_ids,
            }
        return metadata

    @staticmethod
    def _optimization_rows(
        report: Mapping[str, object] | None,
    ) -> tuple[tuple[Mapping[str, object], ...], tuple[Mapping[str, object], ...]]:
        if report is None:
            return (), ()
        validation = report.get("validation")
        if not isinstance(validation, Mapping):
            return (), ()
        candidates = validation.get("candidates", [])
        candidate_rows = (
            tuple(dict(item) for item in candidates if isinstance(item, Mapping))
            if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes))
            else ()
        )
        validation_rows: list[Mapping[str, object]] = []
        optimization = report.get("optimization")
        if isinstance(optimization, Mapping):
            raw_rounds = optimization.get("rounds", [])
            if isinstance(raw_rounds, Sequence) and not isinstance(raw_rounds, (str, bytes)):
                for raw_round in raw_rounds:
                    if not isinstance(raw_round, Mapping):
                        continue
                    accounting = raw_round.get("proposal_accounting")
                    accounting_columns = (
                        {f"proposal_{key}": value for key, value in accounting.items()}
                        if isinstance(accounting, Mapping)
                        else {}
                    )
                    for raw_candidate in raw_round.get("candidates", []):
                        if isinstance(raw_candidate, Mapping):
                            validation_rows.append(
                                {
                                    "scope": raw_round.get("scope"),
                                    "round": raw_round.get("round"),
                                    "target_kind": raw_round.get("target_kind"),
                                    "target_id": raw_round.get("target_id"),
                                    "selected_candidate_id": raw_round.get("selected_candidate_id"),
                                    **accounting_columns,
                                    **dict(raw_candidate),
                                }
                            )
        return candidate_rows, tuple(validation_rows)

    def _traces(
        self,
        statuses: Sequence[StageStatus],
        model_calls: Sequence[Mapping[str, object]],
        scope_by_packet: Mapping[str, str],
    ) -> tuple[TraceDescriptor, ...]:
        calls_by_attempt: dict[str, list[Mapping[str, object]]] = defaultdict(list)
        for call in model_calls:
            attempt_id = call.get("attempt_id")
            if isinstance(attempt_id, str):
                calls_by_attempt[attempt_id].append(call)
        traces: list[TraceDescriptor] = []
        for status in statuses:
            result = status.result
            if result is None:
                continue
            packet = status.packet
            calls = sorted(
                calls_by_attempt.get(packet.attempt_id, []),
                key=lambda item: str(item.get("call_id")),
            )
            inputs = self._trace_inputs(status)
            outputs = self._trace_outputs(status)
            spans = tuple(self._span(call, index) for index, call in enumerate(calls, start=1))
            links: list[Mapping[str, object]] = [
                {"type": "dependency", "packet_id": dependency}
                for dependency in packet.dependencies
            ]
            if result.provenance.get("cache_hit") is True:
                links.append(
                    {
                        "type": "reuse",
                        "source_run_id": result.provenance.get("source_run_id"),
                        "source_result_id": result.provenance.get("source_result_id"),
                        "source_work_id": result.provenance.get("source_work_id"),
                    }
                )
            attributes = {
                "agent_eval.run_id": packet.run_id,
                "agent_eval.experiment_revision_id": packet.experiment_revision_id,
                "agent_eval.stage": packet.stage.value,
                "agent_eval.work_id": packet.work_id,
                "agent_eval.packet_id": packet.packet_id,
                "agent_eval.attempt_id": packet.attempt_id,
                "agent_eval.result_id": result.result_id,
                "agent_eval.executor": packet.executor.id,
                "agent_eval.model": packet.model.id,
                "agent_eval.cache_hit": result.provenance.get("cache_hit") is True,
                "agent_eval.source_result_id": result.provenance.get("source_result_id"),
                "agent_eval.error_kind": None if result.error is None else result.error.kind,
            }
            identity = {
                "attempt_id": packet.attempt_id,
                "result_id": result.result_id,
                "scope_id": scope_by_packet[packet.packet_id],
                "spans": spans,
                "links": links,
            }
            traces.append(
                TraceDescriptor(
                    id=content_hash(identity),
                    name=f"{packet.stage.value}:{packet.attempt_id.removeprefix('sha256:')[:12]}",
                    scope_id=scope_by_packet[packet.packet_id],
                    status=result.status,
                    inputs=inputs,
                    outputs=outputs,
                    attributes=attributes,
                    spans=spans,
                    links=tuple(links),
                )
            )
        return _topological_traces(tuple(traces))

    def _trace_inputs(self, status: StageStatus) -> object:
        packet = status.packet
        return {
            "inputs": [_artifact_pointer(item) for item in packet.inputs],
            "instructions": _artifact_pointer(packet.instructions),
            "content_location": "MLflow artifacts and canonical artifact index",
        }

    def _trace_outputs(self, status: StageStatus) -> object:
        assert status.result is not None
        return {
            "status": status.result.status,
            "outputs": [_artifact_pointer(item) for item in status.result.outputs],
            "error_kind": (None if status.result.error is None else status.result.error.kind),
            "content_location": "MLflow artifacts and canonical artifact index",
        }

    def _span(self, call: Mapping[str, object], index: int) -> SpanDescriptor:
        call_id = _string(call.get("call_id"), "model call id")
        request_reference = self._stored_reference(call.get("request_artifact"))
        response_reference = self._stored_reference(call.get("response_artifact"))
        request = {
            **_artifact_pointer(request_reference),
            "mlflow_artifact_path": f"evidence/model-calls/{call_id}/request.json",
        }
        response = {
            **_artifact_pointer(response_reference),
            "mlflow_artifact_path": f"evidence/model-calls/{call_id}/response.json",
        }
        return SpanDescriptor(
            id=content_hash(
                {
                    "call_id": call_id,
                    "request": _nested_value(call, "request_artifact", "content_id"),
                    "response": _nested_value(call, "response_artifact", "content_id"),
                }
            ),
            name=f"provider-call-{index:03d}",
            type="LLM",
            inputs=request,
            outputs=response,
            attributes={
                "agent_eval.call_id": call_id,
                "agent_eval.role": call.get("role"),
                "agent_eval.provider": call.get("provider_id"),
                "agent_eval.model": call.get("model"),
                "agent_eval.status": call.get("status"),
                "agent_eval.error_kind": call.get("error_kind"),
                "agent_eval.latency_seconds": call.get("latency_seconds"),
                "agent_eval.configured_cost": call.get("configured_cost"),
                "agent_eval.usage": json.dumps(
                    call.get("usage", {}), sort_keys=True, separators=(",", ":")
                ),
            },
        )

    def _dag(
        self,
        statuses: Sequence[StageStatus],
        scope_by_packet: Mapping[str, str],
    ) -> Mapping[str, object]:
        nodes: list[Mapping[str, object]] = []
        edges: list[Mapping[str, object]] = []
        attempts_by_work: dict[str, list[StageStatus]] = defaultdict(list)
        for status in statuses:
            attempts_by_work[status.packet.work_id].append(status)
            result = status.result
            phase, candidate, partition = self._stage_coordinates(status)
            work_node_id = f"work:{status.packet.work_id}"
            attempt_node_id = f"attempt:{status.packet.packet_id}"
            nodes.append(
                {
                    "id": work_node_id,
                    "type": "work",
                    "stage": status.packet.stage.value,
                    "work_id": status.packet.work_id,
                }
            )
            nodes.append(
                {
                    "id": attempt_node_id,
                    "type": "attempt",
                    "stage": status.packet.stage.value,
                    "state": status.state.value,
                    "scope": scope_by_packet.get(
                        status.packet.packet_id, status.packet.stage.value
                    ),
                    "phase": phase,
                    "candidate_id": candidate,
                    "partition": partition,
                    "work_id": status.packet.work_id,
                    "attempt_id": status.packet.attempt_id,
                    "attempt": status.packet.attempt,
                    "result_id": None if result is None else result.result_id,
                    "cache_hit": bool(
                        result is not None and result.provenance.get("cache_hit") is True
                    ),
                }
            )
            edges.append({"source": work_node_id, "target": attempt_node_id, "type": "attempt"})
            for dependency in status.packet.dependencies:
                dependency_node_id = f"attempt:{dependency}"
                edges.append(
                    {
                        "source": dependency_node_id,
                        "target": attempt_node_id,
                        "type": "dependency",
                    }
                )
            if result is not None:
                result_node_id = f"result:{result.result_id}"
                nodes.append(
                    {
                        "id": result_node_id,
                        "type": "result",
                        "stage": status.packet.stage.value,
                        "state": result.status,
                        "result_id": result.result_id,
                        "cache_hit": result.provenance.get("cache_hit") is True,
                    }
                )
                edges.append(
                    {"source": attempt_node_id, "target": result_node_id, "type": "produces"}
                )
            if result is not None and result.provenance.get("cache_hit") is True:
                source = result.provenance.get("source_result_id")
                if isinstance(source, str):
                    reuse_id = f"result:{source}"
                    nodes.append(
                        {
                            "id": reuse_id,
                            "type": "external_result",
                            "result_id": source,
                            "source_run_id": result.provenance.get("source_run_id"),
                        }
                    )
                    edges.append(
                        {
                            "source": reuse_id,
                            "target": f"result:{result.result_id}",
                            "type": "reuse",
                        }
                    )
        for work_attempts in attempts_by_work.values():
            ordered = sorted(work_attempts, key=lambda item: item.packet.attempt)
            for previous, current in zip(ordered, ordered[1:], strict=False):
                edges.append(
                    {
                        "source": f"attempt:{previous.packet.packet_id}",
                        "target": f"attempt:{current.packet.packet_id}",
                        "type": "retry",
                    }
                )
        unique_nodes: dict[str, Mapping[str, object]] = {}
        for item in nodes:
            node_id = str(item["id"])
            existing = unique_nodes.get(node_id)
            if existing is None or (
                existing.get("type") == "external_result" and item.get("type") == "result"
            ):
                unique_nodes[node_id] = item
        overview = _dag_overview(tuple(unique_nodes.values()), edges)
        return {
            "schema_version": 2,
            "nodes": [unique_nodes[key] for key in sorted(unique_nodes)],
            "edges": sorted(
                edges,
                key=lambda item: (str(item["target"]), str(item["source"]), str(item["type"])),
            ),
            "overview": overview,
        }

    def _charts(
        self,
        report: Mapping[str, object] | None,
        statuses: Sequence[StageStatus],
        model_calls: Sequence[Mapping[str, object]],
    ) -> tuple[Mapping[str, tuple[bytes, str]], tuple[Mapping[str, object], ...]]:
        artifacts: dict[str, tuple[bytes, str]] = {}
        omissions: list[Mapping[str, object]] = []
        skills = report.get("skills") if report is not None else None
        skill_values = _named_values(skills, "skill_name", "mean_score")
        if skill_values:
            artifacts["graphs/scores-by-skill.svg"] = (
                _bar_svg(
                    "Mean score by skill",
                    skill_values,
                    maximum=1.0,
                    description=_chart_description("scores-by-skill.svg"),
                    unit="judge score",
                ),
                "image/svg+xml",
            )
        else:
            omissions.append({"artifact": "scores-by-skill.svg", "reason": "no_scores"})

        criteria = report.get("criteria") if report is not None else None
        criterion_rows = (
            tuple(item for item in criteria if isinstance(item, Mapping))
            if isinstance(criteria, Sequence) and not isinstance(criteria, (str, bytes))
            else ()
        )
        if criterion_rows:
            artifacts["graphs/criterion-heatmap.svg"] = (
                _heatmap_svg(
                    criterion_rows,
                    description=_chart_description("criterion-heatmap.svg"),
                ),
                "image/svg+xml",
            )
        else:
            omissions.append({"artifact": "criterion-heatmap.svg", "reason": "no_criteria"})

        latency_rows = _latency_summary_rows(statuses)
        if latency_rows:
            artifacts["graphs/stage-latency-summary.svg"] = (
                _latency_summary_svg(
                    latency_rows,
                    description=_chart_description("stage-latency-summary.svg"),
                ),
                "image/svg+xml",
            )
        else:
            omissions.append(
                {"artifact": "stage-latency-summary.svg", "reason": "no_executed_attempts"}
            )

        token_rows = _token_usage_rows(model_calls)
        if token_rows:
            artifacts["graphs/token-usage-by-stage.svg"] = (
                _token_usage_svg(
                    token_rows,
                    description=_chart_description("token-usage-by-stage.svg"),
                ),
                "image/svg+xml",
            )
        else:
            omissions.append(
                {"artifact": "token-usage-by-stage.svg", "reason": "no_model_call_tokens"}
            )

        endpoints = self._endpoint_rows(statuses)
        endpoint_values = [
            (str(item.get("endpoint_id")), float(_int_or(item.get("requests"), 0)))
            for item in endpoints
        ]
        if endpoint_values:
            artifacts["graphs/endpoint-workload.svg"] = (
                _bar_svg(
                    "Endpoint workload distribution",
                    endpoint_values,
                    description=_chart_description("endpoint-workload.svg"),
                    unit="requests",
                ),
                "image/svg+xml",
            )
        else:
            omissions.append({"artifact": "endpoint-workload.svg", "reason": "no_endpoint_usage"})

        saturation_rows = tuple(
            item
            for item in endpoints
            if _int_or(item.get("configured_capacity"), 0) > 0
            and _int_or(item.get("peak_observed_concurrency"), 0) > 0
        )
        if saturation_rows:
            artifacts["graphs/endpoint-saturation.svg"] = (
                _endpoint_saturation_svg(
                    saturation_rows,
                    description=_chart_description("endpoint-saturation.svg"),
                ),
                "image/svg+xml",
            )
        else:
            omissions.append(
                {
                    "artifact": "endpoint-saturation.svg",
                    "reason": "no_observed_concurrency",
                }
            )

        cache_rows = _cache_reuse_rows(statuses)
        if cache_rows:
            artifacts["graphs/cache-reuse-by-stage.svg"] = (
                _cache_reuse_svg(
                    cache_rows,
                    description=_chart_description("cache-reuse-by-stage.svg"),
                ),
                "image/svg+xml",
            )
        else:
            omissions.append({"artifact": "cache-reuse-by-stage.svg", "reason": "no_results"})

        optimization_observed = any(
            item.packet.stage in {StageKind.OPTIMIZE, StageKind.FIT, StageKind.SELECT}
            for item in statuses
        )
        candidates, _ = self._optimization_rows(report)
        candidate_values = _named_values(candidates, "candidate_id", "mean_score")
        if candidate_values:
            artifacts["graphs/optimization-validation.svg"] = (
                _bar_svg(
                    "Optimization validation",
                    candidate_values,
                    maximum=1.0,
                    description=_chart_description("optimization-validation.svg"),
                    unit="judge score",
                ),
                "image/svg+xml",
            )
        else:
            omissions.append(
                {
                    "artifact": "optimization-validation.svg",
                    "reason": (
                        "optimization_report_unavailable"
                        if optimization_observed
                        else "not_optimization"
                    ),
                }
            )
        comparison = report.get("test_comparison") if report is not None else None
        if isinstance(comparison, Mapping):
            overall = (
                (
                    "Untouched test set",
                    _number_or(comparison.get("baseline_mean_score"), 0.0),
                    _number_or(comparison.get("optimized_mean_score"), 0.0),
                ),
            )
            artifacts["graphs/copro-overall-paired-score.svg"] = (
                _dumbbell_svg(
                    "COPRO paired score on untouched test cases",
                    overall,
                    description=_chart_description("copro-overall-paired-score.svg"),
                ),
                "image/svg+xml",
            )
            skill_rows = _comparison_rows(comparison, "skills")
            if skill_rows:
                skill_values = tuple(
                    (
                        str(item.get("skill_name")),
                        _number_or(item.get("baseline_mean_score"), 0.0),
                        _number_or(item.get("optimized_mean_score"), 0.0),
                    )
                    for item in skill_rows
                )
                artifacts["graphs/copro-skill-lift.svg"] = (
                    _dumbbell_svg(
                        "COPRO score by skill · ordered by optimization delta",
                        skill_values,
                        description=_chart_description("copro-skill-lift.svg"),
                    ),
                    "image/svg+xml",
                )
            else:
                omissions.append(
                    {"artifact": "copro-skill-lift.svg", "reason": "no_skill_comparison"}
                )
            case_rows = _comparison_rows(comparison, "cases")
            if case_rows:
                ordered_cases = sorted(
                    case_rows,
                    key=lambda item: (
                        -_number_or(item.get("delta"), 0.0),
                        str(item.get("case_id")),
                        _int_or(item.get("repetition"), 0),
                    ),
                )
                artifacts["graphs/copro-case-paired-score.svg"] = (
                    _dumbbell_svg(
                        "COPRO paired score by test case · ordered by optimization delta",
                        tuple(
                            (
                                f"{item.get('case_id')} · repetition {item.get('repetition')}",
                                _number_or(item.get("baseline_score"), 0.0),
                                _number_or(item.get("optimized_score"), 0.0),
                            )
                            for item in ordered_cases
                        ),
                        description=_chart_description("copro-case-paired-score.svg"),
                    ),
                    "image/svg+xml",
                )
                artifacts["graphs/copro-case-delta-distribution.svg"] = (
                    _delta_histogram_svg(
                        case_rows,
                        description=_chart_description("copro-case-delta-distribution.svg"),
                    ),
                    "image/svg+xml",
                )
            else:
                omissions.append(
                    {
                        "artifact": "copro-case-paired-score.svg",
                        "reason": "no_case_comparison",
                    }
                )
                omissions.append(
                    {
                        "artifact": "copro-case-delta-distribution.svg",
                        "reason": "no_case_comparison",
                    }
                )
            token_rows = tuple(
                item
                for item in case_rows
                if isinstance(item.get("expected_response_tokens"), int)
                and not isinstance(item.get("expected_response_tokens"), bool)
            )
            if token_rows:
                artifacts["graphs/copro-delta-by-expected-tokens.svg"] = (
                    _token_delta_scatter_svg(
                        token_rows,
                        description=_chart_description("copro-delta-by-expected-tokens.svg"),
                    ),
                    "image/svg+xml",
                )
            else:
                omissions.append(
                    {
                        "artifact": "copro-delta-by-expected-tokens.svg",
                        "reason": "exact_token_measurements_unavailable",
                    }
                )
            band_rows = _comparison_rows(comparison, "length_bands")
            if band_rows:
                artifacts["graphs/copro-score-by-token-band.svg"] = (
                    _dumbbell_svg(
                        "COPRO score by expected-response token band",
                        tuple(
                            (
                                str(item.get("expected_response_length_band")),
                                _number_or(item.get("baseline_mean_score"), 0.0),
                                _number_or(item.get("optimized_mean_score"), 0.0),
                            )
                            for item in band_rows
                        ),
                        description=_chart_description("copro-score-by-token-band.svg"),
                    ),
                    "image/svg+xml",
                )
            else:
                omissions.append(
                    {
                        "artifact": "copro-score-by-token-band.svg",
                        "reason": "no_token_band_comparison",
                    }
                )
            criterion_rows = _comparison_rows(comparison, "criteria")
            if criterion_rows:
                artifacts["graphs/copro-criterion-lift.svg"] = (
                    _dumbbell_svg(
                        "COPRO score by rubric criterion · ordered by delta",
                        tuple(
                            (
                                str(item.get("criterion_id")),
                                _number_or(item.get("baseline_mean_score"), 0.0),
                                _number_or(item.get("optimized_mean_score"), 0.0),
                            )
                            for item in criterion_rows
                        ),
                        description=_chart_description("copro-criterion-lift.svg"),
                    ),
                    "image/svg+xml",
                )
            else:
                omissions.append(
                    {
                        "artifact": "copro-criterion-lift.svg",
                        "reason": "no_criterion_comparison",
                    }
                )
        else:
            reason = (
                "optimization_incomplete_before_test_comparison"
                if optimization_observed
                else "not_copro_experiment"
            )
            for artifact in (
                "copro-overall-paired-score.svg",
                "copro-skill-lift.svg",
                "copro-case-paired-score.svg",
                "copro-case-delta-distribution.svg",
                "copro-delta-by-expected-tokens.svg",
                "copro-score-by-token-band.svg",
                "copro-criterion-lift.svg",
            ):
                omissions.append({"artifact": artifact, "reason": reason})
        artifacts.update(
            _interactive_plotly_charts(
                report,
                statuses,
                model_calls,
                endpoints,
                generated_static_paths=frozenset(artifacts),
            )
        )
        return artifacts, tuple(omissions)

    def _identity_artifacts(self, run: Mapping[str, object]) -> tuple[ArtifactDescriptor, ...]:
        values: list[ArtifactDescriptor] = []
        run_id = _string(run.get("run_id"), "run id")
        run_path = self.store.manifest_root / "runs" / run_id / "run.json"
        values.append(self._file_artifact("identity/run.json", run_path, "application/json"))
        for field, path in (
            ("experiment_lock_artifact", "identity/experiment.lock.json"),
            ("experiment_bundle_artifact", "identity/experiment-bundle.json"),
            ("experiment_definition_artifact", "identity/experiment-definition.json"),
        ):
            reference = self._stored_reference(run.get(field))
            values.append(
                ArtifactDescriptor(
                    content_hash({"path": path, "reference": reference}), path, reference
                )
            )
        environment = {
            "schema_version": 1,
            "python_requirement": ">=3.12",
            "framework": "ms-agent-eval",
            "workspace_schema_version": WORKSPACE_SCHEMA_VERSION,
        }
        values.append(
            self._artifact(
                "identity/software-environment.json",
                canonical_json_bytes(environment),
                "application/json",
            )
        )
        return tuple(values)

    def _report_artifacts(self, run_id: str) -> tuple[ArtifactDescriptor, ...]:
        if self.recording.content_policy is RecordingContentPolicy.HASHES_ONLY:
            return ()
        root = self.store.summary_root / "runs" / run_id
        if not root.is_dir() or root.is_symlink():
            return ()
        values: list[ArtifactDescriptor] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if (
                relative == "index.html"
                or relative == "tables/index.html"
                or relative.startswith("graphs/")
            ):
                # These files are recorder-neutral local views of the same chart and
                # table payloads already published at the MLflow artifact root. Do not
                # duplicate them under reports/ on the next projection.
                continue
            media_type = _media_type(path)
            values.append(self._file_artifact(f"reports/{relative}", path, media_type))
        return tuple(values)

    def _materialize_local_run_assets(
        self,
        run_id: str,
        files: Mapping[str, tuple[bytes, str]],
    ) -> None:
        """Publish human-facing chart and table views outside operational storage."""

        if Path(run_id).name != run_id or run_id in {".", ".."}:
            raise IntegrityError(f"run id is not a safe path component: {run_id!r}")
        runs_root = self.store.summary_root / "runs"
        runs_root.mkdir(parents=True, exist_ok=True)
        root = runs_root / run_id
        if root.is_symlink():
            raise IntegrityError(f"local report root must not be a symlink: {root}")
        root.mkdir(parents=True, exist_ok=True)

        graph_root = root / "graphs"
        if graph_root.is_symlink():
            raise IntegrityError(f"local graph root must not be a symlink: {graph_root}")
        graph_root.mkdir(parents=True, exist_ok=True)
        expected_graphs = {
            Path(relative).relative_to("graphs").as_posix()
            for relative in files
            if relative.startswith("graphs/")
        }
        for existing in sorted(graph_root.rglob("*"), reverse=True):
            if existing.is_symlink():
                existing.unlink()
                continue
            if existing.is_file():
                relative = existing.relative_to(graph_root).as_posix()
                if relative not in expected_graphs:
                    existing.unlink()
            elif existing.is_dir():
                try:
                    existing.rmdir()
                except OSError:
                    pass

        for relative, (payload, _media_type_value) in sorted(files.items()):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                path.unlink()
            if path.is_file() and path.read_bytes() == payload:
                continue
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, path)
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise

    def _full_evidence_artifacts(
        self,
        statuses: Sequence[StageStatus],
        model_calls: Sequence[Mapping[str, object]],
    ) -> tuple[ArtifactDescriptor, ...]:
        values: list[ArtifactDescriptor] = []
        for status in statuses:
            if status.result is None:
                continue
            for output in status.result.outputs:
                reference = self.store.reference(output.content_id, output.media_type)
                path = (
                    f"evidence/results/{status.result.result_id.removeprefix('sha256:')}/"
                    f"{output.id}.json"
                )
                self._scan_reference(reference, path)
                values.append(
                    ArtifactDescriptor(
                        content_hash({"path": path, "reference": reference}), path, reference
                    )
                )
        for call in model_calls:
            call_id = _string(call.get("call_id"), "model call id")
            for field, suffix in (
                ("request_artifact", "request.json"),
                ("response_artifact", "response.json"),
            ):
                reference = _reference_from_mapping(call.get(field))
                path = f"evidence/model-calls/{call_id}/{suffix}"
                self._scan_reference(reference, path)
                values.append(
                    ArtifactDescriptor(
                        content_hash({"path": path, "reference": reference}), path, reference
                    )
                )
        return tuple(values)

    def _artifact_index(
        self,
        run_id: str,
        statuses: Sequence[StageStatus],
        model_calls: Sequence[Mapping[str, object]],
        published: Sequence[ArtifactDescriptor],
    ) -> Mapping[str, object]:
        canonical: dict[str, Mapping[str, object]] = {}
        for status in statuses:
            packet = status.packet
            for artifact in (*packet.inputs, packet.instructions):
                canonical[artifact.content_id] = {
                    "content_id": artifact.content_id,
                    "media_type": artifact.media_type,
                    "role": "stage_input",
                }
            if status.result is not None:
                for artifact in status.result.outputs:
                    canonical[artifact.content_id] = {
                        "content_id": artifact.content_id,
                        "media_type": artifact.media_type,
                        "role": "stage_output",
                    }
        for call in model_calls:
            for field in ("request_artifact", "response_artifact"):
                reference = _reference_from_mapping(call.get(field))
                canonical[reference.content_id] = {
                    "content_id": reference.content_id,
                    "media_type": reference.media_type,
                    "role": field,
                }
        return {
            "schema_version": 1,
            "run_id": run_id,
            "content_policy": self.recording.content_policy.value,
            "artifact_policy": self.recording.artifact_policy.value,
            "canonical_artifacts": [canonical[key] for key in sorted(canonical)],
            "published_artifacts": [
                {
                    "path": item.path,
                    "content_id": item.reference.content_id,
                    "media_type": item.reference.media_type,
                    "size_bytes": item.reference.size_bytes,
                    **(
                        {"description": _chart_description(Path(item.path).name)}
                        if Path(item.path).name in _CHART_DEFINITIONS
                        else {}
                    ),
                }
                for item in sorted(published, key=lambda item: item.path)
            ],
        }

    def _model_calls(self, run_id: str) -> tuple[Mapping[str, object], ...]:
        root = self.store.manifest_root / "runs" / run_id / "model-calls"
        if not root.is_dir():
            return ()
        return tuple(_load_mapping(path, "model call") for path in sorted(root.glob("*.json")))

    def _transition_rows(self, run_id: str) -> tuple[Mapping[str, object], ...]:
        root = self.store.manifest_root / "runs" / run_id / "transitions"
        if not root.is_dir():
            return ()
        rows = tuple(_load_mapping(path, "stage transition") for path in root.glob("*.json"))
        return tuple(sorted(rows, key=lambda item: _int_or(item.get("sequence"), 0)))

    def _artifact(self, path: str, payload: bytes, media_type: str) -> ArtifactDescriptor:
        if self.recording.content_policy is RecordingContentPolicy.FULL and _textual(media_type):
            _assert_no_secret_bytes(payload, path)
        reference = self.store.put_blob(BytesIO(payload), media_type)
        return ArtifactDescriptor(
            id=content_hash({"path": path, "reference": reference}),
            path=path,
            reference=reference,
        )

    def _file_artifact(
        self, logical_path: str, source: Path, media_type: str
    ) -> ArtifactDescriptor:
        payload = source.read_bytes()
        return self._artifact(logical_path, payload, media_type)

    def _scan_reference(self, reference: ArtifactReference, label: str) -> None:
        if not _textual(reference.media_type):
            return
        with self.store.get_blob(reference) as handle:
            _assert_no_secret_bytes(handle.read(), label)

    def _stored_reference(self, value: object) -> ArtifactReference:
        item = _mapping(value, "artifact reference")
        if "size_bytes" in item and "relative_path" in item:
            return _reference_from_mapping(item)
        return self.store.reference(
            _string(item.get("content_id"), "artifact reference content_id"),
            _string(item.get("media_type"), "artifact reference media_type"),
        )


def _retryable_failure(kind: str | None) -> bool:
    return kind in {"connection_error", "provider_server_error", "rate_limit_error"}


def _failure_summary_rows(
    stage_failures: Sequence[Mapping[str, object]],
    call_failures: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}

    def group(row: Mapping[str, object]) -> dict[str, object]:
        raw_urls = row.get("endpoint_base_urls")
        urls = (
            [str(value) for value in raw_urls if isinstance(value, str)]
            if isinstance(raw_urls, Sequence) and not isinstance(raw_urls, (str, bytes))
            else []
        )
        endpoint = str(row.get("endpoint_base_url") or (urls[0] if urls else "local"))
        key = (
            str(row.get("stage") or "unknown"),
            str(row.get("error_kind") or "unknown_error"),
            endpoint,
        )
        return grouped.setdefault(
            key,
            {
                "stage": key[0],
                "error_kind": key[1],
                "endpoint": key[2],
                "failed_stage_attempts": 0,
                "failed_model_calls": 0,
                "recovered_model_calls": 0,
                "retryable": _retryable_failure(key[1]),
                "scopes": set(),
                "case_ids": set(),
                "stage_messages": set(),
                "root_messages": set(),
                "first_seen": None,
                "last_seen": None,
            },
        )

    def observe(common: dict[str, object], row: Mapping[str, object], timestamp: object) -> None:
        for field, target in (("scope", "scopes"), ("case_id", "case_ids")):
            value = row.get(field)
            collection = common[target]
            if isinstance(collection, set) and isinstance(value, str) and value:
                collection.add(value)
        if isinstance(timestamp, str) and timestamp:
            first = common.get("first_seen")
            last = common.get("last_seen")
            common["first_seen"] = (
                timestamp if not isinstance(first, str) else min(first, timestamp)
            )
            common["last_seen"] = timestamp if not isinstance(last, str) else max(last, timestamp)

    for row in stage_failures:
        common = group(row)
        common["failed_stage_attempts"] = int(common["failed_stage_attempts"]) + 1
        common["retryable"] = bool(common["retryable"] or row.get("retryable") is True)
        message = row.get("stage_error_message")
        if isinstance(message, str) and message:
            cast_messages = common["stage_messages"]
            assert isinstance(cast_messages, set)
            cast_messages.add(message)
        root_message = row.get("root_error_message")
        if isinstance(root_message, str) and root_message:
            cast_roots = common["root_messages"]
            assert isinstance(cast_roots, set)
            cast_roots.add(root_message)
        observe(common, row, row.get("failed_at"))

    for row in call_failures:
        common = group(row)
        common["failed_model_calls"] = int(common["failed_model_calls"]) + 1
        if row.get("recovered_by_retry") is True:
            common["recovered_model_calls"] = int(common["recovered_model_calls"]) + 1
        common["retryable"] = bool(common["retryable"] or row.get("retryable") is True)
        root_message = row.get("root_error_message") or row.get("error_message")
        if isinstance(root_message, str) and root_message:
            cast_roots = common["root_messages"]
            assert isinstance(cast_roots, set)
            cast_roots.add(root_message)
        observe(common, row, row.get("recorded_at"))

    rows: list[Mapping[str, object]] = []
    for common in grouped.values():
        failed_calls = int(common["failed_model_calls"])
        recovered_calls = int(common["recovered_model_calls"])
        stage_count = int(common["failed_stage_attempts"])
        recovery = (
            "unrecovered"
            if stage_count
            else "recovered"
            if failed_calls and recovered_calls == failed_calls
            else "partially_recovered"
            if recovered_calls
            else "unknown"
        )
        rows.append(
            {
                **{
                    key: value
                    for key, value in common.items()
                    if key
                    not in {
                        "scopes",
                        "case_ids",
                        "stage_messages",
                        "root_messages",
                    }
                },
                "unrecovered_model_calls": failed_calls - recovered_calls,
                "recovery_status": recovery,
                "scope_count": len(common["scopes"]),
                "scopes": tuple(sorted(common["scopes"])),
                "affected_case_count": len(common["case_ids"]),
                "case_ids": tuple(sorted(common["case_ids"])),
                "stage_error_examples": tuple(sorted(common["stage_messages"]))[:3],
                "root_error_examples": tuple(sorted(common["root_messages"]))[:3],
            }
        )
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                -_int_or(item.get("failed_stage_attempts"), 0),
                -_int_or(item.get("failed_model_calls"), 0),
                str(item.get("stage")),
                str(item.get("endpoint")),
            ),
        )
    )


def _effective_lifecycle_state(
    lifecycle: Sequence[RunLifecycleEvent], statuses: Sequence[StageStatus]
) -> RunLifecycleState:
    for event in reversed(lifecycle):
        if event.state is not RunLifecycleState.REPORT_PUBLISHED:
            return event.state
    if any(item.state in {StageState.FAILED, StageState.REJECTED} for item in statuses):
        return RunLifecycleState.FAILED
    if any(item.state is StageState.PERSISTED for item in statuses):
        return RunLifecycleState.RUNNING
    return RunLifecycleState.CREATED


def _terminal_time_ms(
    lifecycle: Sequence[RunLifecycleEvent], status: RunLifecycleState
) -> int | None:
    if status not in {
        RunLifecycleState.COMPLETED,
        RunLifecycleState.FAILED,
        RunLifecycleState.CANCELLED,
    }:
        return None
    for event in reversed(lifecycle):
        if event.state is status:
            return _timestamp_ms(event.at)
    return None


def _topological_scopes(scopes: Sequence[ScopeDescriptor]) -> tuple[ScopeDescriptor, ...]:
    by_id = {item.id: item for item in scopes}
    result: list[ScopeDescriptor] = []
    pending = set(by_id)
    while pending:
        ready = sorted(
            scope_id
            for scope_id in pending
            if by_id[scope_id].parent_id is None or by_id[scope_id].parent_id not in pending
        )
        if not ready:
            raise IntegrityError("recording scope hierarchy contains a cycle")
        for scope_id in ready:
            result.append(by_id[scope_id])
            pending.remove(scope_id)
    return tuple(result)


def _topological_traces(traces: Sequence[TraceDescriptor]) -> tuple[TraceDescriptor, ...]:
    packet_to_trace = {str(item.attributes["agent_eval.packet_id"]): item for item in traces}
    result: list[TraceDescriptor] = []
    pending = set(packet_to_trace)
    while pending:
        ready = sorted(
            packet_id
            for packet_id in pending
            if all(
                link.get("packet_id") not in pending
                for link in packet_to_trace[packet_id].links
                if link.get("type") == "dependency"
            )
        )
        if not ready:
            raise IntegrityError("recording trace dependency graph contains a cycle")
        for packet_id in ready:
            result.append(packet_to_trace[packet_id])
            pending.remove(packet_id)
    return tuple(result)


def _metric(key: str, value: float, step: int, timestamp_ms: int) -> MetricPoint:
    identity = {
        "key": key,
        "value": value,
        "step": step,
        "timestamp_ms": timestamp_ms,
        "scope_id": None,
    }
    return MetricPoint(content_hash(identity), key, value, step, timestamp_ms)


def _table(name: str, rows: Sequence[Mapping[str, object]]) -> TableArtifact:
    normalized = tuple(dict(row) for row in rows)
    # Version 2 distinguishes MLflow tables produced after fixing the artifact
    # path collision that overwrote MLflow's split-orient table payload with a
    # recorder-neutral {"rows": ...} document. A new id causes an existing run
    # to publish a repaired table on its next recorder sync.
    return TableArtifact(
        content_hash({"schema_version": 2, "name": name, "rows": normalized}),
        name,
        normalized,
    )


def _artifact_pointer(value: StageArtifact | ArtifactReference) -> Mapping[str, object]:
    result: dict[str, object] = {
        "content_id": value.content_id,
        "media_type": value.media_type,
    }
    if isinstance(value, StageArtifact):
        result["id"] = value.id
    else:
        result["size_bytes"] = value.size_bytes
    return result


def _report_cases(report: Mapping[str, object] | None) -> tuple[Mapping[str, object], ...]:
    if report is None:
        return ()
    raw = report.get("cases", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _comparison_rows(value: object, field: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Mapping):
        return ()
    raw = value.get(field, [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    return tuple(dict(item) for item in raw if isinstance(item, Mapping))


def _expected_response_token_rows(
    report: Mapping[str, object] | None,
) -> tuple[Mapping[str, object], ...]:
    if report is None:
        return ()
    measurement = report.get("expected_response_token_measurements")
    if not isinstance(measurement, Mapping):
        return ()
    raw = measurement.get("cases", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    provenance = {
        key: measurement.get(key)
        for key in (
            "status",
            "measurement",
            "model",
            "deployment_revision",
            "executor_id",
            "endpoint_id",
            "endpoint_configuration_hash",
            "tokenizer_fingerprint",
            "add_special_tokens",
            "chat_template_applied",
        )
    }
    return tuple({**dict(item), **provenance} for item in raw if isinstance(item, Mapping))


def _comparison_arm_row(report: Mapping[str, object]) -> Mapping[str, object]:
    identity = _mapping(report.get("identity", {}), "report identity")
    models = _mapping(report.get("models", {}), "report models")
    solver = _mapping(models.get("solver", {}), "solver model")
    judge = _mapping(models.get("judge", {}), "judge model")
    raw_optimizer = models.get("optimizer")
    optimizer = (
        _mapping(raw_optimizer, "optimizer model") if isinstance(raw_optimizer, Mapping) else {}
    )
    metrics = report.get("benchmark_metrics")
    metrics = _mapping(metrics, "benchmark metrics") if isinstance(metrics, Mapping) else {}
    comparison = report.get("test_comparison")
    comparison = _mapping(comparison, "test comparison") if isinstance(comparison, Mapping) else {}
    optimization = report.get("optimization")
    optimization = (
        _mapping(optimization, "optimization") if isinstance(optimization, Mapping) else {}
    )
    role_usage = report.get("usage_by_role")
    role_usage = _mapping(role_usage, "usage by role") if isinstance(role_usage, Mapping) else {}
    total_tokens = 0
    total_latency = 0.0
    for raw_usage in role_usage.values():
        if not isinstance(raw_usage, Mapping):
            continue
        total_tokens += _int_or(raw_usage.get("tokens"), 0)
        total_latency += _number_or(raw_usage.get("latency_seconds"), 0.0)
    endpoint_execution = report.get("endpoint_execution")
    endpoint_execution = (
        _mapping(endpoint_execution, "endpoint execution")
        if isinstance(endpoint_execution, Mapping)
        else {}
    )
    endpoint_rows = endpoint_execution.get("endpoints", [])
    endpoint_requests = (
        sum(_int_or(item.get("requests"), 0) for item in endpoint_rows if isinstance(item, Mapping))
        if isinstance(endpoint_rows, Sequence) and not isinstance(endpoint_rows, (str, bytes))
        else 0
    )
    score = comparison.get("optimized_mean_score", metrics.get("mean_score"))
    baseline_score = comparison.get("baseline_mean_score", score)
    delta = comparison.get("micro_case_delta")
    strategy = optimization.get("strategy") if optimization else "baseline"
    solver_label = _model_label(solver)
    optimizer_label = _model_label(optimizer) if optimizer else None
    label = (
        f"{solver_label} · {strategy} · {optimizer_label}"
        if optimizer_label is not None
        else f"{solver_label} · baseline"
    )
    return {
        "run_id": report.get("run_id"),
        "experiment_id": report.get("experiment_id"),
        "arm_id": identity.get("arm_id"),
        "benchmark_revision_id": identity.get("benchmark_revision_id"),
        "comparison_group_id": identity.get("comparison_group_id"),
        "comparison_id": identity.get("comparison_id", "all-compatible-arms"),
        "report_hash": report.get("content_hash") or content_hash(report),
        "label": label,
        "strategy": strategy,
        "solver_profile": solver.get("profile_id"),
        "solver_deployment": solver.get("deployment_id"),
        "solver_model": solver.get("configured_name"),
        "solver_revision": solver.get("deployment_revision"),
        "optimizer_profile": optimizer.get("profile_id"),
        "optimizer_deployment": optimizer.get("deployment_id"),
        "optimizer_model": optimizer.get("configured_name"),
        "optimizer_revision": optimizer.get("deployment_revision"),
        "judge_profile": judge.get("profile_id"),
        "judge_deployment": judge.get("deployment_id"),
        "judge_model": judge.get("configured_name"),
        "judge_revision": judge.get("deployment_revision"),
        "score": score,
        "paired_baseline_score": baseline_score,
        "optimization_delta": delta,
        "macro_skill_delta": comparison.get("macro_skill_delta"),
        "case_count": report.get("case_count"),
        "total_tokens": total_tokens,
        "total_latency_seconds": total_latency,
        "endpoint_requests": endpoint_requests,
    }


def _comparison_skill_rows(
    report: Mapping[str, object], arm: Mapping[str, object]
) -> tuple[Mapping[str, object], ...]:
    comparison = report.get("test_comparison")
    paired = _comparison_rows(comparison, "skills")
    if paired:
        return tuple(
            {
                "run_id": arm.get("run_id"),
                "arm_id": arm.get("arm_id"),
                "label": arm.get("label"),
                "solver_profile": arm.get("solver_profile"),
                "optimizer_profile": arm.get("optimizer_profile"),
                "strategy": arm.get("strategy"),
                "skill_name": item.get("skill_name"),
                "score": item.get("optimized_mean_score"),
                "paired_baseline_score": item.get("baseline_mean_score"),
                "optimization_delta": item.get("delta"),
                "case_count": item.get("case_count"),
            }
            for item in paired
        )
    raw = report.get("skills", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(
        {
            "run_id": arm.get("run_id"),
            "arm_id": arm.get("arm_id"),
            "label": arm.get("label"),
            "solver_profile": arm.get("solver_profile"),
            "optimizer_profile": None,
            "strategy": "baseline",
            "skill_name": item.get("skill_name"),
            "score": item.get("mean_score"),
            "paired_baseline_score": item.get("mean_score"),
            "optimization_delta": None,
            "case_count": item.get("case_count"),
        }
        for item in raw
        if isinstance(item, Mapping)
    )


def _comparison_token_band_rows(
    report: Mapping[str, object], arm: Mapping[str, object]
) -> tuple[Mapping[str, object], ...]:
    paired = _comparison_rows(report.get("test_comparison"), "length_bands")
    if paired:
        return tuple(
            {
                "run_id": arm.get("run_id"),
                "arm_id": arm.get("arm_id"),
                "label": arm.get("label"),
                "solver_profile": arm.get("solver_profile"),
                "optimizer_profile": arm.get("optimizer_profile"),
                "strategy": arm.get("strategy"),
                "expected_response_length_band": item.get("expected_response_length_band"),
                "score": item.get("optimized_mean_score"),
                "paired_baseline_score": item.get("baseline_mean_score"),
                "optimization_delta": item.get("delta"),
                "case_count": item.get("case_count"),
            }
            for item in paired
        )
    grouped: dict[str, list[float]] = defaultdict(list)
    for item in _report_cases(report):
        band = item.get("expected_response_length_band")
        score = item.get("score")
        if (
            isinstance(band, str)
            and isinstance(score, (int, float))
            and not isinstance(score, bool)
        ):
            grouped[band].append(float(score))
    return tuple(
        {
            "run_id": arm.get("run_id"),
            "arm_id": arm.get("arm_id"),
            "label": arm.get("label"),
            "solver_profile": arm.get("solver_profile"),
            "optimizer_profile": None,
            "strategy": "baseline",
            "expected_response_length_band": band,
            "score": statistics.fmean(values),
            "paired_baseline_score": statistics.fmean(values),
            "optimization_delta": None,
            "case_count": len(values),
        }
        for band, values in sorted(grouped.items())
    )


def _model_label(model: Mapping[str, object]) -> str:
    return str(model.get("profile_id") or model.get("configured_name") or "unknown-model")


def _nested_mapping_value(item: Mapping[str, object], field: str, key: str) -> object:
    value = item.get(field)
    return value.get(key) if isinstance(value, Mapping) else None


def _aggregate_result_id(report: Mapping[str, object]) -> object:
    stages = report.get("stages", ())
    if not isinstance(stages, Sequence) or isinstance(stages, (str, bytes, bytearray)):
        return None
    for stage in stages:
        if isinstance(stage, Mapping) and stage.get("stage") == "aggregate":
            return stage.get("result_id")
    return None


def _report_order_key(report: Mapping[str, object]) -> tuple[datetime, str]:
    try:
        created = _parse_timestamp(report.get("created_at"))
    except (TypeError, ValueError):
        created = datetime.min.replace(tzinfo=UTC)
    return created, str(report.get("run_id", ""))


def _named_values(values: object, name_key: str, value_key: str) -> tuple[tuple[str, float], ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return ()
    result = []
    for item in values:
        if not isinstance(item, Mapping) or not isinstance(item.get(name_key), str):
            continue
        value = item.get(value_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result.append((str(item[name_key]), float(value)))
    return tuple(sorted(result))


_STAGE_ORDER = {
    stage: index
    for index, stage in enumerate(
        (
            "author",
            "validate_judge",
            "optimize",
            "fit",
            "solve",
            "judge",
            "select",
            "aggregate",
        )
    )
}


_CHART_DEFINITIONS: Mapping[str, Mapping[str, object]] = {
    "execution-dag.svg": {
        "title": "Execution dependency overview",
        "question": "How did work move through the experiment's execution scopes?",
        "description": (
            "A scope-collapsed view of the canonical execution DAG. Green means every attempt "
            "persisted, orange means unfinished work remains, and red means at least one attempt "
            "failed or was rejected. Solid gray arrows are dependencies; dashed purple arrows "
            "are retries."
        ),
        "source": "execution-dag.json",
        "unit": "stage attempts",
    },
    "scores-by-skill.svg": {
        "title": "Mean score by skill",
        "question": "Which evaluated skills received stronger or weaker judge scores?",
        "description": "Mean final judge score for each skill in the report population.",
        "source": "case-results",
        "unit": "judge score from 0 to 1",
    },
    "criterion-heatmap.svg": {
        "title": "Criterion heatmap",
        "question": "Which rubric criteria are strong or weak within each skill?",
        "description": "Mean weighted score for each observed skill and rubric criterion.",
        "source": "criterion-results",
        "unit": "judge score from 0 to 1",
    },
    "stage-latency-summary.svg": {
        "title": "Executed stage latency",
        "question": "How long did executed stage attempts take?",
        "description": (
            "Median, 95th percentile, and maximum wall-clock duration by stage. Exact cache "
            "reuses are excluded because they do not represent new execution latency."
        ),
        "source": "stage-attempts",
        "unit": "seconds",
    },
    "token-usage-by-stage.svg": {
        "title": "Model token usage by stage",
        "question": "Where did model input and output tokens get consumed?",
        "description": (
            "Input, output, and provider-reported unclassified tokens summed from completed model "
            "calls and grouped by stage."
        ),
        "source": "model-calls",
        "unit": "tokens",
    },
    "endpoint-workload.svg": {
        "title": "Endpoint workload distribution",
        "question": "How many provider requests were routed to each endpoint?",
        "description": (
            "Absolute routed request counts by endpoint. This measures workload distribution, "
            "not time-based endpoint utilization."
        ),
        "source": "endpoint-usage",
        "unit": "requests",
    },
    "endpoint-saturation.svg": {
        "title": "Peak endpoint saturation",
        "question": "How much of each endpoint's configured concurrency was observed in use?",
        "description": (
            "Peak observed in-flight requests divided by configured concurrency. It is a peak, "
            "not average utilization over time."
        ),
        "source": "endpoint-usage",
        "unit": "percent of configured concurrency",
    },
    "cache-reuse-by-stage.svg": {
        "title": "Stage-result cache reuse",
        "question": "Which stages executed new work and which reused an exact prior result?",
        "description": (
            "Executed and exact-reuse result counts within each stage. Every persisted stage "
            "result has equal weight; this does not directly measure time or tokens saved."
        ),
        "source": "stage-attempts",
        "unit": "stage results",
    },
    "optimization-validation.svg": {
        "title": "Optimization validation",
        "question": "How did candidate programs score during optimization?",
        "description": "Mean validation judge score for each evaluated optimizer candidate.",
        "source": "optimization-candidates",
        "unit": "judge score from 0 to 1",
    },
    "copro-overall-paired-score.svg": {
        "title": "COPRO paired test score",
        "question": "Did the selected COPRO program improve the untouched test mean?",
        "description": "Original and optimized mean judge score on the same untouched test cases.",
        "source": "copro-case-comparison",
        "unit": "judge score from 0 to 1",
    },
    "copro-skill-lift.svg": {
        "title": "COPRO score by skill",
        "question": "Which skills improved or regressed after prompt optimization?",
        "description": "Paired original and optimized skill means ordered by signed score delta.",
        "source": "copro-skill-comparison",
        "unit": "judge score from 0 to 1",
    },
    "copro-case-paired-score.svg": {
        "title": "COPRO baseline versus optimized by test case",
        "question": "Which untouched test cases improved or regressed after optimization?",
        "description": (
            "Original and optimized judge score for every paired test case and solver "
            "repetition, ordered by signed optimization delta with the strongest gains first."
        ),
        "source": "copro-case-comparison",
        "unit": "judge score from 0 to 1",
    },
    "copro-case-delta-distribution.svg": {
        "title": "COPRO case-delta distribution",
        "question": "How broadly are gains and regressions distributed across test cases?",
        "description": "Histogram of optimized minus original score for paired untouched cases.",
        "source": "copro-case-comparison",
        "unit": "judge-score delta",
    },
    "copro-delta-by-expected-tokens.svg": {
        "title": "COPRO delta by expected-response tokens",
        "question": "Is optimization lift related to expected answer length?",
        "description": (
            "Paired score delta against the exact hidden expected-response token count. Token "
            "length is not a semantic difficulty label."
        ),
        "source": "expected-response-token-counts and copro-case-comparison",
        "unit": "tokens and judge-score delta",
    },
    "copro-score-by-token-band.svg": {
        "title": "COPRO score by expected-response token band",
        "question": "How do original and optimized scores compare across answer-length bands?",
        "description": "Paired score means grouped by exact expected-response token bands.",
        "source": "copro-token-band-comparison",
        "unit": "judge score from 0 to 1",
    },
    "copro-criterion-lift.svg": {
        "title": "COPRO score by rubric criterion",
        "question": "Which rubric dimensions improved or regressed after optimization?",
        "description": "Paired criterion means ordered by optimized-minus-original score delta.",
        "source": "copro-criterion-comparison",
        "unit": "judge score from 0 to 1",
    },
    "model-baseline-score.svg": {
        "title": "Baseline score by solver model",
        "question": "How do solver models perform before prompt optimization?",
        "description": (
            "Mean judge score for each completed baseline arm in this exact benchmark revision "
            "and fixed-judge comparison group."
        ),
        "source": "model-arm-comparison",
        "unit": "judge score from 0 to 1",
    },
    "model-optimized-score.svg": {
        "title": "Optimized score by solver and optimizer",
        "question": "What absolute score does each solver and compiled optimizer program reach?",
        "description": (
            "Untouched-test mean for every completed optimization arm, labeled by solver and "
            "optimizer profile. Absolute quality belongs to the solver/program pair."
        ),
        "source": "model-arm-comparison",
        "unit": "judge score from 0 to 1",
    },
    "optimizer-lift.svg": {
        "title": "Paired optimizer lift",
        "question": "Which optimizer arms improve or regress their own paired baseline?",
        "description": (
            "Optimized minus baseline score on identical untouched cases, ordered by signed "
            "delta. This measures optimizer effectiveness rather than raw solver quality."
        ),
        "source": "model-arm-comparison",
        "unit": "judge-score delta",
    },
    "model-skill-heatmap.svg": {
        "title": "Model performance by skill",
        "question": "Which model arms are strong or weak for each skill?",
        "description": (
            "Mean judge score by arm and skill within one immutable comparison group. Missing "
            "cells are not silently imputed."
        ),
        "source": "model-skill-comparison",
        "unit": "judge score from 0 to 1",
    },
    "model-token-band-score.svg": {
        "title": "Model performance by expected-response token band",
        "question": "How does model quality vary with concrete expected answer length?",
        "description": (
            "Mean judge score grouped by exact expected-response token band. Token length is a "
            "measurement, not a semantic difficulty label."
        ),
        "source": "model-token-band-comparison",
        "unit": "judge score from 0 to 1",
    },
    "model-quality-vs-latency.svg": {
        "title": "Quality versus model-call latency",
        "question": "Which arms offer a stronger score for their total recorded model latency?",
        "description": (
            "Arm score against summed model-call latency. It is an operational Pareto view and "
            "does not alter the quality metric."
        ),
        "source": "model-arm-comparison",
        "unit": "seconds and judge score",
    },
    "model-quality-vs-tokens.svg": {
        "title": "Quality versus generated token use",
        "question": "Which arms offer a stronger score for their recorded token use?",
        "description": (
            "Arm score against total provider-reported tokens. It exposes efficiency tradeoffs "
            "without combining token use into the judge score."
        ),
        "source": "model-arm-comparison",
        "unit": "tokens and judge score",
    },
}

_COMPARISON_CHART_NAMES = frozenset(
    {
        "model-baseline-score.svg",
        "model-optimized-score.svg",
        "optimizer-lift.svg",
        "model-skill-heatmap.svg",
        "model-token-band-score.svg",
        "model-quality-vs-latency.svg",
        "model-quality-vs-tokens.svg",
    }
)


def _interactive_plotly_charts(
    report: Mapping[str, object] | None,
    statuses: Sequence[StageStatus],
    model_calls: Sequence[Mapping[str, object]],
    endpoint_rows: Sequence[Mapping[str, object]],
    *,
    generated_static_paths: frozenset[str],
) -> Mapping[str, tuple[bytes, str]]:
    """Render recorder-neutral Plotly HTML for every generated analytical chart."""

    artifacts: dict[str, tuple[bytes, str]] = {}

    def add(static_name: str, figure: go.Figure, *, height: int = 560) -> None:
        static_path = f"graphs/{static_name}"
        if static_path not in generated_static_paths:
            return
        interactive_name = static_name.removesuffix(".svg") + ".html"
        artifacts[f"graphs/{interactive_name}"] = (
            _plotly_html(static_name, figure, height=height),
            "text/html",
        )

    skills = report.get("skills") if report is not None else None
    skill_values = _named_values(skills, "skill_name", "mean_score")
    if skill_values:
        ordered = sorted(skill_values, key=lambda item: (item[1], item[0]))
        add(
            "scores-by-skill.svg",
            go.Figure(
                go.Bar(
                    x=[value for _, value in ordered],
                    y=[label for label, _ in ordered],
                    orientation="h",
                    marker_color="#2563eb",
                    text=[f"{value:.3f}" for _, value in ordered],
                    textposition="outside",
                    hovertemplate="%{y}<br>mean score=%{x:.4f}<extra></extra>",
                )
            ),
            height=max(520, 30 * len(ordered) + 180),
        )

    criteria = report.get("criteria") if report is not None else None
    criterion_rows = (
        tuple(item for item in criteria if isinstance(item, Mapping))
        if isinstance(criteria, Sequence) and not isinstance(criteria, (str, bytes))
        else ()
    )
    if criterion_rows:
        criterion_names = sorted({str(item.get("criterion_id")) for item in criterion_rows})
        skill_names = sorted({str(item.get("skill_name")) for item in criterion_rows})
        lookup = {
            (str(item.get("skill_name")), str(item.get("criterion_id"))): _number_or(
                item.get("mean_score"), 0.0
            )
            for item in criterion_rows
        }
        add(
            "criterion-heatmap.svg",
            go.Figure(
                go.Heatmap(
                    x=criterion_names,
                    y=skill_names,
                    z=[
                        [lookup.get((skill, criterion), 0.0) for criterion in criterion_names]
                        for skill in skill_names
                    ],
                    zmin=0,
                    zmax=1,
                    colorscale="RdYlGn",
                    colorbar={"title": {"text": "Score"}},
                    hovertemplate=(
                        "skill=%{y}<br>criterion=%{x}<br>mean score=%{z:.4f}<extra></extra>"
                    ),
                )
            ),
            height=max(560, 30 * len(skill_names) + 220),
        )

    latency_rows = _latency_summary_rows(statuses)
    if latency_rows:
        latency = go.Figure()
        for key, label, color in (
            ("p50_seconds", "p50", "#0f172a"),
            ("p95_seconds", "p95", "#2563eb"),
            ("maximum_seconds", "maximum", "#94a3b8"),
        ):
            latency.add_bar(
                name=label,
                x=[_number_or(item.get(key), 0.0) for item in latency_rows],
                y=[str(item.get("stage")) for item in latency_rows],
                orientation="h",
                marker_color=color,
                customdata=[
                    [_int_or(item.get("count"), 0), _int_or(item.get("failed"), 0)]
                    for item in latency_rows
                ],
                hovertemplate=(
                    "%{y}<br>" + label + "=%{x:.3f}s<br>attempts=%{customdata[0]}"
                    "<br>failed=%{customdata[1]}<extra></extra>"
                ),
            )
        latency.update_layout(barmode="group")
        add("stage-latency-summary.svg", latency)

    token_rows = _token_usage_rows(model_calls)
    if token_rows:
        tokens = go.Figure()
        for key, label, color in (
            ("input_tokens", "Input", "#2563eb"),
            ("output_tokens", "Output", "#f59e0b"),
            ("unclassified_tokens", "Unclassified", "#94a3b8"),
        ):
            tokens.add_bar(
                name=label,
                x=[str(item.get("stage")) for item in token_rows],
                y=[_int_or(item.get(key), 0) for item in token_rows],
                marker_color=color,
                customdata=[_int_or(item.get("calls"), 0) for item in token_rows],
                hovertemplate=(
                    "%{x}<br>" + label.lower() + " tokens=%{y:,}<br>calls=%{customdata}"
                    "<extra></extra>"
                ),
            )
        tokens.update_layout(barmode="stack")
        add("token-usage-by-stage.svg", tokens)

    if endpoint_rows:
        add(
            "endpoint-workload.svg",
            go.Figure(
                go.Bar(
                    x=[str(item.get("endpoint_id")) for item in endpoint_rows],
                    y=[_int_or(item.get("requests"), 0) for item in endpoint_rows],
                    marker_color="#2563eb",
                    customdata=[
                        [
                            _int_or(item.get("failed_requests"), 0),
                            _number_or(item.get("queue_wait_seconds"), 0.0),
                        ]
                        for item in endpoint_rows
                    ],
                    hovertemplate=(
                        "%{x}<br>requests=%{y:,}<br>failed=%{customdata[0]:,}"
                        "<br>queued=%{customdata[1]:.3f}s<extra></extra>"
                    ),
                )
            ),
        )

    saturation_rows = tuple(
        item
        for item in endpoint_rows
        if _int_or(item.get("configured_capacity"), 0) > 0
        and _int_or(item.get("peak_observed_concurrency"), 0) > 0
    )
    if saturation_rows:
        add(
            "endpoint-saturation.svg",
            go.Figure(
                go.Bar(
                    x=[str(item.get("endpoint_id")) for item in saturation_rows],
                    y=[
                        _int_or(item.get("peak_observed_concurrency"), 0)
                        / _int_or(item.get("configured_capacity"), 1)
                        for item in saturation_rows
                    ],
                    marker_color="#2563eb",
                    customdata=[
                        [
                            _int_or(item.get("peak_observed_concurrency"), 0),
                            _int_or(item.get("configured_capacity"), 0),
                        ]
                        for item in saturation_rows
                    ],
                    hovertemplate=(
                        "%{x}<br>peak saturation=%{y:.1%}<br>peak in flight=%{customdata[0]}"
                        "<br>configured capacity=%{customdata[1]}<extra></extra>"
                    ),
                )
            ),
        )

    cache_rows = _cache_reuse_rows(statuses)
    if cache_rows:
        cache = go.Figure()
        for key, label, color in (
            ("executed", "Executed", "#2563eb"),
            ("reused", "Exact reuse", "#16a34a"),
        ):
            cache.add_bar(
                name=label,
                x=[str(item.get("stage")) for item in cache_rows],
                y=[_int_or(item.get(key), 0) for item in cache_rows],
                marker_color=color,
                hovertemplate="%{x}<br>" + label.lower() + "=%{y:,}<extra></extra>",
            )
        cache.update_layout(barmode="stack")
        add("cache-reuse-by-stage.svg", cache)

    validation = report.get("validation") if report is not None else None
    raw_candidates = validation.get("candidates", []) if isinstance(validation, Mapping) else ()
    candidates = (
        tuple(item for item in raw_candidates if isinstance(item, Mapping))
        if isinstance(raw_candidates, Sequence) and not isinstance(raw_candidates, (str, bytes))
        else ()
    )
    candidate_values = _named_values(candidates, "candidate_id", "mean_score")
    if candidate_values:
        ordered = sorted(candidate_values, key=lambda item: (item[1], item[0]))
        add(
            "optimization-validation.svg",
            go.Figure(
                go.Bar(
                    x=[value for _, value in ordered],
                    y=[label for label, _ in ordered],
                    orientation="h",
                    marker_color="#7c3aed",
                    hovertemplate="%{y}<br>validation mean=%{x:.4f}<extra></extra>",
                )
            ),
            height=max(520, 28 * len(ordered) + 180),
        )

    comparison = report.get("test_comparison") if report is not None else None
    if isinstance(comparison, Mapping):
        add(
            "copro-overall-paired-score.svg",
            _paired_plotly_figure(
                (
                    (
                        "Untouched test set",
                        _number_or(comparison.get("baseline_mean_score"), 0.0),
                        _number_or(comparison.get("optimized_mean_score"), 0.0),
                    ),
                )
            ),
        )
        skill_rows = sorted(
            _comparison_rows(comparison, "skills"),
            key=lambda item: (_number_or(item.get("delta"), 0.0), str(item.get("skill_name"))),
        )
        if skill_rows:
            add(
                "copro-skill-lift.svg",
                _paired_plotly_figure(
                    tuple(
                        (
                            str(item.get("skill_name")),
                            _number_or(item.get("baseline_mean_score"), 0.0),
                            _number_or(item.get("optimized_mean_score"), 0.0),
                        )
                        for item in skill_rows
                    )
                ),
                height=max(560, 34 * len(skill_rows) + 190),
            )
        case_rows = _comparison_rows(comparison, "cases")
        if case_rows:
            ordered_cases = sorted(
                case_rows,
                key=lambda item: (
                    _number_or(item.get("delta"), 0.0),
                    str(item.get("case_id")),
                    _int_or(item.get("repetition"), 0),
                ),
            )
            add(
                "copro-case-paired-score.svg",
                _paired_plotly_figure(
                    tuple(
                        (
                            f"{item.get('case_id')} · repetition {item.get('repetition')}",
                            _number_or(item.get("baseline_score"), 0.0),
                            _number_or(item.get("optimized_score"), 0.0),
                        )
                        for item in ordered_cases
                    )
                ),
                height=max(620, 30 * len(ordered_cases) + 200),
            )
            delta_values = [_number_or(item.get("delta"), 0.0) for item in case_rows]
            histogram = go.Figure(
                go.Histogram(
                    x=delta_values,
                    nbinsx=20,
                    marker_color="#7c3aed",
                    hovertemplate="delta band=%{x}<br>cases=%{y}<extra></extra>",
                )
            )
            histogram.add_vline(x=0, line_dash="dash", line_color="#64748b")
            add("copro-case-delta-distribution.svg", histogram)
        measured_cases = tuple(
            item
            for item in case_rows
            if isinstance(item.get("expected_response_tokens"), int)
            and not isinstance(item.get("expected_response_tokens"), bool)
        )
        if measured_cases:
            add(
                "copro-delta-by-expected-tokens.svg",
                go.Figure(
                    go.Scatter(
                        x=[int(item["expected_response_tokens"]) for item in measured_cases],
                        y=[_number_or(item.get("delta"), 0.0) for item in measured_cases],
                        mode="markers",
                        marker={
                            "color": [
                                _number_or(item.get("delta"), 0.0) for item in measured_cases
                            ],
                            "colorscale": "RdYlGn",
                            "cmin": -1,
                            "cmax": 1,
                            "showscale": True,
                            "colorbar": {"title": {"text": "Delta"}},
                        },
                        text=[str(item.get("case_id")) for item in measured_cases],
                        hovertemplate=(
                            "%{text}<br>expected tokens=%{x:,}<br>score delta=%{y:+.4f}"
                            "<extra></extra>"
                        ),
                    )
                ),
            )
        band_rows = _comparison_rows(comparison, "length_bands")
        if band_rows:
            add(
                "copro-score-by-token-band.svg",
                _paired_plotly_figure(
                    tuple(
                        (
                            str(item.get("expected_response_length_band")),
                            _number_or(item.get("baseline_mean_score"), 0.0),
                            _number_or(item.get("optimized_mean_score"), 0.0),
                        )
                        for item in band_rows
                    )
                ),
            )
        comparison_criteria = sorted(
            _comparison_rows(comparison, "criteria"),
            key=lambda item: (_number_or(item.get("delta"), 0.0), str(item.get("criterion_id"))),
        )
        if comparison_criteria:
            add(
                "copro-criterion-lift.svg",
                _paired_plotly_figure(
                    tuple(
                        (
                            str(item.get("criterion_id")),
                            _number_or(item.get("baseline_mean_score"), 0.0),
                            _number_or(item.get("optimized_mean_score"), 0.0),
                        )
                        for item in comparison_criteria
                    )
                ),
                height=max(560, 34 * len(comparison_criteria) + 190),
            )
    return artifacts


def _comparison_chart_artifacts(
    arm_rows: Sequence[Mapping[str, object]],
    skill_rows: Sequence[Mapping[str, object]],
    token_band_rows: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, tuple[bytes, str]], Mapping[str, object]]:
    """Create paired static and interactive charts for one comparison group."""

    artifacts: dict[str, tuple[bytes, str]] = {}
    generated: list[Mapping[str, object]] = []

    def add(name: str, svg: bytes, figure: go.Figure, *, height: int = 560) -> None:
        artifacts[f"comparison/graphs/{name}"] = (svg, "image/svg+xml")
        html_name = name.removesuffix(".svg") + ".html"
        artifacts[f"comparison/graphs/{html_name}"] = (
            _plotly_html(name, figure, height=height),
            "text/html",
        )
        generated.append(
            {
                "artifact": f"comparison/graphs/{html_name}",
                "static_artifact": f"comparison/graphs/{name}",
                "interactive_artifact": f"comparison/graphs/{html_name}",
                **dict(_CHART_DEFINITIONS[name]),
            }
        )

    baseline = tuple(
        row
        for row in arm_rows
        if row.get("optimizer_profile") is None and isinstance(row.get("score"), (int, float))
    )
    if baseline:
        ordered = sorted(
            ((str(row.get("label")), float(row["score"])) for row in baseline),
            key=lambda item: (item[1], item[0]),
        )
        figure = go.Figure(
            go.Bar(
                x=[value for _, value in ordered],
                y=[label for label, _ in ordered],
                orientation="h",
                marker_color="#2563eb",
                hovertemplate="%{y}<br>mean score=%{x:.4f}<extra></extra>",
            )
        )
        add(
            "model-baseline-score.svg",
            _bar_svg(
                str(_CHART_DEFINITIONS["model-baseline-score.svg"]["title"]),
                ordered,
                maximum=1.0,
                description=_chart_description("model-baseline-score.svg"),
                unit="judge score",
            ),
            figure,
            height=max(500, 34 * len(ordered) + 180),
        )

    optimized = tuple(
        row
        for row in arm_rows
        if row.get("optimizer_profile") is not None and isinstance(row.get("score"), (int, float))
    )
    if optimized:
        ordered = sorted(
            ((str(row.get("label")), float(row["score"])) for row in optimized),
            key=lambda item: (item[1], item[0]),
        )
        figure = go.Figure(
            go.Bar(
                x=[value for _, value in ordered],
                y=[label for label, _ in ordered],
                orientation="h",
                marker_color="#7c3aed",
                hovertemplate="%{y}<br>optimized mean=%{x:.4f}<extra></extra>",
            )
        )
        add(
            "model-optimized-score.svg",
            _bar_svg(
                str(_CHART_DEFINITIONS["model-optimized-score.svg"]["title"]),
                ordered,
                maximum=1.0,
                description=_chart_description("model-optimized-score.svg"),
                unit="judge score",
            ),
            figure,
            height=max(500, 34 * len(ordered) + 180),
        )

        delta_values = sorted(
            (
                (str(row.get("label")), float(row["optimization_delta"]))
                for row in optimized
                if isinstance(row.get("optimization_delta"), (int, float))
            ),
            key=lambda item: (item[1], item[0]),
        )
        if delta_values:
            delta_figure = go.Figure(
                go.Bar(
                    x=[value for _, value in delta_values],
                    y=[label for label, _ in delta_values],
                    orientation="h",
                    marker_color=[
                        "#16a34a" if value > 0 else "#dc2626" if value < 0 else "#64748b"
                        for _, value in delta_values
                    ],
                    hovertemplate="%{y}<br>paired lift=%{x:+.4f}<extra></extra>",
                )
            )
            delta_figure.add_vline(x=0, line_dash="dash", line_color="#64748b")
            add(
                "optimizer-lift.svg",
                _signed_bar_svg(
                    str(_CHART_DEFINITIONS["optimizer-lift.svg"]["title"]),
                    delta_values,
                    description=_chart_description("optimizer-lift.svg"),
                ),
                delta_figure,
                height=max(500, 34 * len(delta_values) + 180),
            )

    if skill_rows:
        labels, columns, matrix, counts = _comparison_matrix(
            skill_rows,
            column_field="skill_name",
        )
        add(
            "model-skill-heatmap.svg",
            _matrix_svg(
                str(_CHART_DEFINITIONS["model-skill-heatmap.svg"]["title"]),
                labels,
                columns,
                matrix,
                description=_chart_description("model-skill-heatmap.svg"),
            ),
            go.Figure(
                go.Heatmap(
                    x=columns,
                    y=labels,
                    z=matrix,
                    customdata=counts,
                    zmin=0,
                    zmax=1,
                    colorscale="RdYlGn",
                    colorbar={"title": {"text": "Score"}},
                    hovertemplate=(
                        "arm=%{y}<br>skill=%{x}<br>mean score=%{z:.4f}"
                        "<br>cases=%{customdata}<extra></extra>"
                    ),
                )
            ),
            height=max(560, 38 * len(labels) + 220),
        )

    if token_band_rows:
        labels, columns, matrix, counts = _comparison_matrix(
            token_band_rows,
            column_field="expected_response_length_band",
        )
        add(
            "model-token-band-score.svg",
            _matrix_svg(
                str(_CHART_DEFINITIONS["model-token-band-score.svg"]["title"]),
                labels,
                columns,
                matrix,
                description=_chart_description("model-token-band-score.svg"),
            ),
            go.Figure(
                go.Heatmap(
                    x=columns,
                    y=labels,
                    z=matrix,
                    customdata=counts,
                    zmin=0,
                    zmax=1,
                    colorscale="RdYlGn",
                    colorbar={"title": {"text": "Score"}},
                    hovertemplate=(
                        "arm=%{y}<br>token band=%{x}<br>mean score=%{z:.4f}"
                        "<br>cases=%{customdata}<extra></extra>"
                    ),
                )
            ),
            height=max(560, 38 * len(labels) + 220),
        )

    for name, x_field, x_label in (
        ("model-quality-vs-latency.svg", "total_latency_seconds", "total latency (seconds)"),
        ("model-quality-vs-tokens.svg", "total_tokens", "total tokens"),
    ):
        values = tuple(
            row
            for row in arm_rows
            if isinstance(row.get("score"), (int, float))
            and isinstance(row.get(x_field), (int, float))
        )
        if not values:
            continue
        figure = go.Figure(
            go.Scatter(
                x=[float(row[x_field]) for row in values],
                y=[float(row["score"]) for row in values],
                text=[str(row.get("label")) for row in values],
                customdata=[row.get("case_count") for row in values],
                mode="markers+text",
                textposition="top center",
                marker={"size": 11, "color": "#2563eb"},
                hovertemplate=(
                    "%{text}<br>" + x_label + "=%{x:,.3f}<br>score=%{y:.4f}"
                    "<br>cases=%{customdata}<extra></extra>"
                ),
            )
        )
        add(
            name,
            _quality_scatter_svg(
                str(_CHART_DEFINITIONS[name]["title"]),
                tuple(
                    (str(row.get("label")), float(row[x_field]), float(row["score"]))
                    for row in values
                ),
                x_label=x_label,
                description=_chart_description(name),
            ),
            figure,
        )

    manifest = {
        "schema_version": 1,
        "purpose": (
            "Cross-arm model comparison charts. Every chart is restricted to one benchmark "
            "revision and one fixed-judge comparison group; raw rows are recorded as MLflow tables."
        ),
        "charts": generated,
    }
    return artifacts, manifest


def _comparison_matrix(
    rows: Sequence[Mapping[str, object]],
    *,
    column_field: str,
) -> tuple[list[str], list[str], list[list[float | None]], list[list[object]]]:
    labels = sorted({str(row.get("label")) for row in rows})
    columns = sorted({str(row.get(column_field)) for row in rows})
    lookup = {(str(row.get("label")), str(row.get(column_field))): row for row in rows}
    matrix: list[list[float | None]] = []
    counts: list[list[object]] = []
    for label in labels:
        matrix.append(
            [
                (
                    _number_or(lookup[(label, column)].get("score"), 0.0)
                    if (label, column) in lookup
                    else None
                )
                for column in columns
            ]
        )
        counts.append(
            [
                lookup[(label, column)].get("case_count") if (label, column) in lookup else None
                for column in columns
            ]
        )
    return labels, columns, matrix, counts


def _paired_plotly_figure(values: Sequence[tuple[str, float, float]]) -> go.Figure:
    labels = [label for label, _, _ in values]
    baseline = [original for _, original, _ in values]
    optimized = [candidate for _, _, candidate in values]
    deltas = [candidate - original for original, candidate in zip(baseline, optimized, strict=True)]
    figure = go.Figure()
    figure.add_bar(
        name="Original",
        x=baseline,
        y=labels,
        orientation="h",
        marker_color="#4C78A8",
        customdata=deltas,
        hovertemplate="%{y}<br>original=%{x:.4f}<br>delta=%{customdata:+.4f}<extra></extra>",
    )
    figure.add_bar(
        name="Optimized",
        x=optimized,
        y=labels,
        orientation="h",
        marker_color="#F58518",
        customdata=deltas,
        hovertemplate="%{y}<br>optimized=%{x:.4f}<br>delta=%{customdata:+.4f}<extra></extra>",
    )
    figure.update_layout(barmode="group")
    return figure


def _plotly_html(static_name: str, figure: go.Figure, *, height: int) -> bytes:
    definition = _CHART_DEFINITIONS[static_name]
    title = html.escape(str(definition["title"]))
    description = html.escape(str(definition["description"]))
    unit = str(definition["unit"])
    figure.update_layout(
        template="plotly_white",
        height=height,
        autosize=True,
        title={"text": f"{title}<br><sup>{description}</sup>", "x": 0.02, "xanchor": "left"},
        margin={"l": 90, "r": 40, "t": 110, "b": 80},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "x": 0.02},
        hoverlabel={"namelength": -1},
    )
    if static_name in {
        "scores-by-skill.svg",
        "optimization-validation.svg",
        "copro-overall-paired-score.svg",
        "copro-skill-lift.svg",
        "copro-case-paired-score.svg",
        "copro-score-by-token-band.svg",
        "copro-criterion-lift.svg",
        "model-baseline-score.svg",
        "model-optimized-score.svg",
    }:
        figure.update_xaxes(range=[0, 1], title_text=unit)
    elif static_name == "stage-latency-summary.svg":
        figure.update_xaxes(title_text="seconds")
    elif static_name == "token-usage-by-stage.svg":
        figure.update_yaxes(title_text="tokens")
    elif static_name == "endpoint-workload.svg":
        figure.update_yaxes(title_text="requests")
    elif static_name == "endpoint-saturation.svg":
        figure.update_yaxes(title_text="peak / configured capacity", tickformat=".0%")
    elif static_name == "cache-reuse-by-stage.svg":
        figure.update_yaxes(title_text="stage results")
    elif static_name == "copro-case-delta-distribution.svg":
        figure.update_xaxes(title_text="optimized − original judge score")
        figure.update_yaxes(title_text="cases")
    elif static_name == "copro-delta-by-expected-tokens.svg":
        figure.update_xaxes(type="log", title_text="expected-response tokens")
        figure.update_yaxes(title_text="optimized − original judge score", range=[-1, 1])
        figure.add_hline(y=0, line_dash="dash", line_color="#64748b")
    elif static_name == "optimizer-lift.svg":
        figure.update_xaxes(title_text="optimized − paired baseline judge score", range=[-1, 1])
    elif static_name in {"model-skill-heatmap.svg", "model-token-band-score.svg"}:
        figure.update_xaxes(title_text=("skill" if "skill" in static_name else "token band"))
        figure.update_yaxes(title_text="model arm")
    elif static_name == "model-quality-vs-latency.svg":
        figure.update_xaxes(title_text="total model-call latency (seconds)")
        figure.update_yaxes(title_text="judge score", range=[0, 1])
    elif static_name == "model-quality-vs-tokens.svg":
        figure.update_xaxes(title_text="total provider-reported tokens")
        figure.update_yaxes(title_text="judge score", range=[0, 1])
    return pio.to_html(
        figure,
        include_plotlyjs="cdn",
        full_html=True,
        auto_play=False,
        div_id=f"agent-eval-{static_name.removesuffix('.svg')}",
        config={
            "displaylogo": False,
            "responsive": True,
            "scrollZoom": True,
            "toImageButtonOptions": {"format": "svg", "filename": static_name.removesuffix(".svg")},
        },
    ).encode("utf-8")


def _chart_description(name: str) -> str:
    definition = _CHART_DEFINITIONS.get(name, {})
    return str(definition.get("description", ""))


def _chart_manifest(
    artifacts: Mapping[str, tuple[bytes, str]],
    omissions: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    present = {Path(path).name for path in artifacts if path.endswith(".svg")}
    interactive = {Path(path).name for path in artifacts if path.endswith(".html")}
    omitted = {
        str(item.get("artifact")): str(item.get("reason"))
        for item in omissions
        if isinstance(item.get("artifact"), str)
    }
    charts = []
    for name, definition in sorted(_CHART_DEFINITIONS.items()):
        if name in _COMPARISON_CHART_NAMES:
            continue
        status = "generated" if name in present else "omitted"
        interactive_name = name.removesuffix(".svg") + ".html"
        canonical_artifact = (
            f"graphs/{interactive_name}" if interactive_name in interactive else f"graphs/{name}"
        )
        charts.append(
            {
                # The Plotly document is the canonical human view. SVG is a
                # static export for reports and environments that cannot run
                # JavaScript.
                "artifact": canonical_artifact,
                "static_artifact": f"graphs/{name}",
                **(
                    {"interactive_artifact": f"graphs/{interactive_name}"}
                    if interactive_name in interactive
                    else {}
                ),
                **definition,
                "status": status,
                **(
                    {"omission_reason": omitted.get(name, "not_applicable")}
                    if status == "omitted"
                    else {}
                ),
            }
        )
    return {
        "schema_version": 2,
        "purpose": (
            "Definitions and provenance for recorder-neutral experiment charts. Plotly HTML is "
            "the interactive analytical view; SVG is the static export. Raw observations remain "
            "in the named source tables."
        ),
        "interactive_explorer": "graphs/index.html",
        "charts": charts,
    }


def _dag_overview(
    nodes: Sequence[Mapping[str, object]],
    edges: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    attempts = {str(item["id"]): item for item in nodes if item.get("type") == "attempt"}
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for item in attempts.values():
        grouped[str(item.get("scope", item.get("stage", "unknown")))].append(item)
    overview_nodes: list[Mapping[str, object]] = []
    for scope, values in sorted(grouped.items()):
        states = Counter(str(item.get("state", "unknown")) for item in values)
        stage = str(values[0].get("stage", "unknown"))
        overview_nodes.append(
            {
                "id": f"scope:{scope}",
                "scope": scope,
                "stage": stage,
                "attempts": len(values),
                "states": dict(sorted(states.items())),
                "executed": sum(not bool(item.get("cache_hit")) for item in values),
                "reused": sum(bool(item.get("cache_hit")) for item in values),
            }
        )
    scope_by_attempt = {
        node_id: f"scope:{item.get('scope', item.get('stage', 'unknown'))}"
        for node_id, item in attempts.items()
    }
    edge_counts: Counter[tuple[str, str, str]] = Counter()
    for edge in edges:
        edge_type = str(edge.get("type"))
        if edge_type not in {"dependency", "retry"}:
            continue
        source = scope_by_attempt.get(str(edge.get("source")))
        target = scope_by_attempt.get(str(edge.get("target")))
        if source is None or target is None or source == target:
            continue
        edge_counts[(source, target, edge_type)] += 1
    overview_edges = [
        {"source": source, "target": target, "type": edge_type, "count": count}
        for (source, target, edge_type), count in sorted(edge_counts.items())
    ]
    return {"schema_version": 1, "nodes": overview_nodes, "edges": overview_edges}


def _dag_svg(dag: Mapping[str, object]) -> bytes:
    overview = dag.get("overview", {})
    overview = overview if isinstance(overview, Mapping) else {}
    raw_nodes = overview.get("nodes", [])
    raw_edges = overview.get("edges", [])
    nodes = (
        [item for item in raw_nodes if isinstance(item, Mapping)]
        if isinstance(raw_nodes, Sequence) and not isinstance(raw_nodes, (str, bytes))
        else []
    )
    edges = (
        [item for item in raw_edges if isinstance(item, Mapping)]
        if isinstance(raw_edges, Sequence) and not isinstance(raw_edges, (str, bytes))
        else []
    )
    stages = sorted(
        {str(item.get("stage", "unknown")) for item in nodes},
        key=lambda value: (_STAGE_ORDER.get(value, len(_STAGE_ORDER)), value),
    )
    by_stage: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for item in nodes:
        by_stage[str(item.get("stage", "unknown"))].append(item)
    for values in by_stage.values():
        values.sort(key=lambda item: str(item.get("scope")))
    column_width = 210
    node_width = 174
    node_height = 72
    left = 42
    top = 182
    gap_y = 26
    width = max(960, left * 2 + column_width * max(1, len(stages)))
    height = max(
        260,
        top + max((len(by_stage[stage]) for stage in stages), default=1) * (node_height + gap_y),
    )
    title = str(_CHART_DEFINITIONS["execution-dag.svg"]["title"])
    description = _chart_description("execution-dag.svg")
    body = [
        _svg_header(width, height, title, description),
        *_svg_heading(title, description),
        '<defs><marker id="dag-dependency-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#64748b"/></marker>'
        '<marker id="dag-retry-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#7c3aed"/></marker></defs>',
        '<g aria-label="Legend">',
        '<circle cx="48" cy="85" r="6" fill="#15803d"/>'
        '<text x="60" y="89" font-size="11">Green — all attempts persisted</text>',
        '<circle cx="286" cy="85" r="6" fill="#d97706"/>'
        '<text x="298" y="89" font-size="11">Orange — unfinished attempts remain</text>',
        '<circle cx="558" cy="85" r="6" fill="#dc2626"/>'
        '<text x="570" y="89" font-size="11">Red — contains failed or rejected attempts</text>',
        '<path d="M 48 112 L 84 112" stroke="#64748b" stroke-width="1.8" '
        'marker-end="url(#dag-dependency-arrow)"/>'
        '<text x="94" y="116" font-size="11">Solid gray arrow — dependency</text>',
        '<path d="M 286 112 L 322 112" stroke="#7c3aed" stroke-width="1.8" '
        'stroke-dasharray="7 5" marker-end="url(#dag-retry-arrow)"/>'
        '<text x="332" y="116" font-size="11">Dashed purple arrow — retry</text>',
        '<text x="558" y="116" font-size="11" fill="#475569">'
        "Attempts = recorded executions; reused = cache hits</text>",
        "</g>",
    ]
    positions: dict[str, tuple[float, float]] = {}
    for column, stage in enumerate(stages):
        x = left + column * column_width
        body.append(
            f'<text x="{x + node_width / 2:.1f}" y="160" text-anchor="middle" '
            f'font-size="12" font-weight="bold">{html.escape(stage.replace("_", " ").title())}</text>'
        )
        for row, item in enumerate(by_stage[stage]):
            y = top + row * (node_height + gap_y)
            positions[str(item.get("id"))] = (x, y)
    for edge in edges:
        source = positions.get(str(edge.get("source")))
        target = positions.get(str(edge.get("target")))
        if source is None or target is None:
            continue
        x0, y0 = source[0] + node_width, source[1] + node_height / 2
        x1, y1 = target[0], target[1] + node_height / 2
        bend = max(36.0, abs(x1 - x0) * 0.45)
        edge_type = str(edge.get("type"))
        retry = edge_type == "retry"
        edge_color = "#7c3aed" if retry else "#64748b"
        edge_dash = ' stroke-dasharray="7 5"' if retry else ""
        marker = "dag-retry-arrow" if retry else "dag-dependency-arrow"
        body.append(
            f'<path class="dag-edge" data-edge-type="{html.escape(str(edge.get("type")))}" '
            f'd="M {x0:.1f} {y0:.1f} C {x0 + bend:.1f} {y0:.1f}, '
            f'{x1 - bend:.1f} {y1:.1f}, {x1:.1f} {y1:.1f}" fill="none" '
            f'stroke="{edge_color}" stroke-width="1.8" opacity="0.72"{edge_dash} '
            f'marker-end="url(#{marker})">'
            f"<title>{int(_number_or(edge.get('count'), 0))} "
            f"{html.escape(str(edge.get('type')))} edges</title></path>"
        )
    for stage in stages:
        for item in by_stage[stage]:
            x, y = positions[str(item.get("id"))]
            states = item.get("states", {})
            states = states if isinstance(states, Mapping) else {}
            failed = _int_or(states.get("failed"), 0) + _int_or(states.get("rejected"), 0)
            persisted = _int_or(states.get("persisted"), 0)
            attempts = _int_or(item.get("attempts"), 0)
            unfinished = max(0, attempts - persisted - failed)
            color = "#dc2626" if failed else "#d97706" if unfinished else "#15803d"
            scope = str(item.get("scope", stage))
            label = scope.replace("optimize/", "").replace("/", " › ")
            summary = f"{attempts} attempts · {_int_or(item.get('reused'), 0)} reused"
            state_order = (
                "failed",
                "rejected",
                "prepared",
                "claimed",
                "submitted",
                "validated",
                "persisted",
            )
            state_summary = (
                " · ".join(
                    f"{_int_or(states.get(state), 0)} {state}"
                    for state in state_order
                    if _int_or(states.get(state), 0)
                )
                or "no recorded state"
            )
            body.extend(
                (
                    f'<g class="dag-node" data-stage="{html.escape(stage)}">'
                    f"<title>{html.escape(scope)} · {html.escape(summary)} · "
                    f"{html.escape(json.dumps(states, sort_keys=True))}</title>",
                    f'<rect x="{x}" y="{y}" width="{node_width}" height="{node_height}" '
                    f'rx="6" fill="#ffffff" stroke="{color}" stroke-width="2"/>',
                    f'<circle cx="{x + 13}" cy="{y + 17}" r="5" fill="{color}"/>',
                    f'<text x="{x + 24}" y="{y + 21}" font-size="11" font-weight="bold">'
                    f"{html.escape(label[:24])}</text>",
                    f'<text x="{x + 12}" y="{y + 43}" font-size="10" fill="#475569">'
                    f"{html.escape(state_summary[:31])}</text>",
                    f'<text x="{x + 12}" y="{y + 60}" font-size="9" fill="#64748b">'
                    f"{html.escape(summary)}</text></g>",
                )
            )
    if not edges:
        body.append(
            '<text x="42" y="154" font-size="11" fill="#64748b">'
            "No cross-scope dependency or retry edges have been recorded yet.</text>"
        )
    body.append("</svg>")
    return "\n".join(body).encode("utf-8")


def _latency_summary_rows(
    statuses: Sequence[StageStatus],
) -> tuple[Mapping[str, object], ...]:
    grouped: dict[str, list[float]] = defaultdict(list)
    failures: Counter[str] = Counter()
    for status in statuses:
        if status.result is None or status.result.provenance.get("cache_hit") is True:
            continue
        stage = status.packet.stage.value
        grouped[stage].append(_duration(status))
        if status.result.status != "completed":
            failures[stage] += 1
    return tuple(
        {
            "stage": stage,
            "count": len(values),
            "failed": failures[stage],
            "p50_seconds": _quantile(values, 0.5),
            "p95_seconds": _quantile(values, 0.95),
            "maximum_seconds": max(values),
        }
        for stage, values in sorted(
            grouped.items(), key=lambda item: (_STAGE_ORDER.get(item[0], 99), item[0])
        )
        if values
    )


def _token_usage_rows(
    model_calls: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for call in model_calls:
        if call.get("status") != "completed":
            continue
        stage = str(call.get("stage", "unknown"))
        input_tokens = _usage_token(call, "prompt_tokens", "input_tokens")
        output_tokens = _usage_token(call, "completion_tokens", "output_tokens")
        total_tokens = _usage_token(call, "total_tokens")
        if total_tokens <= 0:
            total_tokens = input_tokens + output_tokens
        grouped[stage]["input_tokens"] += input_tokens
        grouped[stage]["output_tokens"] += output_tokens
        grouped[stage]["unclassified_tokens"] += max(0, total_tokens - input_tokens - output_tokens)
        grouped[stage]["calls"] += 1
    return tuple(
        {"stage": stage, **dict(values)}
        for stage, values in sorted(
            grouped.items(), key=lambda item: (_STAGE_ORDER.get(item[0], 99), item[0])
        )
        if sum(values[key] for key in ("input_tokens", "output_tokens", "unclassified_tokens"))
    )


def _cache_reuse_rows(
    statuses: Sequence[StageStatus],
) -> tuple[Mapping[str, object], ...]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for status in statuses:
        if status.result is None:
            continue
        key = "reused" if status.result.provenance.get("cache_hit") is True else "executed"
        grouped[status.packet.stage.value][key] += 1
    return tuple(
        {
            "stage": stage,
            "executed": values["executed"],
            "reused": values["reused"],
            "total": values["executed"] + values["reused"],
        }
        for stage, values in sorted(
            grouped.items(), key=lambda item: (_STAGE_ORDER.get(item[0], 99), item[0])
        )
    )


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _latency_summary_svg(
    rows: Sequence[Mapping[str, object]],
    *,
    description: str,
) -> bytes:
    title = str(_CHART_DEFINITIONS["stage-latency-summary.svg"]["title"])
    width = 1080
    left, right, top, bottom = 190, 80, 112, 52
    row_height = 54
    height = max(250, top + row_height * len(rows) + bottom)
    plot_width = width - left - right
    maximum = max((_number_or(item.get("maximum_seconds"), 0.0) for item in rows), default=1)
    maximum = max(maximum, 1e-12)
    body = [_svg_header(width, height, title, description), *_svg_heading(title, description)]
    for tick in range(6):
        value = maximum * tick / 5
        x = left + plot_width * tick / 5
        body.extend(
            (
                f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" '
                f'y2="{height - bottom}" stroke="#e2e8f0"/>',
                f'<text x="{x:.1f}" y="{height - 24}" text-anchor="middle" '
                f'font-size="11">{value:.1f}</text>',
            )
        )
    body.append(
        f'<text x="{left + plot_width / 2:.1f}" y="{height - 5}" text-anchor="middle" '
        'font-size="11">Wall-clock duration (seconds)</text>'
    )
    for index, item in enumerate(rows):
        y = top + index * row_height + 18
        p50 = _number_or(item.get("p50_seconds"), 0.0)
        p95 = _number_or(item.get("p95_seconds"), 0.0)
        observed_max = _number_or(item.get("maximum_seconds"), 0.0)
        x50 = left + plot_width * p50 / maximum
        x95 = left + plot_width * p95 / maximum
        xmax = left + plot_width * observed_max / maximum
        stage = str(item.get("stage", "unknown"))
        count = _int_or(item.get("count"), 0)
        failed = _int_or(item.get("failed"), 0)
        body.extend(
            (
                f'<text x="{left - 14}" y="{y + 4}" text-anchor="end" font-size="12">'
                f"{html.escape(stage)}</text>",
                f'<line x1="{left}" y1="{y}" x2="{xmax:.1f}" y2="{y}" '
                'stroke="#94a3b8" stroke-width="2"/>',
                f'<line x1="{x50:.1f}" y1="{y}" x2="{x95:.1f}" y2="{y}" '
                'stroke="#2563eb" stroke-width="8" stroke-linecap="round"/>',
                f'<circle cx="{x50:.1f}" cy="{y}" r="5" fill="#0f172a">'
                f"<title>p50 {p50:.3f}s</title></circle>",
                f'<circle cx="{x95:.1f}" cy="{y}" r="5" fill="#2563eb">'
                f"<title>p95 {p95:.3f}s</title></circle>",
                f'<circle cx="{xmax:.1f}" cy="{y}" r="4" fill="#94a3b8">'
                f"<title>maximum {observed_max:.3f}s</title></circle>",
                f'<text x="{width - right + 10}" y="{y + 4}" font-size="10">'
                f"n={count}, failed={failed}</text>",
            )
        )
    body.extend(
        (
            f'<circle cx="{left}" cy="92" r="5" fill="#0f172a"/>',
            f'<text x="{left + 10}" y="96" font-size="10">p50</text>',
            f'<circle cx="{left + 58}" cy="92" r="5" fill="#2563eb"/>',
            f'<text x="{left + 68}" y="96" font-size="10">p95</text>',
            f'<circle cx="{left + 126}" cy="92" r="4" fill="#94a3b8"/>',
            f'<text x="{left + 136}" y="96" font-size="10">maximum</text>',
            "</svg>",
        )
    )
    return "\n".join(body).encode("utf-8")


def _token_usage_svg(
    rows: Sequence[Mapping[str, object]],
    *,
    description: str,
) -> bytes:
    title = str(_CHART_DEFINITIONS["token-usage-by-stage.svg"]["title"])
    series = (
        ("input_tokens", "Input", "#2563eb"),
        ("output_tokens", "Output", "#f59e0b"),
        ("unclassified_tokens", "Unclassified", "#94a3b8"),
    )
    width = 1080
    left, right, top = 190, 110, 118
    row_height = 46
    height = max(250, top + row_height * len(rows) + 48)
    plot_width = width - left - right
    maximum = max(
        (sum(_int_or(item.get(key), 0) for key, _, _ in series) for item in rows),
        default=1,
    )
    body = [_svg_header(width, height, title, description), *_svg_heading(title, description)]
    legend_x = left
    for key, label, color in series:
        del key
        body.extend(
            (
                f'<rect x="{legend_x}" y="88" width="12" height="12" fill="{color}"/>',
                f'<text x="{legend_x + 18}" y="98" font-size="10">{label}</text>',
            )
        )
        legend_x += 112
    for index, item in enumerate(rows):
        y = top + index * row_height
        stage = str(item.get("stage", "unknown"))
        body.append(
            f'<text x="{left - 14}" y="{y + 20}" text-anchor="end" font-size="12">'
            f"{html.escape(stage)}</text>"
        )
        offset = left
        total = 0
        for key, label, color in series:
            value = _int_or(item.get(key), 0)
            total += value
            size = plot_width * value / max(maximum, 1)
            body.append(
                f'<rect x="{offset:.1f}" y="{y}" width="{size:.1f}" height="28" '
                f'fill="{color}"><title>{label}: {value:,} tokens</title></rect>'
            )
            offset += size
        body.append(
            f'<text x="{width - right + 10}" y="{y + 20}" font-size="10">'
            f"{total:,} · {_int_or(item.get('calls'), 0)} calls</text>"
        )
    body.append("</svg>")
    return "\n".join(body).encode("utf-8")


def _endpoint_saturation_svg(
    rows: Sequence[Mapping[str, object]],
    *,
    description: str,
) -> bytes:
    title = str(_CHART_DEFINITIONS["endpoint-saturation.svg"]["title"])
    values = tuple(
        (
            str(item.get("endpoint_id")),
            _int_or(item.get("peak_observed_concurrency"), 0),
            _int_or(item.get("configured_capacity"), 0),
        )
        for item in rows
    )
    width = 1040
    left, right, top = 270, 120, 108
    row_height = 48
    height = max(230, top + row_height * len(values) + 42)
    plot_width = width - left - right
    maximum_ratio = max((peak / capacity for _, peak, capacity in values), default=1.0)
    scale = max(1.0, maximum_ratio)
    body = [_svg_header(width, height, title, description), *_svg_heading(title, description)]
    capacity_x = left + plot_width / scale
    body.append(
        f'<line x1="{capacity_x:.1f}" y1="{top - 12}" x2="{capacity_x:.1f}" '
        f'y2="{height - 32}" stroke="#dc2626" stroke-dasharray="5 4"/>'
    )
    body.append(
        f'<text x="{capacity_x:.1f}" y="96" text-anchor="middle" font-size="10" '
        'fill="#991b1b">configured capacity</text>'
    )
    for index, (endpoint, peak, capacity) in enumerate(values):
        y = top + index * row_height
        ratio = peak / capacity
        size = plot_width * ratio / scale
        body.extend(
            (
                f'<text x="{left - 14}" y="{y + 20}" text-anchor="end" font-size="12">'
                f"{html.escape(endpoint)}</text>",
                f'<rect x="{left}" y="{y}" width="{plot_width}" height="28" fill="#e2e8f0"/>',
                f'<rect x="{left}" y="{y}" width="{size:.1f}" height="28" fill="#2563eb"/>',
                f'<text x="{width - right + 10}" y="{y + 20}" font-size="11">'
                f"{ratio:.0%} · {peak}/{capacity}</text>",
            )
        )
    body.append("</svg>")
    return "\n".join(body).encode("utf-8")


def _cache_reuse_svg(
    rows: Sequence[Mapping[str, object]],
    *,
    description: str,
) -> bytes:
    title = str(_CHART_DEFINITIONS["cache-reuse-by-stage.svg"]["title"])
    width = 1040
    left, right, top = 190, 170, 112
    row_height = 48
    height = max(240, top + row_height * len(rows) + 42)
    plot_width = width - left - right
    body = [_svg_header(width, height, title, description), *_svg_heading(title, description)]
    body.extend(
        (
            f'<rect x="{left}" y="88" width="12" height="12" fill="#2563eb"/>',
            f'<text x="{left + 18}" y="98" font-size="10">executed</text>',
            f'<rect x="{left + 90}" y="88" width="12" height="12" fill="#16a34a"/>',
            f'<text x="{left + 108}" y="98" font-size="10">exact reuse</text>',
        )
    )
    for index, item in enumerate(rows):
        y = top + index * row_height
        total = max(1, _int_or(item.get("total"), 0))
        executed = _int_or(item.get("executed"), 0)
        reused = _int_or(item.get("reused"), 0)
        executed_width = plot_width * executed / total
        reused_width = plot_width * reused / total
        stage = str(item.get("stage", "unknown"))
        body.extend(
            (
                f'<text x="{left - 14}" y="{y + 20}" text-anchor="end" font-size="12">'
                f"{html.escape(stage)}</text>",
                f'<rect x="{left}" y="{y}" width="{executed_width:.1f}" height="28" '
                'fill="#2563eb"/>',
                f'<rect x="{left + executed_width:.1f}" y="{y}" '
                f'width="{reused_width:.1f}" height="28" fill="#16a34a"/>',
                f'<text x="{width - right + 10}" y="{y + 20}" font-size="11">'
                f"{reused / total:.0%} reused · {executed}/{reused}</text>",
            )
        )
    body.append("</svg>")
    return "\n".join(body).encode("utf-8")


def _bar_svg(
    title: str,
    values: Sequence[tuple[str, float]],
    *,
    maximum: float | None = None,
    description: str = "",
    unit: str = "value",
) -> bytes:
    width = 1000
    label_width = 260
    bar_width = 680
    row_height = 34
    top = 86
    height = max(150, top + row_height * len(values) + 34)
    observed = max((value for _, value in values), default=1.0)
    scale = maximum if maximum is not None else max(observed, 1e-12)
    body = [
        _svg_header(width, height, title, description),
        *_svg_heading(title, description),
    ]
    for index, (label, value) in enumerate(values):
        y = top + index * row_height
        size = max(0.0, min(bar_width, bar_width * value / scale))
        body.extend(
            (
                f'<text x="20" y="{y + 17}" font-size="12">{html.escape(label[:42])}</text>',
                f'<rect x="{label_width}" y="{y}" width="{bar_width}" height="22" rx="4" fill="#e2e8f0"/>',
                f'<rect x="{label_width}" y="{y}" width="{size:.3f}" height="22" rx="4" fill="#2563eb"/>',
                f'<text x="{label_width + 8}" y="{y + 16}" font-size="12" fill="#0f172a">'
                f"{value:.4f} {html.escape(unit)}</text>",
            )
        )
    body.append("</svg>")
    return "\n".join(body).encode("utf-8")


def _signed_bar_svg(
    title: str,
    values: Sequence[tuple[str, float]],
    *,
    description: str,
) -> bytes:
    width = 1100
    left, right, top = 310, 90, 92
    plot_width = width - left - right
    row_height = 34
    height = max(180, top + row_height * len(values) + 54)
    limit = max((abs(value) for _, value in values), default=1.0)
    limit = max(limit, 0.01)
    center = left + plot_width / 2
    body = [_svg_header(width, height, title, description), *_svg_heading(title, description)]
    body.append(
        f'<line x1="{center:.1f}" y1="{top - 10}" x2="{center:.1f}" '
        f'y2="{height - 38}" stroke="#64748b" stroke-dasharray="5 4"/>'
    )
    for index, (label, value) in enumerate(values):
        y = top + index * row_height
        size = abs(value) / limit * (plot_width / 2)
        x = center if value >= 0 else center - size
        color = "#16a34a" if value > 0 else "#dc2626" if value < 0 else "#64748b"
        body.extend(
            (
                f'<text x="{left - 14}" y="{y + 17}" text-anchor="end" font-size="12">'
                f"{html.escape(label[:47])}</text>",
                f'<rect x="{x:.2f}" y="{y}" width="{max(size, 1.0):.2f}" height="22" '
                f'rx="3" fill="{color}"/>',
                f'<text x="{width - right + 8}" y="{y + 17}" font-size="11" '
                f'fill="{color}">{value:+.4f}</text>',
            )
        )
    body.append("</svg>")
    return "\n".join(body).encode("utf-8")


def _matrix_svg(
    title: str,
    labels: Sequence[str],
    columns: Sequence[str],
    matrix: Sequence[Sequence[float | None]],
    *,
    description: str,
) -> bytes:
    label_width = 290
    cell_width = 112
    cell_height = 38
    top = 150
    width = max(900, label_width + cell_width * len(columns) + 50)
    height = max(250, top + cell_height * len(labels) + 48)
    body = [_svg_header(width, height, title, description), *_svg_heading(title, description)]
    for index, column in enumerate(columns):
        x = label_width + index * cell_width + cell_width / 2
        body.append(
            f'<text x="{x:.1f}" y="{top - 12}" text-anchor="end" font-size="10" '
            f'transform="rotate(-35 {x:.1f} {top - 12})">{html.escape(column[:28])}</text>'
        )
    for row_index, label in enumerate(labels):
        y = top + row_index * cell_height
        body.append(
            f'<text x="{label_width - 12}" y="{y + 24}" text-anchor="end" '
            f'font-size="11">{html.escape(label[:46])}</text>'
        )
        for column_index, value in enumerate(matrix[row_index]):
            x = label_width + column_index * cell_width
            if value is None:
                color, text_value = "#e2e8f0", "—"
            else:
                bounded = max(0.0, min(1.0, value))
                red = int(220 - 150 * bounded)
                green = int(70 + 115 * bounded)
                color, text_value = f"rgb({red},{green},90)", f"{value:.3f}"
            body.extend(
                (
                    f'<rect x="{x}" y="{y}" width="{cell_width - 3}" '
                    f'height="{cell_height - 3}" fill="{color}"/>',
                    f'<text x="{x + (cell_width - 3) / 2:.1f}" y="{y + 24}" '
                    f'text-anchor="middle" font-size="11">{text_value}</text>',
                )
            )
    body.append("</svg>")
    return "\n".join(body).encode("utf-8")


def _quality_scatter_svg(
    title: str,
    values: Sequence[tuple[str, float, float]],
    *,
    x_label: str,
    description: str,
) -> bytes:
    width, height = 1100, 620
    left, right, top, bottom = 90, 40, 92, 72
    plot_width = width - left - right
    plot_height = height - top - bottom
    maximum_x = max((value for _, value, _ in values), default=1.0)
    maximum_x = max(maximum_x, 1e-12)
    body = [_svg_header(width, height, title, description), *_svg_heading(title, description)]
    body.extend(
        (
            f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" '
            f'y2="{top + plot_height}" stroke="#64748b"/>',
            f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#64748b"/>',
            f'<text x="{left + plot_width / 2:.1f}" y="{height - 20}" '
            f'text-anchor="middle" font-size="12">{html.escape(x_label)}</text>',
            f'<text x="20" y="{top + plot_height / 2:.1f}" '
            f'transform="rotate(-90 20 {top + plot_height / 2:.1f})" '
            'text-anchor="middle" font-size="12">judge score</text>',
        )
    )
    for label, x_value, score in values:
        x = left + max(0.0, x_value) / maximum_x * plot_width
        y = top + (1 - max(0.0, min(1.0, score))) * plot_height
        body.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="6" fill="#2563eb">'
            f"<title>{html.escape(label)} · {x_value:.3f} · score {score:.4f}</title></circle>"
        )
    body.append("</svg>")
    return "\n".join(body).encode("utf-8")


def _dumbbell_svg(
    title: str,
    values: Sequence[tuple[str, float, float]],
    *,
    description: str = "",
) -> bytes:
    width = 1200
    left = 330
    right = 110
    plot_width = width - left - right
    row_height = 34
    top = 122
    height = max(160, top + row_height * len(values) + 46)
    body = [
        _svg_header(width, height, title, description),
        *_svg_heading(title, description),
        '<circle cx="430" cy="88" r="6" fill="#4C78A8"/>',
        '<text x="442" y="93" font-size="12">Original</text>',
        '<circle cx="530" cy="88" r="6" fill="#F58518"/>',
        '<text x="542" y="93" font-size="12">COPRO optimized</text>',
    ]
    for tick in range(6):
        value = tick / 5
        x = left + plot_width * value
        body.extend(
            (
                f'<line x1="{x:.1f}" y1="{top - 10}" x2="{x:.1f}" y2="{height - 32}" stroke="#e2e8f0"/>',
                f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle" font-size="11">{value:.1f}</text>',
            )
        )
    for index, (label, baseline, optimized) in enumerate(values):
        y = top + index * row_height
        baseline = max(0.0, min(1.0, baseline))
        optimized = max(0.0, min(1.0, optimized))
        x0 = left + plot_width * baseline
        x1 = left + plot_width * optimized
        delta = optimized - baseline
        body.extend(
            (
                f'<text x="24" y="{y + 5}" font-size="12">{html.escape(label[:47])}</text>',
                f'<line x1="{x0:.2f}" y1="{y}" x2="{x1:.2f}" y2="{y}" stroke="#64748b" stroke-width="3"/>',
                f'<circle cx="{x0:.2f}" cy="{y}" r="6" fill="#4C78A8"/>',
                f'<circle cx="{x1:.2f}" cy="{y}" r="6" fill="#F58518"/>',
                f'<text x="{width - right + 12}" y="{y + 5}" font-size="11" fill="{("#137333" if delta > 0 else "#b3261e" if delta < 0 else "#475569")}">{delta:+.3f}</text>',
            )
        )
    body.append("</svg>")
    return "\n".join(body).encode("utf-8")


def _delta_histogram_svg(
    rows: Sequence[Mapping[str, object]],
    *,
    description: str = "",
) -> bytes:
    edges = (-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0000001)
    counts = [0] * (len(edges) - 1)
    for row in rows:
        value = max(-1.0, min(1.0, _number_or(row.get("delta"), 0.0)))
        index = next(
            (
                position
                for position in range(len(counts))
                if edges[position] <= value < edges[position + 1]
            ),
            len(counts) - 1,
        )
        counts[index] += 1
    labels = [
        f"{edges[index]:+.2f}…{min(edges[index + 1], 1.0):+.2f}" for index in range(len(counts))
    ]
    return _bar_svg(
        "Distribution of paired COPRO case-score deltas",
        tuple(zip(labels, (float(value) for value in counts), strict=True)),
        description=description,
        unit="cases",
    )


def _token_delta_scatter_svg(
    rows: Sequence[Mapping[str, object]],
    *,
    description: str = "",
) -> bytes:
    width = 1100
    height = 620
    left, right, top, bottom = 90, 40, 92, 70
    plot_width = width - left - right
    plot_height = height - top - bottom
    tokens = [int(row["expected_response_tokens"]) for row in rows]
    maximum = max(tokens, default=1)
    log_maximum = max(math.log1p(maximum), 1e-12)
    body = [
        _svg_header(
            width,
            height,
            "COPRO delta by exact expected-response tokens",
            description,
        ),
        *_svg_heading("COPRO delta by exact expected-response tokens", description),
        f'<line x1="{left}" y1="{top + plot_height / 2:.2f}" x2="{left + plot_width}" y2="{top + plot_height / 2:.2f}" stroke="#64748b" stroke-dasharray="5 4"/>',
        f'<text x="{left + plot_width / 2:.2f}" y="{height - 18}" text-anchor="middle" font-size="12">Expected-response tokens (log scale)</text>',
        f'<text x="18" y="{top + plot_height / 2:.2f}" transform="rotate(-90 18 {top + plot_height / 2:.2f})" text-anchor="middle" font-size="12">Optimized − original judge score</text>',
    ]
    for tick in range(5):
        delta = -1 + tick * 0.5
        y = top + (1 - (delta + 1) / 2) * plot_height
        body.extend(
            (
                f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_width}" y2="{y:.2f}" stroke="#e2e8f0"/>',
                f'<text x="{left - 12}" y="{y + 4:.2f}" text-anchor="end" font-size="11">{delta:+.1f}</text>',
            )
        )
    for row in rows:
        token_count = int(row["expected_response_tokens"])
        delta = max(-1.0, min(1.0, _number_or(row.get("delta"), 0.0)))
        x = left + math.log1p(token_count) / log_maximum * plot_width
        y = top + (1 - (delta + 1) / 2) * plot_height
        color = "#137333" if delta > 0 else "#b3261e" if delta < 0 else "#475569"
        body.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{color}" opacity="0.72"><title>{html.escape(str(row.get("case_id")))} · {token_count} tokens · {delta:+.3f}</title></circle>'
        )
    for token_count in sorted({1, 128, 256, 512, 1024, 2048, maximum}):
        if token_count > maximum:
            continue
        x = left + math.log1p(token_count) / log_maximum * plot_width
        body.append(
            f'<text x="{x:.2f}" y="{height - 42}" text-anchor="middle" font-size="10">{token_count}</text>'
        )
    body.append("</svg>")
    return "\n".join(body).encode("utf-8")


def _heatmap_svg(
    rows: Sequence[Mapping[str, object]],
    *,
    description: str = "",
) -> bytes:
    skills = sorted({str(item.get("skill_name")) for item in rows})
    criteria = sorted({str(item.get("criterion_id")) for item in rows})
    lookup = {
        (str(item.get("skill_name")), str(item.get("criterion_id"))): _number_or(
            item.get("mean_score"), 0.0
        )
        for item in rows
    }
    cell_w = 120
    cell_h = 32
    left = 240
    top = 98
    width = max(700, left + cell_w * len(criteria) + 30)
    height = max(150, top + cell_h * len(skills) + 30)
    body = [
        _svg_header(width, height, "Criterion heatmap", description),
        *_svg_heading("Criterion heatmap", description),
    ]
    for column, criterion in enumerate(criteria):
        x = left + column * cell_w
        body.append(f'<text x="{x + 4}" y="84" font-size="11">{html.escape(criterion[:17])}</text>')
    for row_index, skill in enumerate(skills):
        y = top + row_index * cell_h
        body.append(f'<text x="20" y="{y + 21}" font-size="12">{html.escape(skill[:32])}</text>')
        for column, criterion in enumerate(criteria):
            value = max(0.0, min(1.0, lookup.get((skill, criterion), 0.0)))
            red = round(220 - 150 * value)
            green = round(80 + 120 * value)
            x = left + column * cell_w
            body.extend(
                (
                    f'<rect x="{x}" y="{y}" width="{cell_w - 4}" height="{cell_h - 4}" fill="rgb({red},{green},110)"/>',
                    f'<text x="{x + 8}" y="{y + 20}" font-size="12">{value:.3f}</text>',
                )
            )
    body.append("</svg>")
    return "\n".join(body).encode("utf-8")


def _recording_index_html(
    run: RunDescriptor,
    chart_manifest: Mapping[str, object],
    tables: Sequence[TableArtifact],
) -> bytes:
    raw_charts = chart_manifest.get("charts", ())
    charts = (
        tuple(item for item in raw_charts if isinstance(item, Mapping))
        if isinstance(raw_charts, Sequence) and not isinstance(raw_charts, (str, bytes))
        else ()
    )
    generated = tuple(item for item in charts if item.get("status") == "generated")
    interactive = tuple(
        item for item in generated if isinstance(item.get("interactive_artifact"), str)
    )
    copro = next(
        (
            item
            for item in charts
            if str(item.get("static_artifact", "")).endswith("/copro-overall-paired-score.svg")
        ),
        None,
    )
    if copro is not None and copro.get("status") == "generated":
        comparison = (
            '<a href="graphs/copro-overall-paired-score.html">Open the original versus '
            "optimized comparison</a>."
        )
    else:
        reason = "not available"
        if copro is not None:
            reason = str(copro.get("omission_reason", reason)).replace("_", " ")
        comparison = f"Not available: {html.escape(reason)}."
    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MS Agent Eval run report</title>
<style>
:root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
body { margin: 0 auto; max-width: 920px; padding: 36px 24px; line-height: 1.5; }
h1 { margin-bottom: 4px; }
.muted { color: #64748b; }
.cards { display: grid; grid-template-columns: repeat(3, minmax(130px, 1fr)); gap: 12px; margin: 24px 0; }
.card { border: 1px solid #cbd5e1; border-radius: 8px; padding: 14px; }
.card strong { display: block; font-size: 24px; }
.card span { color: #64748b; font-size: 12px; }
.entry { border: 1px solid #cbd5e1; border-radius: 8px; margin: 12px 0; padding: 16px; }
.entry a { font-size: 18px; font-weight: 700; }
code { overflow-wrap: anywhere; }
@media (max-width: 620px) { .cards { grid-template-columns: 1fr; } }
@media (prefers-color-scheme: dark) {
  body { background: #0f172a; color: #e2e8f0; }
  .muted, .card span { color: #94a3b8; }
  .card, .entry { border-color: #334155; }
}
</style>
</head>
<body>
<h1>MS Agent Eval run report</h1>
<p class="muted">__RUN_NAME__</p>
<div class="cards">
  <div class="card"><strong>__STATUS__</strong><span>run status</span></div>
  <div class="card"><strong>__CHARTS__</strong><span>interactive Plotly charts</span></div>
  <div class="card"><strong>__TABLES__</strong><span>browsable tables</span></div>
</div>
<div class="entry">
  <a href="graphs/index.html">Open interactive charts</a>
  <p>Plotly views support hover, filters, zoom, pan and export. SVG files are static exports, not the primary analytical view.</p>
</div>
<div class="entry">
  <a href="tables/index.html">Open sortable tables</a>
  <p>Search, sort and page through stage attempts, model calls, case results, rubric scores, endpoint use and optimizer comparisons.</p>
</div>
<div class="entry">
  <strong>Original versus optimized</strong>
  <p>__COMPARISON__</p>
</div>
<p class="muted">Canonical local run: <code>__RUN_ID__</code>. Machine-readable artifacts remain available for reproducibility.</p>
</body>
</html>
"""
    return (
        template.replace("__RUN_NAME__", html.escape(run.name))
        .replace("__STATUS__", html.escape(run.status))
        .replace("__CHARTS__", str(len(interactive)))
        .replace("__TABLES__", str(len(tables)))
        .replace("__COMPARISON__", comparison)
        .replace("__RUN_ID__", html.escape(run.local_run_id))
        .encode("utf-8")
    )


def _comparison_index_html(
    run: RunDescriptor,
    chart_manifest: Mapping[str, object],
    tables: Sequence[TableArtifact],
) -> bytes:
    raw_charts = chart_manifest.get("charts", ())
    charts = (
        tuple(item for item in raw_charts if isinstance(item, Mapping))
        if isinstance(raw_charts, Sequence) and not isinstance(raw_charts, (str, bytes))
        else ()
    )
    chart_items = "".join(
        (
            '<li><a href="'
            + html.escape(str(item.get("interactive_artifact", "")))
            + '">'
            + html.escape(str(item.get("title", item.get("interactive_artifact", "Chart"))))
            + "</a><span>"
            + html.escape(str(item.get("description", "")))
            + "</span></li>"
        )
        for item in charts
        if isinstance(item.get("interactive_artifact"), str)
    )
    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model comparison report</title>
<style>
:root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
body { margin: 0 auto; max-width: 920px; padding: 36px 24px; line-height: 1.5; }
h1 { margin-bottom: 4px; }
.muted, li span { color: #64748b; }
.cards { display: grid; grid-template-columns: repeat(2, minmax(130px, 1fr)); gap: 12px; margin: 24px 0; }
.card { border: 1px solid #cbd5e1; border-radius: 8px; padding: 14px; }
.card strong { display: block; font-size: 24px; }
.card span { color: #64748b; font-size: 12px; }
ul { list-style: none; padding: 0; }
li { border-bottom: 1px solid #cbd5e1; display: grid; gap: 4px; padding: 12px 0; }
li a { font-size: 17px; font-weight: 700; }
@media (prefers-color-scheme: dark) {
  body { background: #0f172a; color: #e2e8f0; }
  .muted, li span, .card span { color: #94a3b8; }
  .card, li { border-color: #334155; }
}
</style>
</head>
<body>
<h1>Cross-model comparison</h1>
<p class="muted">__RUN_NAME__</p>
<div class="cards">
  <div class="card"><strong>__CHARTS__</strong><span>interactive Plotly charts</span></div>
  <div class="card"><strong>__TABLES__</strong><span>browsable comparison tables</span></div>
</div>
<p><a href="tables/index.html">Open sortable comparison tables</a></p>
<h2>Interactive charts</h2>
<ul>__CHART_ITEMS__</ul>
</body>
</html>
"""
    return (
        template.replace("__RUN_NAME__", html.escape(run.name))
        .replace("__CHARTS__", str(len(charts)))
        .replace("__TABLES__", str(len(tables)))
        .replace("__CHART_ITEMS__", chart_items)
        .encode("utf-8")
    )


def _table_index_html(tables: Sequence[TableArtifact]) -> bytes:
    payload = json.dumps(
        [
            {
                "name": table.name,
                "rows": [dict(row) for row in table.rows],
            }
            for table in tables
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Experiment tables</title>
<style>
:root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
body { margin: 0; padding: 24px; line-height: 1.4; }
h1 { margin: 0 0 4px; }
.muted { color: #64748b; }
.explanation { background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 10px; margin: 18px 0; padding: 16px 18px; }
.explanation h2 { font-size: 18px; margin: 0 0 8px; }
.explanation p { margin: 8px 0; }
.explanation ul { margin: 8px 0; padding-left: 22px; }
.explanation .warning { color: #b45309; font-weight: 700; }
.formula { background: #e2e8f0; border-radius: 6px; display: inline-block; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; padding: 5px 8px; }
.controls { display: flex; flex-wrap: wrap; gap: 12px; margin: 20px 0; }
label { display: grid; gap: 4px; color: #64748b; font-size: 12px; }
select, input, button { font: inherit; padding: 8px; }
input { min-width: 320px; }
.viewport { border: 1px solid #cbd5e1; max-height: 72vh; overflow: auto; }
table { border-collapse: separate; border-spacing: 0; font-size: 12px; width: 100%; }
th, td { border-bottom: 1px solid #e2e8f0; max-width: 420px; padding: 7px 10px; text-align: left; vertical-align: top; }
th { background: #f8fafc; cursor: pointer; position: sticky; top: 0; white-space: nowrap; z-index: 1; }
td { overflow-wrap: anywhere; }
.pager { align-items: center; display: flex; gap: 10px; margin-top: 12px; }
@media (prefers-color-scheme: dark) {
  body { background: #0f172a; color: #e2e8f0; }
  .muted, label { color: #94a3b8; }
  .explanation { background: #111827; border-color: #334155; }
  .explanation .warning { color: #fbbf24; }
  .formula { background: #1e293b; }
  .viewport, th, td { border-color: #334155; }
  th { background: #111827; }
}
</style>
</head>
<body>
<h1>Experiment tables</h1>
<p class="muted">These are the human-readable views. Failure summary groups incidents by stage,
error kind, and endpoint; stage failures identify affected cases and retry outcomes; model-call
failures preserve the provider message, root exception, endpoint URL, status or errno, and
retryability. The JSON artifacts are MLflow's canonical machine representation.</p>
<section id="explanation" class="explanation" aria-live="polite"></section>
<div class="controls">
  <label>Table<select id="table"></select></label>
  <label>Search<input id="search" type="search" placeholder="Filter any visible column"></label>
  <label>Rows per page<select id="page-size"><option>25</option><option selected>100</option><option>250</option><option>500</option></select></label>
</div>
<p id="summary" class="muted"></p>
<div class="viewport"><table><thead id="head"></thead><tbody id="body"></tbody></table></div>
<div class="pager"><button id="previous">Previous</button><span id="page"></span><button id="next">Next</button></div>
<script id="data" type="application/json">__PAYLOAD__</script>
<script>
const tables = JSON.parse(document.getElementById('data').textContent);
const tableSelect = document.getElementById('table');
for (const table of tables) {
  const option = document.createElement('option'); option.value = table.name;
  option.textContent = `${table.name} (${table.rows.length.toLocaleString()})`;
  tableSelect.appendChild(option);
}
let page = 0, sortColumn = '', sortDirection = 1;
const selected = () => tables.find(table => table.name === tableSelect.value) || tables[0];
const printable = value => value == null ? '' : (typeof value === 'object' ? JSON.stringify(value) : String(value));
const appendText = (parent, tag, text, className = '') => {
  const element = document.createElement(tag); element.textContent = text;
  if (className) element.className = className;
  parent.appendChild(element); return element;
};
function renderExplanation(table) {
  const panel = document.getElementById('explanation'); panel.replaceChildren();
  if (table.name !== 'judge-calibration') {
    appendText(panel, 'h2', table.name);
    appendText(panel, 'p', 'Select a column heading to sort. Search filters across every column in this table.');
    return;
  }
  appendText(panel, 'h2', 'How to read judge calibration');
  appendText(panel, 'p', 'Each row is one controlled candidate response whose expected quality is known. It is not a benchmark solver response. The fixture checks whether the judge ranks known-good and known-bad answers sensibly before solver answers are judged.');
  const skills = new Set(table.rows.map(row => row.skill).filter(Boolean));
  const cases = new Set(table.rows.map(row => row.case_id).filter(Boolean));
  const rubrics = new Set(table.rows.map(row => row.rubric_hash).filter(Boolean));
  appendText(panel, 'p', `Coverage in this calibration corpus: ${table.rows.length} fixtures, ${cases.size} cases, ${skills.size} skills, ${rubrics.size} rubrics.`);
  if (skills.size === 1) {
    appendText(panel, 'p', `Scope warning: every fixture uses only ${[...skills][0]}. This proves ranking behavior for that one rubric only; it does not prove that the judge is calibrated across the full skill bank.`, 'warning');
  }
  const repetitions = [...new Set(table.rows.map(row => Number(row.repetitions || 1)))];
  appendText(panel, 'p', `Repetitions: ${repetitions.join(', ')}. The same candidate is scored independently that many times. For each rubric criterion, the median repetition score is taken; aggregate_score is then the rubric-weighted sum of those criterion medians. Repetitions therefore do not create additional fixture rows.`);
  appendText(panel, 'p', 'quality_label meanings: strong = complete and correct; partial = useful but missing important requirements; incorrect = materially wrong; contradictory = internally incompatible claims; adversarial = attempts to manipulate the judge instead of answering.');
  appendText(panel, 'p', 'The labels do not prescribe exact numeric scores. incorrect, contradictory, and adversarial form one negative group, so they are not required to be ordered relative to one another.');
  const formula = appendText(panel, 'p', 'Current pass rule: ');
  appendText(formula, 'span', 'minimum(strong) > maximum(partial) AND minimum(partial) > maximum(all negative labels)', 'formula');
  appendText(panel, 'p', 'Important limitation: the current validator puts fixtures from every skill into those same global label buckets. For example, if Skill A has strong 0.80 and partial 0.60 while Skill B has strong 0.55 and partial 0.40, both skills are correctly ordered on their own, but the global check fails because 0.55 is not greater than 0.60. Proper multi-skill calibration therefore needs separate ordering ladders per skill/rubric, followed by an explicit aggregate policy.');
  const labelOrder = ['strong', 'partial', 'incorrect', 'contradictory', 'adversarial'];
  const scoreList = document.createElement('ul');
  for (const label of labelOrder) {
    const rows = table.rows.filter(row => row.quality_label === label);
    if (!rows.length) continue;
    const scores = rows.map(row => Number(row.aggregate_score));
    appendText(scoreList, 'li', `${label}: ${scores.map(score => score.toFixed(3)).join(', ')}`);
  }
  panel.appendChild(scoreList);
}
function render() {
  const table = selected();
  if (!table) return;
  renderExplanation(table);
  const columns = [...new Set(table.rows.flatMap(row => Object.keys(row)))].sort();
  const query = document.getElementById('search').value.toLowerCase();
  let rows = table.rows.filter(row => !query || columns.some(column => printable(row[column]).toLowerCase().includes(query)));
  if (sortColumn) rows = rows.slice().sort((left, right) => printable(left[sortColumn]).localeCompare(printable(right[sortColumn]), undefined, {numeric: true}) * sortDirection);
  const size = Number(document.getElementById('page-size').value), pages = Math.max(1, Math.ceil(rows.length / size));
  page = Math.min(page, pages - 1);
  const visible = rows.slice(page * size, (page + 1) * size);
  const head = document.getElementById('head'); head.replaceChildren();
  const headerRow = document.createElement('tr');
  for (const column of columns) {
    const cell = document.createElement('th');
    cell.textContent = column + (sortColumn === column ? (sortDirection > 0 ? ' ▲' : ' ▼') : '');
    cell.addEventListener('click', () => { if (sortColumn === column) sortDirection *= -1; else { sortColumn = column; sortDirection = 1; } render(); });
    headerRow.appendChild(cell);
  }
  head.appendChild(headerRow);
  const body = document.getElementById('body'); body.replaceChildren();
  for (const row of visible) {
    const line = document.createElement('tr');
    for (const column of columns) { const cell = document.createElement('td'); cell.textContent = printable(row[column]); cell.title = cell.textContent; line.appendChild(cell); }
    body.appendChild(line);
  }
  document.getElementById('summary').textContent = `${rows.length.toLocaleString()} matching rows · ${columns.length} columns`;
  document.getElementById('page').textContent = `Page ${page + 1} of ${pages}`;
  document.getElementById('previous').disabled = page === 0;
  document.getElementById('next').disabled = page + 1 >= pages;
}
tableSelect.addEventListener('change', () => { page = 0; sortColumn = ''; render(); });
document.getElementById('search').addEventListener('input', () => { page = 0; render(); });
document.getElementById('page-size').addEventListener('change', () => { page = 0; render(); });
document.getElementById('previous').addEventListener('click', () => { page -= 1; render(); });
document.getElementById('next').addEventListener('click', () => { page += 1; render(); });
render();
</script>
</body>
</html>
"""
    return template.replace("__PAYLOAD__", payload).encode("utf-8")


def _chart_index_html(
    manifest: Mapping[str, object],
    dag: Mapping[str, object],
    stage_rows: Sequence[Mapping[str, object]],
    model_rows: Sequence[Mapping[str, object]],
    endpoint_rows: Sequence[Mapping[str, object]],
) -> bytes:
    endpoint_by_packet: dict[str, str] = {}
    cache_by_packet: dict[str, bool] = {}
    attempts = []
    for item in stage_rows:
        packet_id = str(item.get("packet_id", ""))
        raw_endpoints = item.get("endpoint_ids", ())
        endpoints = (
            [str(value) for value in raw_endpoints]
            if isinstance(raw_endpoints, Sequence) and not isinstance(raw_endpoints, (str, bytes))
            else []
        )
        endpoint_by_packet[packet_id] = ", ".join(endpoints)
        cache_by_packet[packet_id] = bool(item.get("cache_hit"))
        attempts.append(
            {
                "packet_id": packet_id,
                "stage": item.get("stage"),
                "state": item.get("state"),
                "endpoint": endpoint_by_packet[packet_id],
                "cache_hit": cache_by_packet[packet_id],
                "duration_seconds": _number_or(item.get("duration_seconds"), 0.0),
                "tokens": _int_or(item.get("tokens"), 0),
                "started_at": item.get("started_at"),
                "completed_at": item.get("completed_at"),
            }
        )
    calls = []
    for item in model_rows:
        packet_id = str(item.get("packet_id", ""))
        calls.append(
            {
                "call_id": item.get("call_id"),
                "packet_id": packet_id,
                "stage": item.get("stage"),
                "role": item.get("role"),
                "endpoint": endpoint_by_packet.get(packet_id, ""),
                "model": item.get("model"),
                "status": item.get("status"),
                "error_kind": item.get("error_kind"),
                "cache_hit": cache_by_packet.get(packet_id, False),
                "input_tokens": _int_or(item.get("input_tokens"), 0),
                "output_tokens": _int_or(item.get("output_tokens"), 0),
                "total_tokens": _int_or(item.get("total_tokens"), 0),
                "latency_seconds": _number_or(item.get("latency_seconds"), 0.0),
            }
        )
    overview = dag.get("overview", {})
    overview = overview if isinstance(overview, Mapping) else {}
    payload = json.dumps(
        {
            "attempts": attempts,
            "model_calls": calls,
            "endpoints": [dict(item) for item in endpoint_rows],
            "dag_overview": overview,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    charts = manifest.get("charts", [])
    generated_panels: list[str] = []
    omitted_panels: list[str] = []
    comparison_notice = (
        '<section class="comparison-status pending"><h2>Baseline versus optimized</h2>'
        "<strong>Waiting for the paired untouched-test evaluation.</strong>"
        "<p>The optimizer has not produced a final aggregate yet. No benchmark score is "
        "invented from optimization-training or validation cases.</p></section>"
    )
    if isinstance(charts, Sequence) and not isinstance(charts, (str, bytes)):
        for item in charts:
            if not isinstance(item, Mapping):
                continue
            artifact = str(item.get("artifact", ""))
            interactive_artifact = item.get("interactive_artifact")
            preferred_artifact = (
                str(interactive_artifact) if isinstance(interactive_artifact, str) else artifact
            )
            name = Path(preferred_artifact).name
            status = str(item.get("status", "omitted"))
            title = html.escape(str(item.get("title", name)))
            question = html.escape(str(item.get("question", "")))
            description = html.escape(str(item.get("description", "")))
            source = html.escape(str(item.get("source", "")))
            unit = html.escape(str(item.get("unit", "")))
            static_artifact = Path(str(item.get("static_artifact", ""))).name
            if status == "generated":
                if name.endswith(".html"):
                    media = (
                        f'<iframe src="{html.escape(name)}" loading="lazy" '
                        f'title="{title}"></iframe>'
                    )
                else:
                    media = (
                        f'<a href="{html.escape(name)}"><img src="{html.escape(name)}" '
                        f'alt="{title}" loading="lazy"></a>'
                    )
                static_link = (
                    f' · <a href="{html.escape(static_artifact)}">static SVG</a>'
                    if static_artifact and static_artifact != name
                    else ""
                )
                generated_panels.append(
                    '<section class="chart-panel">'
                    f"<h3>{title}</h3><p><strong>{question}</strong></p>"
                    f"<p>{description}</p>{media}"
                    f'<p class="provenance">Source: {source} · unit: {unit}'
                    f' · <a href="{html.escape(name)}">open separately</a>{static_link}</p>'
                    "</section>"
                )
                if static_artifact == "copro-case-paired-score.svg":
                    comparison_notice = (
                        '<section class="comparison-status ready"><h2>Baseline versus optimized</h2>'
                        "<strong>The paired untouched-test comparison is ready.</strong>"
                        f'<p><a href="{html.escape(name)}">Open the per-case comparison</a>.</p>'
                        "</section>"
                    )
            else:
                reason = html.escape(
                    str(item.get("omission_reason", "not applicable")).replace("_", " ")
                )
                omitted_panels.append(
                    '<section class="omitted-panel">'
                    f"<h3>{title}</h3><p>{question}</p>"
                    f"<p><strong>Unavailable:</strong> {reason}.</p>"
                    f'<p class="provenance">Expected source: {source} · unit: {unit}</p>'
                    "</section>"
                )
    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Experiment graph and metric explorer</title>
<style>
:root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
body { margin: 0 auto; max-width: 1180px; padding: 28px; line-height: 1.45; }
h1 { margin: 0 0 6px; font-size: 28px; }
h2 { margin-top: 32px; font-size: 20px; }
p, small { color: #64748b; }
.controls { display: flex; flex-wrap: wrap; gap: 12px; margin: 18px 0; }
label { display: grid; gap: 4px; color: #475569; font-size: 12px; }
select { min-width: 160px; padding: 7px; }
.metrics { display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap: 12px; }
.metric { border: 1px solid #cbd5e1; padding: 12px; }
.metric strong { display: block; font-size: 22px; }
.metric span { color: #64748b; font-size: 12px; }
#scatter { width: 100%; min-height: 520px; border: 1px solid #cbd5e1; background: #fff; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { border-bottom: 1px solid #cbd5e1; padding: 7px; text-align: right; }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
.comparison-status { border: 2px solid; border-radius: 10px; margin: 24px 0; padding: 18px; }
.comparison-status h2 { margin-top: 0; }
.comparison-status.pending { background: #fff7ed; border-color: #f97316; color: #7c2d12; }
.comparison-status.ready { background: #f0fdf4; border-color: #16a34a; color: #14532d; }
.chart-grid { display: grid; gap: 22px; }
.chart-panel { border: 1px solid #cbd5e1; border-radius: 10px; padding: 16px; }
.chart-panel h3, .omitted-panel h3 { margin: 0 0 6px; }
.chart-panel iframe { background: white; border: 0; height: 590px; width: 100%; }
.chart-panel img { background: white; display: block; max-height: 760px; max-width: 100%; object-fit: contain; }
.provenance { font-size: 12px; }
.omitted-grid { display: grid; gap: 10px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.omitted-panel { border: 1px dashed #94a3b8; border-radius: 8px; padding: 14px; }
.muted { color: #64748b; font-size: 12px; }
@media (max-width: 680px) {
  .metrics, .omitted-grid { grid-template-columns: 1fr; }
  body { padding: 16px; }
  .chart-panel iframe { height: 520px; }
}
@media (prefers-color-scheme: dark) {
  body { background: #0f172a; color: #e2e8f0; }
  p, small, label, .muted, .metric span { color: #94a3b8; }
  .metric, #scatter, th, td, .chart-panel, .omitted-panel { border-color: #334155; }
  #scatter { background: #111827; }
  .comparison-status.pending { background: #431407; color: #fed7aa; }
  .comparison-status.ready { background: #052e16; color: #bbf7d0; }
}
</style>
</head>
<body>
<h1>Experiment graph and metric explorer</h1>
<p>Filter, hover, zoom, pan, select, and export the Plotly views below. Static SVG exports and raw
observations remain available for reports, automation, and exact inspection.</p>

__COMPARISON_NOTICE__

<h2>Available experiment charts</h2>
<p>Every generated chart is rendered below. Use “open separately” for a full-window interactive view.</p>
<div class="chart-grid">__GENERATED_CHARTS__</div>

<h2>Call-level latency and token explorer</h2>
<div class="controls">
  <label>Stage<select id="stage"><option value="">All stages</option></select></label>
  <label>Endpoint<select id="endpoint"><option value="">All endpoints</option></select></label>
  <label>Status<select id="status"><option value="">All statuses</option></select></label>
  <label>Cache<select id="cache"><option value="">All</option><option value="executed">Executed</option><option value="reused">Reused</option></select></label>
</div>
<div class="metrics">
  <div class="metric"><strong id="calls">0</strong><span>model calls</span></div>
  <div class="metric"><strong id="tokens">0</strong><span>total tokens</span></div>
  <div class="metric"><strong id="p50">0s</strong><span>p50 latency</span></div>
  <div class="metric"><strong id="p95">0s</strong><span>p95 latency</span></div>
</div>
<h3>Call latency versus total tokens</h3>
<p class="muted">Each point is one model call. Token position uses a log scale. Hover for provenance; drag to zoom or select.</p>
<div id="scatter" role="img" aria-label="Filtered model-call latency versus total tokens"></div>
<p id="table-note" class="muted"></p>
<table>
<thead><tr><th>Call</th><th>Stage / endpoint</th><th>Status</th><th>Input</th><th>Output</th><th>Total</th><th>Latency (s)</th></tr></thead>
<tbody id="call-rows"></tbody>
</table>

<h2>Charts waiting for evidence</h2>
<p>These are explicit missing-data states, not zero scores.</p>
<div class="omitted-grid">__OMITTED_CHARTS__</div>

<script src="https://cdn.plot.ly/plotly-__PLOTLY_VERSION__.min.js" charset="utf-8"></script>
<script id="data" type="application/json">__PAYLOAD__</script>
<script>
const data = JSON.parse(document.getElementById('data').textContent);
const selectors = ['stage', 'endpoint', 'status'];
const unique = (key) => [...new Set(data.model_calls.map(row => row[key]).filter(Boolean))].sort();
for (const key of selectors) {
  const select = document.getElementById(key);
  for (const value of unique(key)) {
    const option = document.createElement('option'); option.value = value; option.textContent = value;
    select.appendChild(option);
  }
  select.addEventListener('change', update);
}
document.getElementById('cache').addEventListener('change', update);
const number = new Intl.NumberFormat();
function quantile(values, q) {
  if (!values.length) return 0;
  values = values.slice().sort((a, b) => a - b);
  const p = (values.length - 1) * q, lo = Math.floor(p), hi = Math.ceil(p);
  return values[lo] + (values[hi] - values[lo]) * (p - lo);
}
function filtered() {
  const stage = document.getElementById('stage').value;
  const endpoint = document.getElementById('endpoint').value;
  const status = document.getElementById('status').value;
  const cache = document.getElementById('cache').value;
  return data.model_calls.filter(row => (!stage || row.stage === stage) &&
    (!endpoint || row.endpoint === endpoint) && (!status || row.status === status) &&
    (!cache || (cache === 'reused') === Boolean(row.cache_hit)));
}
function draw(rows) {
  if (!window.Plotly) {
    document.getElementById('scatter').textContent = 'Plotly could not load. Use the static SVG exports below.';
    return;
  }
  const stages = [...new Set(rows.map(row => row.stage || 'unknown'))].sort();
  const traces = stages.map(stage => {
    const values = rows.filter(row => (row.stage || 'unknown') === stage);
    return {
      name: stage,
      type: 'scattergl',
      mode: 'markers',
      x: values.map(row => Math.max(1, row.total_tokens)),
      y: values.map(row => row.latency_seconds),
      customdata: values.map(row => [row.call_id, row.endpoint || '—', row.status, row.model || '—', row.input_tokens, row.output_tokens]),
      marker: {size: 8, opacity: .7},
      hovertemplate: '%{customdata[0]}<br>stage=' + stage + '<br>endpoint=%{customdata[1]}<br>status=%{customdata[2]}<br>model=%{customdata[3]}<br>input=%{customdata[4]:,}<br>output=%{customdata[5]:,}<br>total=%{x:,}<br>latency=%{y:.3f}s<extra></extra>'
    };
  });
  Plotly.react('scatter', traces, {
    template: 'plotly_white',
    height: 520,
    margin: {l: 80, r: 30, t: 35, b: 70},
    hovermode: 'closest',
    legend: {orientation: 'h'},
    xaxis: {title: 'Total tokens', type: 'log'},
    yaxis: {title: 'Latency (seconds)', rangemode: 'tozero'}
  }, {displaylogo: false, responsive: true, scrollZoom: true, toImageButtonOptions: {format: 'svg', filename: 'latency-vs-tokens'}});
}
function update() {
  const rows = filtered(), latencies = rows.map(row => row.latency_seconds);
  document.getElementById('calls').textContent = number.format(rows.length);
  document.getElementById('tokens').textContent = number.format(rows.reduce((sum, row) => sum + row.total_tokens, 0));
  document.getElementById('p50').textContent = `${quantile(latencies, .5).toFixed(2)}s`;
  document.getElementById('p95').textContent = `${quantile(latencies, .95).toFixed(2)}s`;
  draw(rows);
  const visible = rows.slice().sort((a, b) => b.latency_seconds - a.latency_seconds).slice(0, 250);
  document.getElementById('table-note').textContent = `Showing ${visible.length} of ${rows.length} matching calls, ordered by latency.`;
  const tbody = document.getElementById('call-rows'); tbody.replaceChildren();
  for (const row of visible) {
    const tr = document.createElement('tr');
    const values = [String(row.call_id).slice(0, 12), `${row.stage} / ${row.endpoint || '—'}`, row.status,
      number.format(row.input_tokens), number.format(row.output_tokens), number.format(row.total_tokens), row.latency_seconds.toFixed(3)];
    for (const value of values) { const td = document.createElement('td'); td.textContent = value; tr.appendChild(td); }
    tbody.appendChild(tr);
  }
}
update();
</script>
</body>
</html>
"""
    return (
        template.replace("__COMPARISON_NOTICE__", comparison_notice)
        .replace("__GENERATED_CHARTS__", "\n".join(generated_panels))
        .replace("__OMITTED_CHARTS__", "\n".join(omitted_panels))
        .replace("__PAYLOAD__", payload)
        .replace("__PLOTLY_VERSION__", get_plotlyjs_version())
        .encode("utf-8")
    )


def _svg_header(width: int, height: int, title: str = "Chart", description: str = "") -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="chart-title chart-description">'
        f'<title id="chart-title">{html.escape(title)}</title>'
        f'<desc id="chart-description">{html.escape(description)}</desc>'
        '<rect width="100%" height="100%" fill="white"/>'
    )


def _svg_heading(title: str, description: str) -> tuple[str, ...]:
    visible_description = description
    if len(visible_description) > 145:
        visible_description = f"{visible_description[:142].rstrip()}…"
    return (
        f'<text x="24" y="30" font-size="20" font-weight="bold">{html.escape(title)}</text>',
        f'<text x="24" y="55" font-size="11" fill="#475569">'
        f"{html.escape(visible_description)}</text>",
    )


def _duration(status: StageStatus) -> float:
    if status.result is None:
        return 0.0
    started = status.result.timing.get("started_at")
    completed = status.result.timing.get("completed_at")
    try:
        return max(0.0, (_parse_timestamp(completed) - _parse_timestamp(started)).total_seconds())
    except (TypeError, ValueError):
        return _number_or(status.result.usage.get("latency_seconds"), 0.0)


def _reference_from_mapping(value: object) -> ArtifactReference:
    item = _mapping(value, "artifact reference")
    size = item.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise IntegrityError("artifact reference size_bytes must be non-negative")
    return ArtifactReference(
        _string(item.get("content_id"), "artifact content_id"),
        _string(item.get("media_type"), "artifact media_type"),
        size,
        _string(item.get("relative_path"), "artifact relative_path"),
    )


def _load_mapping(path: Path, label: str) -> Mapping[str, object]:
    if not path.is_file() or path.is_symlink():
        raise IntegrityError(f"{label} is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IntegrityError(f"{label} is invalid: {path}") from error
    return _mapping(value, label)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IntegrityError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntegrityError(f"{field} must be a non-empty string")
    return value.strip()


def _int_or(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _number_or(value: object, default: float) -> float:
    return (
        float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    )


def _usage_token(item: Mapping[str, object], *keys: str) -> int:
    usage = item.get("usage")
    if not isinstance(usage, Mapping):
        return 0
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _nested_value(item: Mapping[str, object], field: str, key: str) -> object:
    value = item.get(field)
    return value.get(key) if isinstance(value, Mapping) else None


def _timestamp_ms(value: object) -> int:
    return int(_parse_timestamp(value).timestamp() * 1000)


def _timestamp_ms_or(value: object, default: int) -> int:
    try:
        return _timestamp_ms(value)
    except (TypeError, ValueError):
        return default


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _media_type(path: Path) -> str:
    return {
        ".json": "application/json",
        ".csv": "text/csv",
        ".md": "text/markdown",
        ".svg": "image/svg+xml",
        ".html": "text/html",
    }.get(path.suffix.lower(), "application/octet-stream")


def _textual(media_type: str) -> bool:
    return media_type.startswith("text/") or media_type in {
        "application/json",
        "application/yaml",
        "image/svg+xml",
    }


def _assert_no_secret(value: object, label: str) -> None:
    _assert_no_secret_bytes(canonical_json_bytes(value), label)


def _assert_no_secret_bytes(value: bytes, label: str) -> None:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        raise RecordingError(
            f"content policy blocked a possible credential in {label}; "
            "remove the secret or select hashes_only/summary"
        )
