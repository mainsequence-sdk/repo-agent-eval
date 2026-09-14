from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import ConfigurationError, ResolutionError


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CATALOG_SCHEMA_VERSION = 1
_MODEL_TOKEN = re.compile(r"[a-z]+|\d+(?:\.\d+)?[a-z]*")
_VARIANT_TOKENS = frozenset(
    {
        "batch",
        "chat",
        "code",
        "coder",
        "coding",
        "free",
        "instruct",
        "latest",
        "model",
        "preview",
    }
)
_PRICE_FIELDS = {
    "input_price_per_million": "prompt",
    "output_price_per_million": "completion",
    "cache_read_price_per_million": "input_cache_read",
    "cache_write_price_per_million": "input_cache_write",
}


@dataclass(frozen=True)
class OpenRouterCatalog:
    path: Path
    models: tuple[Mapping[str, object], ...]

    def by_model(self) -> Mapping[str, Mapping[str, object]]:
        return {str(item["model"]): item for item in self.models}


@dataclass(frozen=True)
class ModelMatch:
    query: str
    model: str
    score: float
    record: Mapping[str, object]


def load_openrouter_catalog(path: Path) -> OpenRouterCatalog:
    """Load a persisted catalog without contacting OpenRouter."""

    resolved = path.resolve()
    if not resolved.exists():
        return OpenRouterCatalog(resolved, ())
    if resolved.is_symlink() or not resolved.is_file():
        raise ConfigurationError("model catalog must be a regular file", path=resolved)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("model catalog must be valid UTF-8 JSON", path=resolved) from error
    if not isinstance(payload, Mapping):
        raise ConfigurationError("model catalog must be a JSON object", path=resolved)
    if payload.get("schema_version") != OPENROUTER_CATALOG_SCHEMA_VERSION:
        raise ConfigurationError("model catalog schema_version must be 1", path=resolved)
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise ConfigurationError("model catalog models must be a list", path=resolved)
    models = tuple(_stored_record(item, path=resolved) for item in raw_models)
    ids = [str(item["model"]) for item in models]
    if len(ids) != len(set(ids)):
        raise ConfigurationError("model catalog contains duplicate model ids", path=resolved)
    return OpenRouterCatalog(resolved, models)


def upsert_openrouter_models(
    path: Path,
    openrouter_models: Sequence[Mapping[str, object]],
) -> OpenRouterCatalog:
    """Insert or replace returned OpenRouter rows without deleting existing rows."""

    current = load_openrouter_catalog(path)
    by_model = dict(current.by_model())
    seen: set[str] = set()
    for item in openrouter_models:
        record = _openrouter_record(item)
        model = str(record["model"])
        if model in seen:
            raise ResolutionError(f"OpenRouter returned duplicate model id {model!r}")
        seen.add(model)
        by_model[model] = record
    if not seen:
        raise ResolutionError("OpenRouter returned no model records")
    payload = {
        "schema_version": OPENROUTER_CATALOG_SCHEMA_VERSION,
        "models": [by_model[model] for model in sorted(by_model)],
    }
    _write_atomic_json(current.path, payload)
    return load_openrouter_catalog(current.path)


