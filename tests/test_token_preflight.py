from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError, URLError

from ms_agent_eval.core.token_preflight import EndpointTokenPreflight
from ms_agent_eval.core.workspace import EndpointConfiguration


def _endpoint() -> EndpointConfiguration:
    return EndpointConfiguration(
        "local",
        "http://localhost:18080/v1",
        4,
        None,
    )


def test_exact_preflight_renders_then_tokenizes_and_caches() -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    def open_url(request, *, timeout):  # type: ignore[no-untyped-def]
        assert timeout == 0.25
        payload = json.loads(request.data) if request.data is not None else None
        calls.append((request.full_url, payload))
        if request.full_url.endswith("/props"):
            value = {
                "default_generation_settings": {"n_ctx": 10},
                "chat_template": "template-v1",
            }
        elif request.full_url.endswith("/apply-template"):
            value = {"prompt": "rendered prompt"}
        else:
            assert request.full_url.endswith("/tokenize")
            value = {"tokens": [1, 2, 3, 4, 5, 6]}
        return BytesIO(json.dumps(value).encode())

    preflight = EndpointTokenPreflight(timeout_seconds=0.25, open_url=open_url)
    arguments = {
        "model": "test-model",
        "deployment_revision": "revision-1",
        "messages": [{"role": "user", "content": "hello"}],
        "maximum_output_tokens": 4,
        "reasoning_effort": "none",
        "api_key": None,
    }
    first = preflight.evaluate(_endpoint(), **arguments)  # type: ignore[arg-type]
    second = preflight.evaluate(_endpoint(), **arguments)  # type: ignore[arg-type]

    assert first == second
    assert first.status == "fits"
    assert first.input_tokens == 6
    assert first.required_tokens == first.context_window == 10
    assert first.capability_source == "props"
    assert [url for url, _ in calls] == [
        "http://localhost:18080/props",
        "http://localhost:18080/apply-template",
        "http://localhost:18080/tokenize",
    ]
    assert calls[1][1] == {
        "messages": [{"role": "user", "content": "hello"}],
        "add_generation_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_exact_preflight_rejects_a_request_larger_than_context() -> None:
    def open_url(request, *, timeout):  # type: ignore[no-untyped-def]
        del timeout
        if request.full_url.endswith("/props"):
            value = {"default_generation_settings": {"n_ctx": 8}}
        elif request.full_url.endswith("/apply-template"):
            value = {"prompt": "rendered"}
        else:
            value = {"tokens": [1, 2, 3, 4, 5]}
        return BytesIO(json.dumps(value).encode())

    result = EndpointTokenPreflight(open_url=open_url).evaluate(
        _endpoint(),
        model="test-model",
        deployment_revision="revision-1",
        messages=[{"role": "user", "content": "large"}],
        maximum_output_tokens=4,
        reasoning_effort=None,
        api_key=None,
    )

    assert result.status == "exceeds"
    assert result.required_tokens == 9
    assert not result.permits_dispatch


def test_missing_optional_capability_skips_without_blocking_dispatch() -> None:
    def open_url(request, *, timeout):  # type: ignore[no-untyped-def]
        del timeout
        if request.full_url.endswith("/props"):
            raise HTTPError(request.full_url, 404, "missing", {}, None)
        assert request.full_url.endswith("/v1/models")
        return BytesIO(json.dumps({"data": [{"id": "test-model"}]}).encode())

    result = EndpointTokenPreflight(open_url=open_url).evaluate(
        _endpoint(),
        model="test-model",
        deployment_revision="revision-1",
        messages=[{"role": "user", "content": "hello"}],
        maximum_output_tokens=4,
        reasoning_effort=None,
        api_key=None,
    )

    assert result.status == "skipped"
    assert result.permits_dispatch
    assert result.context_window is None


def test_unavailable_optional_capability_is_skipped_and_retried_later() -> None:
    now = [10.0]
    calls = 0

    def open_url(request, *, timeout):  # type: ignore[no-untyped-def]
        nonlocal calls
        del request, timeout
        calls += 1
        raise URLError("offline")

    preflight = EndpointTokenPreflight(
        open_url=open_url,
        clock=lambda: now[0],
        unavailable_retry_seconds=5,
    )
    arguments = {
        "model": "test-model",
        "deployment_revision": "revision-1",
        "messages": [{"role": "user", "content": "hello"}],
        "maximum_output_tokens": 4,
        "reasoning_effort": None,
        "api_key": None,
    }

    assert preflight.evaluate(_endpoint(), **arguments).status == "skipped"  # type: ignore[arg-type]
    first_calls = calls
    assert preflight.evaluate(_endpoint(), **arguments).status == "skipped"  # type: ignore[arg-type]
    assert calls == first_calls
    now[0] = 16.0
    assert preflight.evaluate(_endpoint(), **arguments).status == "skipped"  # type: ignore[arg-type]
    assert calls > first_calls


def test_unauthorized_capability_is_skipped_without_exposing_the_key() -> None:
    seen_authorization: list[str | None] = []

    def open_url(request, *, timeout):  # type: ignore[no-untyped-def]
        del timeout
        seen_authorization.append(request.headers.get("Authorization"))
        raise HTTPError(request.full_url, 401, "unauthorized", {}, None)

    result = EndpointTokenPreflight(open_url=open_url).evaluate(
        _endpoint(),
        model="test-model",
        deployment_revision="revision-1",
        messages=[{"role": "user", "content": "hello"}],
        maximum_output_tokens=4,
        reasoning_effort=None,
        api_key="secret-value",
    )

    assert result.status == "skipped"
    assert result.reason == "capability_unauthorized"
    assert seen_authorization == ["Bearer secret-value", "Bearer secret-value"]
    assert "secret-value" not in json.dumps(result.as_dict())


def test_missing_template_extension_skips_after_context_discovery() -> None:
    def open_url(request, *, timeout):  # type: ignore[no-untyped-def]
        del timeout
        if request.full_url.endswith("/props"):
            return BytesIO(
                json.dumps({"default_generation_settings": {"n_ctx": 16}}).encode()
            )
        raise HTTPError(request.full_url, 404, "missing", {}, None)

    result = EndpointTokenPreflight(open_url=open_url).evaluate(
        _endpoint(),
        model="test-model",
        deployment_revision="revision-1",
        messages=[{"role": "user", "content": "hello"}],
        maximum_output_tokens=4,
        reasoning_effort=None,
        api_key=None,
    )

    assert result.status == "skipped"
    assert result.reason == "apply_template_unsupported"
    assert result.context_window == 16
    assert result.permits_dispatch


def test_oversized_capability_response_is_not_used_for_admission() -> None:
    def open_url(request, *, timeout):  # type: ignore[no-untyped-def]
        del request, timeout
        return BytesIO(b"{" + b" " * 100 + b"}")

    result = EndpointTokenPreflight(
        open_url=open_url,
        maximum_response_bytes=20,
    ).evaluate(
        _endpoint(),
        model="test-model",
        deployment_revision="revision-1",
        messages=[{"role": "user", "content": "hello"}],
        maximum_output_tokens=4,
        reasoning_effort=None,
        api_key=None,
    )

    assert result.status == "skipped"
    assert result.permits_dispatch
