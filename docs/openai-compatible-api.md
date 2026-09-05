# OpenAI-Compatible API

The case builder, solver, and judge use the same provider-neutral HTTP
protocol. Each role stores its API base URL and model id directly in
`workspace.yaml`:

```yaml
model:
  provider: openai_compatible
  name: solver-model
  base_url: http://127.0.0.1:8000/v1
  parameters:
    temperature: 0.2
```

The API address and model name are versioned experiment configuration. They do
not come from environment variables. All three roles may share one API
address, but their model ids must differ.

The base URL must include the path expected by the server; the framework does
not silently append `/v1`. Ollama, vLLM, LM Studio, and hosted services may be
used when they expose a compatible API. Provider-specific branches are not
part of the evaluation path.

## Authentication

For an unauthenticated local server, omit `api_key_env` and do not create an
environment file.

For an authenticated server, reference only the credential:

```yaml
model:
  provider: openai_compatible
  name: solver-model
  base_url: https://provider.example/v1
  api_key_env: OPENAI_API_KEY
  parameters:
    temperature: 0.2
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
select or enter three model ids, and previews the resulting YAML before it
writes anything. Model discovery performs no inference call.

Check the resolved non-secret identities afterward:

```bash
uv run ms-agent-eval inspect --workspace /path/to/workspace.yaml
```

DSPy's typed adapter renders requests. The observer stores the final messages,
provider response, usage, latency, configured cost, role, and model identity
under the external data root. Cache and LiteLLM retries remain disabled so the
framework's budgets and call evidence stay authoritative.
