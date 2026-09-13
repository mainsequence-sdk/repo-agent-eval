# OpenAI-Compatible API

Any model-driven stage can use the provider-neutral HTTP executor. API address,
model identity, and stage assignment are separate values in `workspace.yaml`:

```yaml
executors:
  remote-models:
    type: endpoint
    adapter: openai_compatible
    base_url: http://127.0.0.1:8000/v1
models:
  solver:
    name: solver-model
    reasoning_effort: high
    parameters: {temperature: 0.2}
stages:
  solve:
    executor: remote-models
    model: solver
    budget: {model_calls: 1, tokens: 32000, wall_seconds: 600}
```

The API address and model name are versioned experiment configuration. They do
not come from environment variables. Solver and judge are isolated roles but
may deliberately share an API address and model id.

Reasoning effort is also versioned explicitly with the model:

```yaml
models:
  solver:
    name: qwen3.6:27b-coding
    reasoning_effort: high
    parameters: {temperature: 0.2, max_tokens: 4096}
  judge:
    name: qwen3.6:27b-coding
    reasoning_effort: none
    parameters: {temperature: 0.0, max_tokens: 2048}
```

Accepted framework values are `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, `max`, and `ultra`. The selected endpoint and model must support the
chosen value. Omit the field to use the provider/model default. It is a
first-class field and is prohibited inside the generic `parameters` mapping.

The base URL must include the path expected by the server; the framework does
not silently append `/v1`. Ollama, vLLM, LM Studio, and hosted services may be
used when they expose a compatible API. Provider-specific branches are not
part of the evaluation path.

## Authentication

For an unauthenticated local server, omit `api_key_env` and do not create an
environment file.

For an authenticated server, reference only the credential:

```yaml
executors:
  hosted-models:
    type: endpoint
    adapter: openai_compatible
    base_url: https://provider.example/v1
    api_key_env: OPENAI_API_KEY
```

Put the value in the ignored `.env` beside `workspace.yaml`:

```dotenv
OPENAI_API_KEY=replace-with-a-real-secret
```

When the CLI creates an authenticated workspace in a new directory, it also
creates `.gitignore` with an `.env` entry.

The key is passed only when making a request. It is excluded from workspace
identity, locks, hashes, call evidence, errors, and reports.

## Guided configuration

Run:

```bash
uv run ms-agent-eval setup
```

The wizard asks for the base URL, optionally tests `GET /models`, lets the user
select or enter model ids, and previews the resulting YAML before it
writes anything. Model discovery performs no inference call.

Check the resolved non-secret identities afterward:

```bash
uv run ms-agent-eval inspect --workspace /path/to/workspace.yaml
```

DSPy's typed adapter renders requests. The observer stores the final messages,
provider response, usage, latency, configured cost, role, and model identity
under the external data root. Cache and LiteLLM retries remain disabled so the
framework's budgets and call evidence stay authoritative.