def refresh_openrouter_catalog(
    path: Path,
    *,
    endpoint: str = OPENROUTER_MODELS_URL,
    timeout_seconds: float = 30.0,
) -> OpenRouterCatalog:
    """Fetch OpenRouter's current model list and upsert it into a static catalog."""

    request = Request(endpoint, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            payload = json.load(response)
    except HTTPError as error:
        raise ResolutionError(
            f"OpenRouter model discovery failed with HTTP {error.code}"
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise ResolutionError("could not connect to the OpenRouter Models API") from error
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise ResolutionError("OpenRouter returned an invalid models response")
    records = [item for item in payload["data"] if isinstance(item, Mapping)]
    if len(records) != len(payload["data"]):
        raise ResolutionError("OpenRouter returned a non-object model record")
    return upsert_openrouter_models(path, records)


def rank_model_matches(
    query: str,
    catalog: OpenRouterCatalog | Sequence[Mapping[str, object]],
    *,
    limit: int = 5,
    source: str | None = "openrouter",
) -> tuple[ModelMatch, ...]:
    """Rank catalog rows using deterministic, model-name-aware fuzzy matching."""

    value = query.strip()
    if not value:
        raise ConfigurationError("model match query must not be empty")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ConfigurationError("model match limit must be a positive integer")
    records = catalog.models if isinstance(catalog, OpenRouterCatalog) else tuple(catalog)
    matches = [
        ModelMatch(value, model, _record_similarity(value, record), record)
        for raw in records
        for record in [_stored_record(raw)]
        if source is None or record.get("source") == source
        for model in [str(record["model"])]
    ]
    return tuple(sorted(matches, key=lambda item: (-item.score, item.model))[:limit])


def match_model(
    query: str,
    catalog: OpenRouterCatalog | Sequence[Mapping[str, object]],
    *,
    source: str | None = "openrouter",
) -> ModelMatch | None:
    """Return the best fuzzy match, or ``None`` when the catalog is empty."""

    matches = rank_model_matches(query, catalog, limit=1, source=source)
    return matches[0] if matches else None


def _openrouter_record(item: Mapping[str, object]) -> Mapping[str, object]:
    raw_id = item.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ResolutionError("OpenRouter model record has no valid id")
    model = raw_id.strip()
    raw_pricing = item.get("pricing", {})
    if raw_pricing is None:
        raw_pricing = {}
    if not isinstance(raw_pricing, Mapping):
        raise ResolutionError(f"OpenRouter pricing for {model!r} is not an object")
    pricing = _json_copy(raw_pricing, field=f"pricing for {model!r}")
    architecture = item.get("architecture")
    if architecture is not None and not isinstance(architecture, Mapping):
        raise ResolutionError(f"OpenRouter architecture for {model!r} is not an object")
    supported = item.get("supported_parameters")
    if supported is not None and not isinstance(supported, list):
        raise ResolutionError(f"OpenRouter supported_parameters for {model!r} is not a list")
    capabilities: dict[str, object] = {}
    if architecture is not None:
        capabilities["architecture"] = _json_copy(architecture, field=f"architecture for {model!r}")
    if supported is not None:
        capabilities["supported_parameters"] = _json_copy(
            supported, field=f"supported_parameters for {model!r}"
        )
    if isinstance(item.get("reasoning"), Mapping):
        capabilities["reasoning"] = _json_copy(item["reasoning"], field=f"reasoning for {model!r}")
    record: dict[str, object] = {
        "model": model,
        "source": "openrouter",
        "provider": model.partition("/")[0].lstrip("~") if "/" in model else None,
        "name": item.get("name") if isinstance(item.get("name"), str) else None,
        "canonical_slug": (
            item.get("canonical_slug") if isinstance(item.get("canonical_slug"), str) else None
        ),
        "hugging_face_id": (
            item.get("hugging_face_id") if isinstance(item.get("hugging_face_id"), str) else None
        ),
        "context_length": (
            item.get("context_length")
            if isinstance(item.get("context_length"), int)
            and not isinstance(item.get("context_length"), bool)
            else None
        ),
        "pricing": pricing,
        "capabilities": capabilities,
    }
    for output_field, pricing_field in _PRICE_FIELDS.items():
        record[output_field] = _price_per_million(raw_pricing.get(pricing_field))
    return record


def _stored_record(item: object, *, path: Path | None = None) -> Mapping[str, object]:
    if not isinstance(item, Mapping):
        raise ConfigurationError("model catalog rows must be objects", path=path)
    record = {str(key): value for key, value in item.items()}
    model = record.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConfigurationError("model catalog rows require a model id", path=path)
    record["model"] = model.strip()
    return record


def _price_per_million(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value)) * Decimal(1_000_000)
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite():
        return None
    return float(price)


def _record_similarity(query: str, record: Mapping[str, object]) -> float:
    candidates = [record.get("model"), record.get("name"), record.get("canonical_slug")]
    hugging_face = record.get("hugging_face_id")
    if isinstance(hugging_face, str):
        candidates.append(hugging_face)
    forms = [
        form
        for candidate in candidates
        if isinstance(candidate, str)
        for form in (candidate, candidate.rpartition("/")[2])
        if form
    ]
    return max((_model_similarity(query, candidate) for candidate in forms), default=0.0)


def _model_similarity(left: str, right: str) -> float:
    left_raw = _model_tokens(left, drop_variants=False)
    right_raw = _model_tokens(right, drop_variants=False)
    left_core = _model_tokens(left, drop_variants=True)
    right_core = _model_tokens(right, drop_variants=True)
    if not left_core or not right_core:
        return 0.0
    core_ratio = SequenceMatcher(None, "".join(left_core), "".join(right_core)).ratio()
    raw_ratio = SequenceMatcher(None, "".join(left_raw), "".join(right_raw)).ratio()
    overlap = len(set(left_core) & set(right_core)) / len(set(left_core) | set(right_core))
    left_numeric = {token for token in left_core if token[0].isdigit()}
    right_numeric = {token for token in right_core if token[0].isdigit()}
    numeric = 1.0 if left_numeric == right_numeric else 0.0
    score = 0.55 * core_ratio + 0.2 * raw_ratio + 0.15 * overlap + 0.1 * numeric
    return round(min(max(score, 0.0), 1.0), 6)


def _model_tokens(value: str, *, drop_variants: bool) -> tuple[str, ...]:
    tokens = tuple(_MODEL_TOKEN.findall(value.casefold()))
    if not drop_variants:
        return tokens
    return tuple(token for token in tokens if token not in _VARIANT_TOKENS)


def _json_copy(value: object, *, field: str) -> object:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as error:
        raise ResolutionError(f"OpenRouter returned non-JSON {field}") from error


def _write_atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
