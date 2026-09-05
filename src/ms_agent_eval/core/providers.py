from __future__ import annotations

from collections.abc import Mapping, Sequence
from io import BytesIO
from uuid import uuid4

from .hashing import canonical_json_bytes
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
    ) -> None:
        if role not in {"case_builder", "solver", "judge"}:
            raise ValueError(f"invalid LLM role: {role}")
        self.store = store
        self.role = role
        self.records: list[ModelCallRecord] = []

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
        self.records.append(record)
        return record

    def _json_artifact(self, value: object) -> ArtifactReference:
        return self.store.put_blob(BytesIO(canonical_json_bytes(value)), "application/json")
