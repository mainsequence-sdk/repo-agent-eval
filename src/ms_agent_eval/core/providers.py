from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

from .hashing import canonical_json_bytes, content_hash, json_value
from .models import ArtifactReference, ModelCallRecord
from .storage import ArtifactStore


def _redacted_message(error: BaseException, secret_values: Sequence[str]) -> str:
    message = str(error)
    for secret in secret_values:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message


def failure_kind(error: Exception) -> str:
    chain: list[BaseException] = []
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited and len(chain) < 8:
        visited.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    text = " ".join(f"{type(item).__name__} {item}" for item in chain).lower()
    status_codes = [getattr(item, "status_code", None) for item in chain]
    if any(
        token in text
        for token in (
            "contextwindowexceeded",
            "context window",
            "context_length_exceeded",
            "maximum context length",
            "context size",
            "too many tokens",
        )
    ):
        return "context_window_exceeded"
    if (
        any(code in {401, 403} for code in status_codes)
        or "authentication" in text
        or "unauthorized" in text
    ):
        return "authentication_error"
    if 429 in status_codes or "rate limit" in text:
        return "rate_limit_error"
    if any(token in text for token in ("connection", "connecterror", "timeout", "dns")):
        return "connection_error"
    if any(isinstance(code, int) and code >= 500 for code in status_codes):
        return "provider_server_error"
    return type(error).__name__.lower()


