from __future__ import annotations

from collections.abc import Mapping, Sequence
from io import BytesIO
from uuid import uuid4

from .hashing import canonical_json_bytes, json_value
from .models import ArtifactReference, ModelCallRecord
from .storage import ArtifactStore


def _redacted_message(error: Exception, secret_values: Sequence[str]) -> str:
    message = str(error)
    for secret in secret_values:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message


def _failure_kind(error: Exception) -> str:
    text = f"{type(error).__name__} {error}".lower()
    status_code = getattr(error, "status_code", None)
    if status_code in {401, 403} or "authentication" in text or "unauthorized" in text:
        return "authentication_error"
    if status_code == 429 or "rate limit" in text:
        return "rate_limit_error"
    if isinstance(status_code, int) and status_code >= 500:
        return "provider_server_error"
    if any(token in text for token in ("connection", "connecterror", "timeout", "dns")):
        return "connection_error"
    return type(error).__name__


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
        if role not in {"author", "solver", "judge"}:
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
        error_kind = _failure_kind(error)
        record = ModelCallRecord(
            call_id=str(uuid4()),
            role=self.role,  # type: ignore[arg-type]
            provider_id=provider_id,
            model=model,
            parameters=dict(parameters),
            rendered_messages=tuple(messages),
            request_artifact=self._json_artifact(request),
            response_artifact=self._json_artifact(
                {"error_kind": error_kind, "message": _redacted_message(error, secret_values)}
            ),
            usage={},
            status="failed",
            error_kind=error_kind,
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
        digest = artifact.content_id.removeprefix("sha256:")
        self.store.put_manifest(
            f"runs/{self.run_id}/traces/{digest}",
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "packet_id": self.packet_id,
                "work_id": self.work_id,
                "attempt_id": self.attempt_id,
                "experiment_revision_id": self.experiment_revision_id,
                "stage": self.stage,
                "trace_artifact": json_value(artifact),
                "model_call_ids": [item.call_id for item in calls],
            },
        )

    def _persist(self, record: ModelCallRecord) -> None:
        if self.run_id is None:
            return
        self.store.put_manifest(
            f"runs/{self.run_id}/model-calls/{record.call_id}",
            {
                "schema_version": 1,
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
                "latency_seconds": record.latency_seconds,
                "configured_cost": record.configured_cost,
            },
        )

    def _json_artifact(self, value: object) -> ArtifactReference:
        return self.store.put_blob(BytesIO(canonical_json_bytes(value)), "application/json")
