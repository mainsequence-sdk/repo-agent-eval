from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .hashing import content_hash, json_value
from .workspace import EndpointConfiguration


PreflightStatus = Literal["fits", "exceeds", "skipped"]


@dataclass(frozen=True)
class TokenPreflightResult:
    status: PreflightStatus
    reason: str
    context_window: int | None
    input_tokens: int | None
    maximum_output_tokens: int | None
    required_tokens: int | None
    tokenizer_fingerprint: str | None
    chat_template_fingerprint: str | None
    capability_source: str | None
    capability_checked_at: str | None
    rendered_message_hash: str

    @property
    def permits_dispatch(self) -> bool:
        return self.status != "exceeds"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "context_window": self.context_window,
            "input_tokens": self.input_tokens,
            "maximum_output_tokens": self.maximum_output_tokens,
            "required_tokens": self.required_tokens,
            "tokenizer_fingerprint": self.tokenizer_fingerprint,
            "chat_template_fingerprint": self.chat_template_fingerprint,
            "capability_source": self.capability_source,
            "capability_checked_at": self.capability_checked_at,
            "rendered_message_hash": self.rendered_message_hash,
        }


class ContextWindowExceededError(RuntimeError):
    def __init__(self, endpoint_id: str, result: TokenPreflightResult) -> None:
        self.endpoint_id = endpoint_id
        self.result = result
        super().__init__(
            f"endpoint {endpoint_id!r} context preflight requires "
            f"{result.required_tokens} tokens "
            f"(input {result.input_tokens} + output {result.maximum_output_tokens}) "
            f"but the endpoint advertises {result.context_window}"
        )


@dataclass(frozen=True)
class _EndpointCapabilities:
    context_window: int | None
    context_source: str | None
    tokenizer_fingerprint: str | None
    chat_template_fingerprint: str | None
    reason: str | None
    checked_at: str
    retry_after: float | None = None


