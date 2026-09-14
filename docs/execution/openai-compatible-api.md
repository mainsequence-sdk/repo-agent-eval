# OpenAI-Compatible API

Any model-driven stage can use the provider-neutral HTTP executor. Transport,
semantic deployment, role profile, and experiment assignment are separate:

```yaml
executors:
  inference-pool:
    type: endpoint
    adapter: openai_compatible
    endpoints:
      - id: primary
        base_url: http://127.0.0.1:8000/v1
        max_concurrent_requests: 4
        routes:
          - {deployment: solver-local, model: solver-model}
deployments:
  solver-local:
    executor: inference-pool
    model: solver-model
    revision: solver-deployment-v1
models:
  solver:
    deployment: solver-local
    reasoning_effort: high
    parameters: {temperature: 0.2}
experiments:
  baseline:
    mode: evaluate
    roles: {solver: solver, judge: judge}
    # solver/runtime/repetitions omitted
```

API addresses, capacities, and model names are versioned experiment
configuration. They do not come from environment variables. Solver and judge
are isolated roles but may deliberately share an executor pool and model id.

Reasoning effort is also versioned explicitly with the model:

```yaml
models:
  solver:
    deployment: qwen-local
    reasoning_effort: high
    parameters: {temperature: 0.2, max_tokens: 4096}
  judge:
    deployment: qwen-local
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
        max_concurrent_requests: 2
        routes:
          - {deployment: qwen-local, model: qwen3.6:27b-coding}
      - id: llama-cpp-17-18080
        base_url: http://192.168.1.17:18080/v1
        max_concurrent_requests: 2
        routes:
          - {deployment: glm-local, model: glm-coding}
      - id: llama-server-10-11434
        base_url: http://192.168.1.10:11434/v1
        max_concurrent_requests: 4
        routes:
          - {deployment: qwen-local, model: qwen3.6:27b-coding}
```

This executor has eight physical slots, but capacity is selected by deployment:
Qwen has six eligible slots and GLM has two. Each endpoint has its own
permit queue, and filesystem slot locks under
`operations/locks/executors/` prevent separate local coordinator processes
from exceeding the same configured capacity. Stage barriers still apply:
judge validation precedes solving, and judging waits for solver outputs.
Within judge validation, every `(fixture, repetition)` vote acquires its own
permit. Five fixtures with three repetitions therefore create 15 independent
ready requests; a Qwen judge executes at most six concurrently in this pool. Vote results
are reordered by fixture manifest order and repetition before deterministic
aggregation.

An endpoint is eligible only when its `routes` includes the profile's deployment.
One endpoint may list multiple routes, but all share the endpoint's single
physical capacity. The configured `deployments.<id>.revision` is the operator's
equivalence attestation. Capacity and replica topology are attempt provenance;
weights, quantization, tokenizer, chat template, or serving changes require a
new deployment revision.

Execution results record endpoint id, endpoint/pool hashes, capacity, queue
wait, observed concurrency, call counts, failures, and latency. Completed run
reports include a human-readable per-endpoint table.

Connection, provider-server, and rate-limit failures are retried within the
same logical DSPy call on another endpoint eligible for that deployment. Each
eligible endpoint is attempted at most once, unhealthy endpoints enter a short
cooldown, and provenance records every failed and successful route. Invalid
output, authentication, application, and budget failures are not rerouted. If
the eligible pool is exhausted, the packet fails durably; an explicit
`run resume` creates a new immutable attempt for that transient failure.
Stage `budget.model_calls` limits completed model outputs; physical request
attempts and failures are tracked separately in endpoint provenance.

## Optional exact token preflight

No tokenizer adapter or manually configured context size is required. Immediately
before each DSPy model call, the coordinator checks whether that concrete
endpoint exposes the llama.cpp-compatible runtime extensions needed for an
exact count:

```text
GET  /props
POST /apply-template
POST /tokenize
```

When available, the coordinator renders the final DSPy messages with the
server's active chat template, counts those tokens with the server tokenizer,
adds the model profile's `max_tokens` output reservation, and compares the
total with the advertised `n_ctx`. This measures the complete request—including
DSPy instructions, skill context, case content, and judge evidence—not only the
small authored prompt.

If one replica is known to be too small, its permit is released and the call is
routed to another replica in the same executor pool. If every replica is known
to be too small, the stage fails before generation with
`context_window_exceeded`. The framework never truncates experiment inputs.

If `/props`, `/apply-template`, or `/tokenize` is missing, unavailable,
unauthorized, or incompatible, preflight is recorded as `skipped` and the
generation request proceeds normally. The provider remains authoritative and
any later context rejection is preserved as the model-call failure. Workspace
`validate` and `inspect` remain network-free; capability detection happens only
during execution.

To calculate the same native fit and the batch's usable parallel capacity
without making a generation call or creating a run:

```bash
uv run ms-agent-eval preflight baseline --workspace workspace.yaml
```

The terminal view separates exact-fit capacity from dispatchable capacity. The
latter includes replicas where optional native tokenization is unavailable,
matching runtime fallback policy. Use `--json` for every rendered request and
endpoint outcome.

Stage provenance and reports distinguish exact checks, skips, preflight
rejections, reroutes, and actual generation calls. See
[ADR-005](../architecture/005-optional-token-preflight.md) for the admission and
caching contract.

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
        routes:
          - {deployment: hosted-solver, model: provider-model-id}
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
