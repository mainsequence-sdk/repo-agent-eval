from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from threading import RLock
from typing import Any

from ms_agent_eval.core.errors import IntegrityError, RecordingError
from ms_agent_eval.core.lifecycle import RunLifecycleState
from ms_agent_eval.core.recorder import (
    ExperimentDescriptor,
    ExternalBinding,
    RecorderCapabilities,
    RecordingPlan,
    RunDescriptor,
    ScopeDescriptor,
    TableArtifact,
    TraceDescriptor,
)
from ms_agent_eval.core.workspace import RecorderConfiguration


_MLFLOW_ENVIRONMENT_LOCK = RLock()


class MlflowRecorder:
    """Project one portable recording plan through MLflow's explicit client API."""

    def __init__(
        self,
        configuration: RecorderConfiguration,
        environment: Mapping[str, str],
        *,
        client: Any | None = None,
    ) -> None:
        self.configuration = configuration
        self.environment = environment
        self.store = None
        self._secret_values: tuple[str, ...] = ()
        self._auth_values = self._resolve_authentication()
        if client is None:
            try:
                from mlflow import MlflowClient
            except ImportError as error:
                raise RecordingError(
                    "the MLflow recorder requires the optional dependency; "
                    "install it with `uv sync --extra mlflow`"
                ) from error
            with self._authenticated():
                client = MlflowClient(tracking_uri=configuration.tracking_uri)
        self.client = client

    @property
    def capabilities(self) -> RecorderCapabilities:
        return RecorderCapabilities(
            nested_runs=True,
            metrics=True,
            tables=True,
            artifacts=True,
            traces=True,
            trace_links=True,
        )

    def healthcheck(self, *, write_test: bool = False) -> Mapping[str, object]:
        with self._authenticated():
            experiments = self.client.search_experiments(max_results=1)
            try:
                import mlflow

                client_version = mlflow.__version__
            except (ImportError, AttributeError):
                client_version = "unknown"
            result: dict[str, object] = {
                "reachable": True,
                "client_version": client_version,
                "readable": True,
                "write_test": "skipped",
                "visible_experiment_count": len(experiments),
            }
            if not write_test:
                return result
            name = self.configuration.resolved_experiment_name("doctor", "doctor")
            descriptor = ExperimentDescriptor(
                name=name,
                tags={"agent_eval.doctor": "true"},
            )
            binding = ExternalBinding.create(
                recorder_id=self.configuration.id,
                recorder_config_hash=self.configuration.configuration_hash,
                local_run_id="doctor",
            )
            experiment_id = self.ensure_experiment(descriptor, binding)
            run = self.client.create_run(
                experiment_id,
                tags={
                    "mlflow.runName": "ms-agent-eval recorder doctor",
                    "agent_eval.doctor": "true",
                },
            )
            run_id = str(run.info.run_id)
            try:
                self.client.log_text(run_id, "ms-agent-eval\n", "doctor/probe.txt")
                self.client.log_table(
                    run_id,
                    data={"check": ["table"], "value": [1]},
                    artifact_file="doctor/probe-table.json",
                )
                listed = self.client.list_artifacts(run_id, "doctor")
                if not any(item.path == "doctor/probe.txt" for item in listed):
                    raise RecordingError("MLflow artifact probe was not readable")
                with tempfile.TemporaryDirectory(prefix="ms-agent-eval-mlflow-") as temporary:
                    downloaded = self.client.download_artifacts(
                        run_id,
                        "doctor/probe.txt",
                        temporary,
                    )
                    if Path(downloaded).read_text(encoding="utf-8") != "ms-agent-eval\n":
                        raise RecordingError("MLflow artifact probe bytes changed")
                root = self.client.start_trace(
                    "ms-agent-eval-recorder-doctor",
                    span_type="CHAIN",
                    inputs={"probe": True},
                    attributes={"agent_eval.doctor": "true"},
                    experiment_id=experiment_id,
                    run_id=run_id,
                )
                self.client.end_trace(root.trace_id, outputs={"ready": True})
                self.client.get_trace(root.trace_id, display=False, flush=True)
                result.update(
                    {
                        "write_test": "passed",
                        "artifact_upload": True,
                        "artifact_download": True,
                        "table": True,
                        "trace": True,
                    }
                )
                self.client.set_terminated(run_id, status="FINISHED")
            except BaseException:
                try:
                    self.client.set_terminated(run_id, status="FAILED")
                except Exception:
                    pass
                raise
            finally:
                try:
                    self.client.delete_run(run_id)
                except Exception:
                    pass
            return result

    def ensure_experiment(
        self,
        descriptor: ExperimentDescriptor,
        binding: ExternalBinding,
    ) -> str:
        with self._authenticated():
            if binding.external_experiment_id is not None:
                experiment = self.client.get_experiment(binding.external_experiment_id)
                if experiment is None or experiment.name != descriptor.name:
                    raise IntegrityError("bound MLflow experiment does not match the plan")
                return str(experiment.experiment_id)
            experiment = self.client.get_experiment_by_name(descriptor.name)
            if experiment is None:
                try:
                    experiment_id = str(self.client.create_experiment(descriptor.name))
                except Exception:
                    experiment = self.client.get_experiment_by_name(descriptor.name)
                    if experiment is None:
                        raise
                    experiment_id = str(experiment.experiment_id)
            else:
                experiment_id = str(experiment.experiment_id)
            for key, value in sorted(descriptor.tags.items()):
                if value is not None:
                    self.client.set_experiment_tag(experiment_id, key, _string_value(value))
            return experiment_id

    def ensure_run(
        self,
        descriptor: RunDescriptor,
        binding: ExternalBinding,
    ) -> str:
        with self._authenticated():
            if binding.external_experiment_id is None:
                raise IntegrityError("MLflow experiment must be bound before its run")
            if binding.external_run_id is not None:
                run = self.client.get_run(binding.external_run_id)
                local_id = run.data.tags.get("agent_eval.local_run_id")
                if local_id != descriptor.local_run_id:
                    raise IntegrityError("bound MLflow run does not match the local run")
                return str(run.info.run_id)
            existing = self._find_run(
                binding.external_experiment_id,
                local_run_id=descriptor.local_run_id,
                scope_id=None,
            )
            if existing is not None:
                return str(existing.info.run_id)
            tags = _tags(descriptor.tags)
            tags.update(
                {
                    "mlflow.runName": descriptor.name,
                    "agent_eval.local_run_id": descriptor.local_run_id,
                    "agent_eval.recorder_config_hash": binding.recorder_config_hash,
                }
            )
            run = self.client.create_run(
                binding.external_experiment_id,
                start_time=descriptor.start_time_ms,
                tags=tags,
            )
            return str(run.info.run_id)

    def sync(
        self,
        plan: RecordingPlan,
        binding: ExternalBinding,
    ) -> Mapping[str, object]:
        with self._authenticated():
            if binding.external_run_id is None or binding.external_experiment_id is None:
                raise IntegrityError("MLflow root run is not bound")
            root_id = binding.external_run_id
            for key, value in sorted(plan.run.tags.items()):
                if value is not None:
                    self.client.set_tag(root_id, key, _string_value(value))
            for key, value in sorted(plan.run.parameters.items()):
                if value is not None:
                    self.client.log_param(root_id, key, _string_value(value))
            self._ensure_scopes(plan.scopes, binding)

            delivered = {
                "metrics": 0,
                "tables": 0,
                "artifacts": 0,
                "traces": 0,
                "scopes": len(binding.scopes),
            }
            for metric in plan.metrics:
                if metric.id in binding.delivered_items:
                    continue
                target = self._target_run(metric.scope_id, binding)
                self.client.log_metric(
                    target,
                    metric.key,
                    metric.value,
                    timestamp=metric.timestamp_ms,
                    step=metric.step,
                )
                binding.delivered_items.add(metric.id)
                delivered["metrics"] += 1
            for table in plan.tables:
                if table.id in binding.delivered_items:
                    continue
                target = self._target_run(table.scope_id, binding)
                self.client.log_table(
                    target,
                    data=_table_data(table),
                    artifact_file=(f"tables/{table.name}/{table.id.removeprefix('sha256:')}.json"),
                )
                binding.delivered_items.add(table.id)
                delivered["tables"] += 1
            for artifact in plan.artifacts:
                if artifact.id in binding.delivered_items:
                    continue
                target = self._target_run(artifact.scope_id, binding)
                self.client.log_stream(
                    target,
                    BytesIO(self._artifact_bytes(artifact.reference)),
                    artifact.path,
                )
                binding.delivered_items.add(artifact.id)
                delivered["artifacts"] += 1
            for trace in plan.traces:
                if trace.id in binding.delivered_items:
                    continue
                trace_id, root_span_id = self._log_trace(trace, binding)
                packet_id = str(trace.attributes["agent_eval.packet_id"])
                binding.traces[packet_id] = trace_id
                binding.trace_spans[packet_id] = root_span_id
                binding.delivered_items.add(trace.id)
                delivered["traces"] += 1
            self.client.set_tag(root_id, "agent_eval.recording_plan_id", plan.plan_id)
            self.client.set_tag(
                root_id,
                "agent_eval.recording_source_sequence",
                str(plan.source_sequence),
            )
            self.client.set_tag(
                root_id,
                "agent_eval.recording_omissions",
                str(len(plan.omissions)),
            )
            return delivered

    def finalize(
        self,
        binding: ExternalBinding,
        status: RunLifecycleState,
        end_time_ms: int,
    ) -> Mapping[str, object]:
        with self._authenticated():
            if binding.external_run_id is None:
                raise IntegrityError("cannot finalize an unbound MLflow run")
            mlflow_status = {
                RunLifecycleState.COMPLETED: "FINISHED",
                RunLifecycleState.FAILED: "FAILED",
                RunLifecycleState.CANCELLED: "KILLED",
            }[status]
            for scope_id in sorted(binding.scopes, reverse=True):
                self.client.set_terminated(
                    binding.scopes[scope_id],
                    status=mlflow_status,
                    end_time=end_time_ms,
                )
            self.client.set_terminated(
                binding.external_run_id,
                status=mlflow_status,
                end_time=end_time_ms,
            )
            return {"status": mlflow_status, "end_time_ms": end_time_ms}

    def _ensure_scopes(
        self,
        scopes: Sequence[ScopeDescriptor],
        binding: ExternalBinding,
    ) -> None:
        assert binding.external_experiment_id is not None
        assert binding.external_run_id is not None
        for scope in scopes:
            if scope.id in binding.scopes:
                self.client.get_run(binding.scopes[scope.id])
                continue
            existing = self._find_run(
                binding.external_experiment_id,
                local_run_id=binding.local_run_id,
                scope_id=scope.id,
            )
            if existing is not None:
                binding.scopes[scope.id] = str(existing.info.run_id)
                continue
            parent = (
                binding.external_run_id
                if scope.parent_id is None
                else binding.scopes.get(scope.parent_id)
            )
            if parent is None:
                raise IntegrityError(f"MLflow parent scope is missing for {scope.id!r}")
            tags = _tags(scope.tags)
            tags.update(
                {
                    "mlflow.runName": scope.name,
                    "mlflow.parentRunId": parent,
                    "agent_eval.local_run_id": binding.local_run_id,
                    "agent_eval.scope_id": scope.id,
                    "agent_eval.recorder_config_hash": binding.recorder_config_hash,
                }
            )
            run = self.client.create_run(
                binding.external_experiment_id,
                tags=tags,
            )
            binding.scopes[scope.id] = str(run.info.run_id)

    def _find_run(
        self,
        experiment_id: str,
        *,
        local_run_id: str,
        scope_id: str | None,
    ) -> Any | None:
        clauses = [
            f"tags.`agent_eval.local_run_id` = '{_filter_value(local_run_id)}'",
            "tags.`agent_eval.recorder_config_hash` = "
            f"'{_filter_value(self.configuration.configuration_hash)}'",
        ]
        runs = self.client.search_runs(
            [experiment_id],
            filter_string=" and ".join(clauses),
            max_results=1000,
        )
        runs = [run for run in runs if run.data.tags.get("agent_eval.scope_id") == scope_id]
        if len(runs) > 1:
            raise IntegrityError("multiple MLflow runs match one canonical local identity")
        return runs[0] if runs else None

    def _target_run(self, scope_id: str | None, binding: ExternalBinding) -> str:
        if scope_id is None:
            assert binding.external_run_id is not None
            return binding.external_run_id
        try:
            return binding.scopes[scope_id]
        except KeyError as error:
            raise IntegrityError(f"MLflow scope is not bound: {scope_id}") from error

    def _log_trace(self, trace: TraceDescriptor, binding: ExternalBinding) -> tuple[str, str]:
        assert binding.external_experiment_id is not None
        # MLflow's tracing provider is process-global even when MlflowClient
        # was constructed with an explicit tracking URI. Reinitialize it while
        # holding _MLFLOW_ENVIRONMENT_LOCK so an earlier recorder cannot send
        # this trace to its store.
        import mlflow
        from mlflow.entities.trace_location import MlflowExperimentLocation

        mlflow.tracing.set_destination(
            MlflowExperimentLocation(experiment_id=binding.external_experiment_id)
        )
        target = self._target_run(trace.scope_id, binding)
        try:
            from mlflow.entities import Link
        except ImportError as error:
            raise RecordingError("installed MLflow client does not support trace links") from error
        links = []
        for link in trace.links:
            dependency = link.get("packet_id")
            if (
                link.get("type") == "dependency"
                and isinstance(dependency, str)
                and dependency in binding.traces
                and dependency in binding.trace_spans
            ):
                links.append(
                    Link(
                        trace_id=binding.traces[dependency],
                        span_id=binding.trace_spans[dependency],
                        attributes={"agent_eval.link_type": "dependency"},
                    )
                )
        attributes = {
            **dict(trace.attributes),
            "agent_eval.links": json.dumps(trace.links, sort_keys=True, separators=(",", ":")),
        }
        try:
            root = self.client.start_trace(
                trace.name,
                span_type="CHAIN",
                inputs=trace.inputs,
                attributes=_attributes(attributes),
                tags={
                    "agent_eval.trace_id": trace.id,
                    "agent_eval.local_run_id": binding.local_run_id,
                    "agent_eval.scope_id": trace.scope_id,
                },
                experiment_id=binding.external_experiment_id,
                run_id=target,
                links=links,
            )
            try:
                for span in trace.spans:
                    child = self.client.start_span(
                        span.name,
                        trace_id=root.trace_id,
                        parent_id=root.span_id,
                        span_type=span.type,
                        inputs=span.inputs,
                        attributes=_attributes(span.attributes),
                    )
                    self.client.end_span(
                        trace_id=child.trace_id,
                        span_id=child.span_id,
                        outputs=span.outputs,
                        status=(
                            "ERROR"
                            if span.attributes.get("agent_eval.status") == "failed"
                            else "OK"
                        ),
                    )
                self.client.end_trace(
                    root.trace_id,
                    outputs=trace.outputs,
                    status="ERROR" if trace.status == "failed" else "OK",
                )
                self.client.get_trace(root.trace_id, display=False, flush=True)
            except BaseException:
                try:
                    self.client.end_trace(root.trace_id, outputs={"status": "recording_failed"})
                except Exception:
                    pass
                raise
            return str(root.trace_id), str(root.span_id)
        finally:
            # Retire the provider so another recorder instance can choose a
            # different tracking store without inheriting this destination.
            mlflow.tracing.reset()

    def _artifact_bytes(self, reference) -> bytes:  # type: ignore[no-untyped-def]
        if self.store is None:
            raise IntegrityError("MLflow adapter has no canonical artifact-store binding")
        with self.store.get_blob(reference) as handle:
            return handle.read()

    def bind_artifact_store(self, store) -> None:  # type: ignore[no-untyped-def]
        """Bind the canonical store without adding it to the public adapter contract."""

        self.store = store

    def _resolve_authentication(self) -> Mapping[str, str]:
        authentication = self.configuration.authentication
        # MlflowClient receives the URI explicitly, but MLflow's asynchronous
        # trace exporter consults the standard environment setting. Keep both
        # paths on the exact same destination for the duration of each call.
        values: dict[str, str] = {
            "MLFLOW_TRACKING_URI": self.configuration.tracking_uri,
            # A recorder receipt must mean the trace is durable.  Synchronous
            # export also avoids MLflow's process-global async exporter keeping
            # the tracking URI from an earlier recorder instance.
            "MLFLOW_ENABLE_ASYNC_TRACE_LOGGING": "false",
        }
        secrets: list[str] = []
        if authentication.type == "basic":
            assert authentication.username_env is not None
            assert authentication.password_env is not None
            username = self.environment.get(authentication.username_env, "").strip()
            password = self.environment.get(authentication.password_env, "").strip()
            if not username or not password:
                raise RecordingError(
                    "MLflow basic authentication credential environment variables are missing"
                )
            values = {
                **values,
                "MLFLOW_TRACKING_USERNAME": username,
                "MLFLOW_TRACKING_PASSWORD": password,
            }
            secrets.extend((username, password))
        elif authentication.type == "bearer":
            assert authentication.token_env is not None
            token = self.environment.get(authentication.token_env, "").strip()
            if not token:
                raise RecordingError(
                    "MLflow bearer authentication credential environment variable is missing"
                )
            values["MLFLOW_TRACKING_TOKEN"] = token
            secrets.append(token)
        self._secret_values = tuple(secrets)
        return values

    @contextmanager
    def _authenticated(self):  # type: ignore[no-untyped-def]
        with _MLFLOW_ENVIRONMENT_LOCK:
            previous = {key: os.environ.get(key) for key in self._auth_values}
            os.environ.update(self._auth_values)
            try:
                yield
            except Exception as error:
                message = str(error)
                for secret in self._secret_values:
                    message = message.replace(secret, "[REDACTED]")
                if isinstance(error, (IntegrityError, RecordingError)):
                    raise type(error)(message) from error
                raise RecordingError(
                    f"MLflow operation failed ({type(error).__name__}): {message}"
                ) from error
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


def _tags(values: Mapping[str, object]) -> dict[str, str]:
    return {str(key): _string_value(value) for key, value in values.items() if value is not None}


def _attributes(values: Mapping[str, object]) -> dict[str, str]:
    return _tags(values)


def _string_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _table_data(table: TableArtifact) -> Mapping[str, Sequence[object]]:
    columns = sorted({str(key) for row in table.rows for key in row})
    return {column: [_table_value(row.get(column)) for row in table.rows] for column in columns}


def _table_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