class _ExtensionFailure(RuntimeError):
    def __init__(self, reason: str, *, temporary: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.temporary = temporary


class EndpointTokenPreflight:
    """Optional exact token admission through llama.cpp-compatible extensions."""

    _UNSUPPORTED_HTTP = {404, 405, 501}
    _UNAUTHORIZED_HTTP = {401, 403}

    def __init__(
        self,
        *,
        timeout_seconds: float = 3.0,
        unavailable_retry_seconds: float = 5.0,
        maximum_response_bytes: int = 8 * 1024 * 1024,
        open_url: Callable[..., object] = urlopen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.unavailable_retry_seconds = unavailable_retry_seconds
        self.maximum_response_bytes = maximum_response_bytes
        self._open_url = open_url
        self._clock = clock
        self._lock = RLock()
        self._capabilities: dict[tuple[str, str, str], _EndpointCapabilities] = {}
        self._unsupported_extensions: set[tuple[str, str]] = set()
        self._token_counts: dict[str, int] = {}

    def evaluate(
        self,
        endpoint: EndpointConfiguration,
        *,
        model: str,
        deployment_revision: str,
        messages: Sequence[Mapping[str, object]],
        maximum_output_tokens: int | None,
        reasoning_effort: str | None,
        api_key: str | None,
    ) -> TokenPreflightResult:
        message_value = json_value(list(messages))
        message_hash = content_hash(message_value)
        if maximum_output_tokens is None or maximum_output_tokens < 0:
            return self._skipped(
                "output_limit_unknown",
                message_hash,
                maximum_output_tokens=maximum_output_tokens,
            )

        capabilities = self._capabilities_for(
            endpoint,
            model=model,
            deployment_revision=deployment_revision,
            api_key=api_key,
        )
        if capabilities.context_window is None:
            return self._skipped(
                capabilities.reason or "context_unknown",
                message_hash,
                maximum_output_tokens=maximum_output_tokens,
                capabilities=capabilities,
            )

        cache_key = content_hash(
            {
                "endpoint_configuration_hash": endpoint.configuration_hash,
                "deployment_revision": deployment_revision,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "rendered_message_hash": message_hash,
                "chat_template_fingerprint": capabilities.chat_template_fingerprint,
                "tokenizer_fingerprint": capabilities.tokenizer_fingerprint,
            }
        )
        with self._lock:
            input_tokens = self._token_counts.get(cache_key)
        if input_tokens is None:
            try:
                rendered = self._apply_template(
                    endpoint,
                    messages,
                    reasoning_effort=reasoning_effort,
                    api_key=api_key,
                )
                input_tokens = self._tokenize(endpoint, rendered, api_key=api_key)
            except _ExtensionFailure as error:
                return self._skipped(
                    error.reason,
                    message_hash,
                    maximum_output_tokens=maximum_output_tokens,
                    capabilities=capabilities,
                )
            with self._lock:
                self._token_counts.setdefault(cache_key, input_tokens)

        required = input_tokens + maximum_output_tokens
        status: PreflightStatus = (
            "fits" if required <= capabilities.context_window else "exceeds"
        )
        return TokenPreflightResult(
            status=status,
            reason="exact_endpoint_tokenization",
            context_window=capabilities.context_window,
            input_tokens=input_tokens,
            maximum_output_tokens=maximum_output_tokens,
            required_tokens=required,
            tokenizer_fingerprint=capabilities.tokenizer_fingerprint,
            chat_template_fingerprint=capabilities.chat_template_fingerprint,
            capability_source=capabilities.context_source,
            capability_checked_at=capabilities.checked_at,
            rendered_message_hash=message_hash,
        )

    def _capabilities_for(
        self,
        endpoint: EndpointConfiguration,
        *,
        model: str,
        deployment_revision: str,
        api_key: str | None,
    ) -> _EndpointCapabilities:
        key = (endpoint.configuration_hash, model, deployment_revision)
        now = self._clock()
        with self._lock:
            cached = self._capabilities.get(key)
            if cached is not None and (
                cached.retry_after is None or cached.retry_after > now
            ):
                return cached
            # Serialize first discovery per endpoint. The network timeout is
            # intentionally short, and this prevents a parallel-run probe storm.
            discovered = self._discover_capabilities(
                endpoint,
                model=model,
                deployment_revision=deployment_revision,
                api_key=api_key,
            )
            self._capabilities[key] = discovered
            return discovered

    def _discover_capabilities(
        self,
        endpoint: EndpointConfiguration,
        *,
        model: str,
        deployment_revision: str,
        api_key: str | None,
    ) -> _EndpointCapabilities:
        checked_at = datetime.now(UTC).isoformat()
        props: Mapping[str, object] | None = None
        reason: str | None = None
        try:
            props = self._request_json(
                endpoint,
                "props",
                method="GET",
                api_key=api_key,
            )
        except _ExtensionFailure as error:
            reason = error.reason

        context_window = self._props_context_window(props)
        context_source = "props" if context_window is not None else None
        template = props.get("chat_template") if props is not None else None
        template_fingerprint = (
            content_hash(template) if isinstance(template, str) and template else None
        )

        model_metadata: Mapping[str, object] | None = None
        if context_window is None:
            try:
                model_metadata = self._request_json(
                    endpoint,
                    "models",
                    method="GET",
                    api_key=api_key,
                    use_openai_base=True,
                )
            except _ExtensionFailure as error:
                reason = error.reason
            context_window = self._models_context_window(model_metadata, model)
            if context_window is not None:
                context_source = "models"

        tokenizer_fingerprint = content_hash(
            {
                "model": model,
                "deployment_revision": deployment_revision,
                "model_metadata": model_metadata,
            }
        )
        if context_window is not None:
            return _EndpointCapabilities(
                context_window,
                context_source,
                tokenizer_fingerprint,
                template_fingerprint,
                None,
                checked_at,
            )
        temporary = reason in {"capability_unavailable", "capability_timeout"}
        return _EndpointCapabilities(
            None,
            None,
            tokenizer_fingerprint,
            template_fingerprint,
            reason or "context_unknown",
            checked_at,
            (
                self._clock() + self.unavailable_retry_seconds
                if temporary
                else None
            ),
        )

    @staticmethod
    def _props_context_window(payload: Mapping[str, object] | None) -> int | None:
        if payload is None:
            return None
        settings = payload.get("default_generation_settings")
        if not isinstance(settings, Mapping):
            return None
        value = settings.get("n_ctx")
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
            else None
        )

    @staticmethod
    def _models_context_window(
        payload: Mapping[str, object] | None,
        model: str,
    ) -> int | None:
        if payload is None:
            return None
        data = payload.get("data")
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes, bytearray)):
            return None
        candidates = [item for item in data if isinstance(item, Mapping)]
        selected = next((item for item in candidates if item.get("id") == model), None)
        if selected is None and len(candidates) == 1:
            selected = candidates[0]
        meta = selected.get("meta") if selected is not None else None
        if not isinstance(meta, Mapping):
            return None
        value = meta.get("n_ctx")
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    def _apply_template(
        self,
        endpoint: EndpointConfiguration,
        messages: Sequence[Mapping[str, object]],
        *,
        reasoning_effort: str | None,
        api_key: str | None,
    ) -> str:
        payload: dict[str, object] = {
            "messages": json_value(list(messages)),
            "add_generation_prompt": True,
        }
        if reasoning_effort == "none":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        response = self._request_json(
            endpoint,
            "apply-template",
            method="POST",
            payload=payload,
            api_key=api_key,
        )
        prompt = response.get("prompt")
        if not isinstance(prompt, str):
            raise _ExtensionFailure("template_schema_incompatible")
        return prompt

    def _tokenize(
        self,
        endpoint: EndpointConfiguration,
        content: str,
        *,
        api_key: str | None,
    ) -> int:
        response = self._request_json(
            endpoint,
            "tokenize",
            method="POST",
            payload={"content": content, "add_special": True},
            api_key=api_key,
        )
        tokens = response.get("tokens")
        if not isinstance(tokens, Sequence) or isinstance(tokens, (str, bytes, bytearray)):
            raise _ExtensionFailure("tokenizer_schema_incompatible")
        if any(not isinstance(item, int) or isinstance(item, bool) for item in tokens):
            raise _ExtensionFailure("tokenizer_schema_incompatible")
        return len(tokens)

    def _request_json(
        self,
        endpoint: EndpointConfiguration,
        extension: str,
        *,
        method: str,
        api_key: str | None,
        payload: Mapping[str, object] | None = None,
        use_openai_base: bool = False,
    ) -> Mapping[str, object]:
        cache_key = (endpoint.configuration_hash, extension)
        with self._lock:
            if cache_key in self._unsupported_extensions:
                raise _ExtensionFailure(f"{extension.replace('-', '_')}_unsupported")
        url = (
            f"{endpoint.base_url.rstrip('/')}/{extension}"
            if use_openai_base
            else f"{self._service_root(endpoint.base_url)}/{extension}"
        )
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            opened = self._open_url(request, timeout=self.timeout_seconds)
            with opened as response:  # type: ignore[attr-defined]
                raw = response.read(self.maximum_response_bytes + 1)
        except HTTPError as error:
            if error.code in self._UNSUPPORTED_HTTP:
                with self._lock:
                    self._unsupported_extensions.add(cache_key)
                raise _ExtensionFailure(
                    f"{extension.replace('-', '_')}_unsupported"
                ) from None
            if error.code in self._UNAUTHORIZED_HTTP:
                raise _ExtensionFailure("capability_unauthorized") from None
            raise _ExtensionFailure("capability_http_error") from None
        except TimeoutError:
            raise _ExtensionFailure("capability_timeout", temporary=True) from None
        except (URLError, OSError):
            raise _ExtensionFailure("capability_unavailable", temporary=True) from None
        if len(raw) > self.maximum_response_bytes:
            raise _ExtensionFailure("capability_response_too_large")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _ExtensionFailure(
                f"{extension.replace('-', '_')}_schema_incompatible"
            ) from None
        if not isinstance(value, Mapping):
            raise _ExtensionFailure(
                f"{extension.replace('-', '_')}_schema_incompatible"
            )
        return {str(key): item for key, item in value.items()}

    @staticmethod
    def _service_root(base_url: str) -> str:
        parsed = urlsplit(base_url.rstrip("/"))
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))

    @staticmethod
    def _skipped(
        reason: str,
        message_hash: str,
        *,
        maximum_output_tokens: int | None,
        capabilities: _EndpointCapabilities | None = None,
    ) -> TokenPreflightResult:
        return TokenPreflightResult(
            status="skipped",
            reason=reason,
            context_window=(None if capabilities is None else capabilities.context_window),
            input_tokens=None,
            maximum_output_tokens=maximum_output_tokens,
            required_tokens=None,
            tokenizer_fingerprint=(
                None if capabilities is None else capabilities.tokenizer_fingerprint
            ),
            chat_template_fingerprint=(
                None if capabilities is None else capabilities.chat_template_fingerprint
            ),
            capability_source=(
                None if capabilities is None else capabilities.context_source
            ),
            capability_checked_at=(
                None if capabilities is None else capabilities.checked_at
            ),
            rendered_message_hash=message_hash,
        )


__all__ = [
    "ContextWindowExceededError",
    "EndpointTokenPreflight",
    "TokenPreflightResult",
]
