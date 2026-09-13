# OpenAI-Compatible API

Any model-driven stage can use the provider-neutral HTTP executor. API address,
model identity, and stage assignment are separate values in `workspace.yaml`:

```yaml
executors:
  inference-pool:
    type: endpoint
    adapter: openai_compatible
    endpoints:
      - id: primary
        base_url: http://127.0.0.1:8000/v1
        max_concurrent_requests: 4
models:
  solver:
    name: solver-model
    deployment_revision: solver-deployment-v1
    reasoning_effort: high
    parameters: {temperature: 0.2}
stages:
  solve:
    executor: inference-pool
    model: solver
    budget: {model_calls: 1, tokens: 32000, wall_seconds: 600}
```

API addresses, capacities, and model names are versioned experiment
configuration. They do not come from environment variables. Solver and judge
are isolated roles but may deliberately share an executor pool and model id.

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

Every base URL must include the path expected by the server; the framework does
not silently append `/v1`. Ollama, vLLM, LM Studio, and hosted services may be
used when they expose a compatible API. Provider-specific branches are not
part of the evaluation path.

## Endpoint pools and concurrency

An endpoint executor is a logical pool. Ready independent packets are executed
by a bounded local thread pool and routed through explicit per-endpoint slots:

```yaml
executors:
  lan-inference:
    type: endpoint
    adapter: openai_compatible
    endpoints:
      - id: llama-cpp-17-18081
        base_url: http://192.168.1.17:18081/v1
        max_concurrent_requests: 4
      - id: llama-cpp-17-18080
        base_url: http://192.168.1.17:18080/v1
        max_concurrent_requests: 4
      - id: llama-server-10-11436
        base_url: http://192.168.1.10:11436/v1
        max_concurrent_requests: 8
```

This pool has a maximum of sixteen concurrent requests. Each endpoint has its own
permit queue, and filesystem slot locks under
`operations/locks/executors/` prevent separate local coordinator processes
from exceeding the same configured capacity. Stage barriers still apply:
judge validation precedes solving, and judging waits for solver outputs.
Within judge validation, every `(fixture, repetition)` vote acquires its own
permit. Five fixtures with three repetitions therefore create 15 independent
requests, all of which fit in this 16-slot pool. Vote results are reordered by
fixture manifest order and repetition before deterministic aggregation.

All endpoints in one pool must serve the same output-affecting deployment for a
given model profile. The configured `deployment_revision` is the operator's
equivalence attestation. Capacity and replica topology are attempt provenance,
not semantic work identity, so changing only those values does not invalidate a
reusable answer. Changing weights, quantization, tokenizer, chat template, or
serving behavior requires a new deployment revision.

Execution results record endpoint id, endpoint/pool hashes, capacity, queue
wait, observed concurrency, call counts, failures, and latency. Completed run
reports include a human-readable per-endpoint table.

## Authentication

For an unauthenticated local server, omit `api_key_env` and do not create an
environment file.

For an authenticated server, reference only the credential:

```yaml
executors:
  hosted-models:
    type: endpoint
    adapter: openai_compatible
    endpoints:
      - id: hosted-primary
        base_url: https://provider.example/v1
        max_concurrent_requests: 6
        api_key_env: OPENAI_API_KEY
```

Put the value in the ignored `.env` beside `workspace.yaml`:

```dotenv
OPENAI_API_KEY=replace-with-a-real-secret
```

When the CLI creates an authenticated workspace in a new directory, it also
creates `.gitignore` with an `.env` entry.

The key is resolved independently for the endpoint that receives the request.
It is passed only when making a request and is excluded from workspace
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
