from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import dspy
from ms_agent_eval.core.hashing import json_value
from ms_agent_eval.core.providers import ModelCallObserver

from .budget import BudgetLedger


def _plain(value: object) -> object:
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())  # type: ignore[union-attr]
    try:
        return json_value(value)
    except TypeError:
        return str(value)


def _redacted_error(error: Exception, secret_values: tuple[str, ...]) -> RuntimeError:
    message = str(error)
    for secret in secret_values:
        message = message.replace(secret, "[REDACTED]")
    return RuntimeError(f"{type(error).__name__}: {message}")


class ObservedDspyLM(dspy.LM):
    """DSPy LM whose final rendered request/response is normalized by core."""

    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        observer: ModelCallObserver,
        configured_cost_per_call: float = 0.0,
        budget: BudgetLedger | None = None,
        **kwargs: Any,
    ) -> None:
        secret_values = tuple(
            value
            for key, value in kwargs.items()
            if key in {"api_key", "credentials"} and isinstance(value, str) and value
        )
        super().__init__(model=model, num_retries=0, **kwargs)
        self.provider_id = provider_id
        self.observer = observer
        self.configured_cost_per_call = configured_cost_per_call
        self.budget = budget
        self._secret_values = secret_values
        self.observed_parameters = {
            key: value
            for key, value in kwargs.items()
            if key not in {"api_key", "headers", "credentials"}
        }

    def __repr__(self) -> str:
        return f"ObservedDspyLM(provider_id={self.provider_id!r}, model={self.model!r})"

    def forward(self, prompt=None, messages=None, **kwargs):  # type: ignore[no-untyped-def]
        rendered = messages or [{"role": "user", "content": prompt}]
        request = {
            "provider": self.provider_id,
            "model": self.model,
            "parameters": self.observed_parameters,
            "messages": rendered,
        }
        started = time.monotonic()
        if self.budget is not None:
            self.budget.begin_call(self.configured_cost_per_call)
        try:
            response = super().forward(prompt=prompt, messages=messages, **kwargs)
            payload = _plain(response)
            response_mapping = payload if isinstance(payload, Mapping) else {"value": payload}
            usage = response_mapping.get("usage", {})
            self.observer.completed(
                provider_id=self.provider_id,
                model=self.model,
                parameters=self.observed_parameters,
                messages=rendered,
                request=request,
                response=response_mapping,
                usage=usage if isinstance(usage, Mapping) else {},
                latency_seconds=time.monotonic() - started,
                configured_cost=self.configured_cost_per_call,
            )
        except Exception as error:
            self.observer.failed(
                provider_id=self.provider_id,
                model=self.model,
                parameters=self.observed_parameters,
                messages=rendered,
                request=request,
                error=error,
                secret_values=self._secret_values,
                latency_seconds=time.monotonic() - started,
                configured_cost=self.configured_cost_per_call,
            )
            if self.budget is not None:
                self.budget.finish_call()
            raise _redacted_error(error, self._secret_values) from None
        if self.budget is not None:
            self.budget.finish_call(usage if isinstance(usage, Mapping) else {})
        return response
