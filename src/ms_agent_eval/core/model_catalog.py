from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .errors import ConfigurationError, ResolutionError


@dataclass(frozen=True)
class ModelCatalog:
    base_url: str
    models: tuple[str, ...]


def validate_api_base_url(value: str) -> str:
    result = value.strip().rstrip("/")
    parsed = urlparse(result)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("API base URL must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(
            "API base URL must not contain credentials, a query, or a fragment"
        )
    return result


def list_models(
    base_url: str,
    *,
    api_key: str | None = None,
    timeout_seconds: float = 10.0,
) -> ModelCatalog:
    normalized = validate_api_base_url(base_url)
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(f"{normalized}/models", headers=headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            payload = json.load(response)
    except HTTPError as error:
        if error.code in {401, 403}:
            raise ResolutionError("model discovery authentication failed") from error
        raise ResolutionError(f"model discovery failed with HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise ResolutionError("could not connect to the OpenAI-compatible API") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ResolutionError("model discovery returned an invalid OpenAI-compatible response")
    models = sorted(
        {
            item["id"].strip()
            for item in payload["data"]
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].strip()
        }
    )
    if not models:
        raise ResolutionError("the OpenAI-compatible API returned no model ids")
    return ModelCatalog(normalized, tuple(models))
