from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError

import dspy
import pytest

from ms_agent_eval.core.errors import ResolutionError
from ms_agent_eval.core.model_catalog import list_models
from ms_agent_eval.core.providers import ModelCallObserver
from ms_agent_eval.core.storage import FilesystemArtifactStore
from ms_agent_eval.core.workspace import ResolvedRoleModel
from ms_agent_eval.providers.openai_compatible import create_observed_lm


class _Response(BytesIO):
    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _model() -> ResolvedRoleModel:
    return ResolvedRoleModel(
        role="solver",
        executor_id="remote-models",
        provider="openai_compatible",
        model="solver-model",
        deployment_revision="solver-model-test-v1",
        base_url="http://localhost:8000/v1",
        api_key_env="OPENAI_API_KEY",
        reasoning_effort=None,
        parameters={"temperature": 0.2},
        configured_cost_per_call=0.0,
        content_hash="sha256:model",
    )


def test_model_catalog_uses_openai_compatible_models_route(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = {}

    def fake_urlopen(request, *, timeout):  # type: ignore[no-untyped-def]
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["timeout"] = timeout
        return _Response(json.dumps({"data": [{"id": "z"}, {"id": "a"}]}).encode())

    monkeypatch.setattr("ms_agent_eval.core.model_catalog.urlopen", fake_urlopen)

    catalog = list_models("http://localhost:8000/v1/", api_key="secret")

    assert catalog.models == ("a", "z")
    assert captured == {
        "url": "http://localhost:8000/v1/models",
        "authorization": "Bearer secret",
        "timeout": 10.0,
    }


@pytest.mark.parametrize(
    ("error", "message"),
    (
        (HTTPError("url", 401, "unauthorized", {}, None), "authentication failed"),
        (HTTPError("url", 404, "missing", {}, None), "failed with HTTP 404"),
        (URLError("connection refused"), "could not connect"),
    ),
)
def test_model_catalog_reports_actionable_failures(monkeypatch, error, message) -> None:  # type: ignore[no-untyped-def]
    def failing_urlopen(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise error

    monkeypatch.setattr("ms_agent_eval.core.model_catalog.urlopen", failing_urlopen)

    with pytest.raises(ResolutionError, match=message):
        list_models("http://localhost:8000/v1")


def test_provider_uses_openai_route_and_redacts_failed_call_secret(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    observer = ModelCallObserver(store, role="solver")
    secret = "do-not-persist-this-key"
    lm = create_observed_lm(_model(), observer, api_key=secret)

    assert lm.model == "openai/solver-model"
    assert secret not in repr(lm)
    assert lm.observed_parameters["api_base"] == "http://localhost:8000/v1"
    assert "api_key" not in lm.observed_parameters

    record = observer.failed(
        provider_id="openai_compatible",
        model="solver-model",
        parameters={},
        messages=(),
        request={},
        error=RuntimeError(f"authentication failed for {secret}"),
        secret_values=(secret,),
        latency_seconds=0.1,
        configured_cost=0.0,
    )
    with store.get_blob(record.response_artifact) as handle:
        failure = json.load(handle)
    assert failure["message"] == "authentication failed for [REDACTED]"
    assert secret not in json.dumps(failure)


def test_provider_supports_an_unauthenticated_openai_compatible_server(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    observer = ModelCallObserver(store, role="solver")

    lm = create_observed_lm(_model(), observer)

    assert lm.model == "openai/solver-model"
    assert "api_key" not in lm.observed_parameters
    assert "not-needed" not in repr(lm)


def test_provider_classifies_wrapped_connection_failures(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    observer = ModelCallObserver(store, role="solver")

    record = observer.failed(
        provider_id="openai_compatible",
        model="solver-model",
        parameters={},
        messages=(),
        request={},
        error=RuntimeError("InternalServerError: OpenAIException - Connection error."),
        latency_seconds=0.1,
        configured_cost=0.0,
    )

    assert record.error_kind == "connection_error"


def test_provider_classifies_context_failure_when_native_preflight_was_skipped(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    observer = ModelCallObserver(store, role="solver")

    record = observer.failed(
        provider_id="openai_compatible",
        model="solver-model",
        parameters={},
        messages=(),
        request={},
        error=RuntimeError("maximum context length exceeded"),
        latency_seconds=0.1,
        configured_cost=0.0,
    )

    assert record.error_kind == "context_window_exceeded"


def test_provider_allows_configured_reasoning_effort_for_custom_models(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    observer = ModelCallObserver(store, role="solver")
    model = replace(
        _model(),
        reasoning_effort="none",
        parameters={"temperature": 0.0},
    )

    lm = create_observed_lm(model, observer)

    assert lm.observed_parameters["reasoning_effort"] == "none"
    assert lm.observed_parameters["allowed_openai_params"] == ["reasoning_effort"]


def test_provider_redacts_secret_before_exception_leaves_lm(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    observer = ModelCallObserver(store, role="solver")
    secret = "secret-from-provider-error"
    lm = create_observed_lm(_model(), observer, api_key=secret)

    def fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise RuntimeError(f"provider echoed {secret}")

    monkeypatch.setattr(dspy.LM, "forward", fail)

    with pytest.raises(RuntimeError, match=r"provider echoed \[REDACTED\]") as captured:
        lm.forward(prompt="hello")
    assert secret not in str(captured.value)
