# OpenRouter Model Catalog

The repository keeps a static, editable catalog at `models/openrouter.json`.
Refresh it from OpenRouter without deleting existing rows:

```bash
ms-agent-eval models refresh
```

Use a different catalog path when needed:

```bash
ms-agent-eval models refresh --catalog /path/to/models.json
```

The same operation is available as a Python function:

```python
from pathlib import Path

from ms_agent_eval.core.model_catalog import refresh_openrouter_catalog

catalog = refresh_openrouter_catalog(Path("models/openrouter.json"))
print(len(catalog.models))
```

The refresh uses `GET https://openrouter.ai/api/v1/models` and performs an
upsert keyed by `model`. A returned model replaces the row with the same id and
new ids are inserted. Rows absent from the response are never deleted, so a
manually added custom model remains in the file. The catalog has no refresh
timestamp.

Each OpenRouter row contains:

- `model`, `name`, `source`, `provider`, `canonical_slug`, and
  `hugging_face_id`;
- `context_length`;
- `input_price_per_million`, `output_price_per_million`,
  `cache_read_price_per_million`, and `cache_write_price_per_million`;
- the complete raw `pricing` object; and
- architecture, supported parameters, and reasoning metadata under
  `capabilities`.

OpenRouter's raw pricing values are USD per token, request, or other billing
unit. Only the four token-price convenience columns are multiplied by one
million. The raw `pricing` object remains authoritative for web searches,
images, audio, reasoning, requests, tiered overrides, and future billing
dimensions. See the
[OpenRouter Models API](https://openrouter.ai/docs/api/api-reference/models/get-models).
The `provider` convenience field is the namespace before `/` in the model id;
it does not claim that a particular OpenRouter hosting route was selected.

Add a custom model directly to the `models` array. Set `source` to `custom` so
default matching continues to search only OpenRouter rows:

```json
{
  "model": "qwen3.6:27b-coding",
  "source": "custom",
  "provider": "local",
  "context_length": 131072,
  "pricing": {}
}
```

Model matching is deterministic, model-name-aware fuzzy matching rather than an
embedding call. It compares ids, display names, canonical slugs, and
Hugging Face ids; normalizes separators and provider prefixes; preserves model
family and parameter-size tokens; and treats suffixes such as `coding`,
`instruct`, and `latest` as variants:

```bash
ms-agent-eval models match 'qwen3.6:27b-coding'
```

The command returns the closest matches as JSON, including context length and
normalized token prices. Use `--limit N` to control the number of results. By
default it matches only against OpenRouter rows; add `--include-custom` to also
rank manually maintained rows. The `similarity` value is used only to order
results—there is no promotion gate or pass/fail threshold.

The Python API exposes the same matcher:

```python
from pathlib import Path

from ms_agent_eval.core.model_catalog import load_openrouter_catalog, match_model

catalog = load_openrouter_catalog(Path("models/openrouter.json"))
match = match_model("qwen3.6:27b-coding", catalog)

assert match is not None
print(match.model)  # qwen/qwen3.6-27b
print(match.record["input_price_per_million"])
```

`match_model` always returns the highest-ranked OpenRouter row when the catalog
contains one; it has no arbitrary confidence cutoff. Use `rank_model_matches`
to inspect multiple candidates and their relative similarity scores. Pass
`source=None` only when custom rows should participate in matching.