def failure_details(
    error: Exception,
    *,
    secret_values: Sequence[str] = (),
    parameters: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """Return a redacted, structured diagnostic without serializing exceptions."""

    chain: list[Mapping[str, object]] = []
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited and len(chain) < 8:
        visited.add(id(current))
        item: dict[str, object] = {
            "exception_type": type(current).__name__,
            "message": _redacted_message(current, secret_values),
        }
        for attribute in ("status_code", "code", "errno"):
            value = getattr(current, attribute, None)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                rendered = str(value)
                for secret in secret_values:
                    if secret:
                        rendered = rendered.replace(secret, "[REDACTED]")
                item[attribute] = rendered if isinstance(value, str) else value
        chain.append(item)
        current = current.__cause__ or current.__context__

    category = failure_kind(error)
    outer = chain[0]
    root = chain[-1]
    endpoint = None
    if parameters is not None and isinstance(parameters.get("api_base"), str):
        endpoint = str(parameters["api_base"])
    return {
        "category": category,
        "exception_type": outer["exception_type"],
        "message": outer["message"],
        "root_exception_type": root["exception_type"],
        "root_message": root["message"],
        "status_code": next((item["status_code"] for item in chain if "status_code" in item), None),
        "error_code": next((item["code"] for item in chain if "code" in item), None),
        "errno": next((item["errno"] for item in reversed(chain) if "errno" in item), None),
        "retryable": category in {"connection_error", "provider_server_error", "rate_limit_error"},
        "endpoint_base_url": endpoint,
        "exception_chain": chain,
    }


class ModelCallObserver:
    """Persist observed DSPy calls for exactly one locked LLM role."""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        role: str,
        run_id: str | None = None,
        packet_id: str | None = None,
        work_id: str | None = None,
        attempt_id: str | None = None,
        experiment_revision_id: str | None = None,
        stage: str | None = None,
    ) -> None:
        if role not in {"author", "optimizer", "solver", "judge"}:
            raise ValueError(f"invalid LLM role: {role}")
        context = (
            run_id,
            packet_id,
            work_id,
            attempt_id,
            experiment_revision_id,
            stage,
        )
        if any(item is None for item in context) and any(item is not None for item in context):
            raise ValueError("model-call persistence context must be complete or omitted")
        self.store = store
        self.role = role
        self.run_id = run_id
        self.packet_id = packet_id
        self.work_id = work_id
        self.attempt_id = attempt_id
        self.experiment_revision_id = experiment_revision_id
        self.stage = stage
        self.records: list[ModelCallRecord] = []
        self.trace_artifacts: list[ArtifactReference] = []
        self.endpoint_routes: list[Mapping[str, object]] = []

    def record_endpoint_route(self, route: Mapping[str, object]) -> None:
        """Attach transport/preflight provenance to this observed call stream."""

        self.endpoint_routes.append(dict(route))

    def completed(
        self,
        *,
        provider_id: str,
        model: str,
        parameters: Mapping[str, object],
        messages: Sequence[Mapping[str, object]],
        request: Mapping[str, object],
        response: Mapping[str, object],
        usage: Mapping[str, object],
        latency_seconds: float,
        configured_cost: float,
    ) -> ModelCallRecord:
        record = ModelCallRecord(
            call_id=str(uuid4()),
            role=self.role,  # type: ignore[arg-type]
            provider_id=provider_id,
            model=model,
            parameters=dict(parameters),
            rendered_messages=tuple(messages),
            request_artifact=self._json_artifact(request),
            response_artifact=self._json_artifact(response),
            usage=dict(usage),
            status="completed",
            error_kind=None,
            error=None,
            recorded_at=datetime.now(UTC).isoformat(),
            latency_seconds=latency_seconds,
            configured_cost=configured_cost,
        )
        self._persist(record)
        self.records.append(record)
        return record

    def failed(
        self,
        *,
        provider_id: str,
        model: str,
        parameters: Mapping[str, object],
        messages: Sequence[Mapping[str, object]],
        request: Mapping[str, object],
        error: Exception,
        secret_values: Sequence[str] = (),
        latency_seconds: float,
        configured_cost: float,
    ) -> ModelCallRecord:
        error_kind = failure_kind(error)
        diagnostic = failure_details(
            error,
            secret_values=secret_values,
            parameters=parameters,
        )
        record = ModelCallRecord(
            call_id=str(uuid4()),
            role=self.role,  # type: ignore[arg-type]
            provider_id=provider_id,
            model=model,
            parameters=dict(parameters),
            rendered_messages=tuple(messages),
            request_artifact=self._json_artifact(request),
            response_artifact=self._json_artifact(diagnostic),
            usage={},
            status="failed",
            error_kind=error_kind,
            error=diagnostic,
            recorded_at=datetime.now(UTC).isoformat(),
            latency_seconds=latency_seconds,
            configured_cost=configured_cost,
        )
        self._persist(record)
        self.records.append(record)
        return record

    def record_trace(
        self,
        artifact: ArtifactReference,
        calls: Sequence[ModelCallRecord],
    ) -> None:
        """Persist a DSPy trace reference before its enclosing stage can fail."""

        self.trace_artifacts.append(artifact)
        if self.run_id is None:
            return
        attempt_digest = self.attempt_id.removeprefix("sha256:")
        manifest = {
            "schema_version": 1,
            "run_id": self.run_id,
            "packet_id": self.packet_id,
            "work_id": self.work_id,
            "attempt_id": self.attempt_id,
            "experiment_revision_id": self.experiment_revision_id,
            "stage": self.stage,
            "trace_artifact": json_value(artifact),
            "model_call_ids": [item.call_id for item in calls],
        }
        observation_digest = content_hash(manifest).removeprefix("sha256:")
        self.store.put_manifest(
            f"runs/{self.run_id}/traces/{attempt_digest}-{observation_digest}",
            manifest,
        )

    def _persist(self, record: ModelCallRecord) -> None:
        if self.run_id is None:
            return
        self.store.put_manifest(
            f"runs/{self.run_id}/model-calls/{record.call_id}",
            {
                "schema_version": 2,
                "run_id": self.run_id,
                "packet_id": self.packet_id,
                "work_id": self.work_id,
                "attempt_id": self.attempt_id,
                "experiment_revision_id": self.experiment_revision_id,
                "stage": self.stage,
                "call_id": record.call_id,
                "role": record.role,
                "provider_id": record.provider_id,
                "model": record.model,
                "parameters": dict(record.parameters),
                "request_artifact": json_value(record.request_artifact),
                "response_artifact": json_value(record.response_artifact),
                "usage": dict(record.usage),
                "status": record.status,
                "error_kind": record.error_kind,
                "error": None if record.error is None else dict(record.error),
                "recorded_at": record.recorded_at,
                "latency_seconds": record.latency_seconds,
                "configured_cost": record.configured_cost,
            },
        )

    def _json_artifact(self, value: object) -> ArtifactReference:
        return self.store.put_blob(BytesIO(canonical_json_bytes(value)), "application/json")
