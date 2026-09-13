from __future__ import annotations

from urllib.parse import urlparse

from ms_agent_eval.core.errors import ConfigurationError
from ms_agent_eval.core.providers import ModelCallObserver
from ms_agent_eval.core.workspace import ResolvedRoleModel
from ms_agent_eval.programs.dspy.budget import BudgetLedger
from ms_agent_eval.programs.dspy.observed_lm import ObservedDspyLM


def create_observed_lm(
    model: ResolvedRoleModel,
    observer: ModelCallObserver,
    *,
    budget: BudgetLedger | None = None,
    api_key: str | None = None,
) -> ObservedDspyLM:
    """Bind a role to the canonical observed DSPy LM through an OpenAI-compatible API."""

    if model.provider != "openai_compatible":
        raise ConfigurationError(f"unsupported DSPy provider: {model.provider}")
    endpoint = urlparse(model.base_url)
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
        raise ConfigurationError("OpenAI-compatible API base URL must be an HTTP(S) URL")
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ConfigurationError(
            "OpenAI-compatible API base URL must not embed credentials/query/fragment"
        )
    arguments: dict[str, object] = {
        "api_base": model.base_url.rstrip("/"),
        # LiteLLM's OpenAI route requires a non-empty key even when the
        # configured OpenAI-compatible server is intentionally unauthenticated.
        # This fixed sentinel is transport plumbing, not a credential.
        "api_key": api_key or "not-needed",
        "cache": False,
        **dict(model.parameters),
    }
    if model.reasoning_effort is not None:
        # OpenAI-compatible servers may implement documented OpenAI fields that
        # LiteLLM's static model catalog does not associate with the configured
        # custom model name. Opt this field in explicitly so it reaches the
        # endpoint instead of failing client-side validation.
        arguments["reasoning_effort"] = model.reasoning_effort
        arguments["allowed_openai_params"] = ["reasoning_effort"]
    return ObservedDspyLM(
        provider_id=model.provider,
        model=f"openai/{model.model}",
        observer=observer,
        configured_cost_per_call=model.configured_cost_per_call,
        budget=budget,
        **arguments,
    )
