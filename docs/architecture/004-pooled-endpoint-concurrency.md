# ADR-004: Capacity-Aware Pooled Endpoint Concurrency

- Status: Accepted and implemented
- Date: 2026-09-13
- Decision owners: MS Agent Eval maintainers
- Scope: OpenAI-compatible endpoint configuration, local request scheduling,
  replica routing, concurrency limits, failures, provenance, and reuse identity
- Refines: [ADR-001: Portable Staged Execution](001-portable-staged-execution.md)
- Preserves:
  [ADR-003: Content-Addressed Experiment Graph and Stage Reuse](003-content-addressed-experiment-graph.md)

> Schema note: ADR-009 refines this executor-wide equivalence model. In schema
> 14, endpoint `routes` declare eligible deployments and usable concurrency is
> calculated for the selected deployment rather than every endpoint in the
> executor. The historical examples below document ADR-004's original schema.

## Context

An endpoint executor currently contains one `base_url`. Its mapping key is an
arbitrary local identifier such as `remote-qwen`, while model identity and
generation parameters are defined separately under `models`. The separation is
correct: an executor describes how a request runs, and a model profile describes
what is run. The current name is nevertheless misleading because:

- the service is on the local network, although the id says `remote`;
- an OpenAI-compatible endpoint may serve more than Qwen; and
- both solver and judge profiles use the same transport but different decoding
  settings.

More importantly, the executor has no capacity configuration and the coordinator
executes ready stage packets sequentially. The DSPy call budget internally uses
a concurrency value of one, but that is a per-packet accounting guard rather
than a scheduler or endpoint-wide capacity limit. It cannot create parallel
work, coordinate several packets, or combine multiple inference servers.

For the current MainSequence baseline, an uncached test run has 102 solver calls,
306 judge calls from three votes per case, and 15 judge-validation calls. Running
423 independent HTTP requests serially wastes the available capacity of an
inference server and becomes more costly as the case bank grows.

An operator may also expose the same model deployment through multiple servers.
For example, one server may safely process eight requests while another safely
processes four. The coordinator should then execute up to twelve independent
requests concurrently without ever sending more than eight to the first server
or four to the second.

## Decision

An endpoint executor becomes a logical, capacity-aware pool of one or more
OpenAI-compatible endpoint replicas. The coordinator owns a bounded local thread
pool and dispatches ready independent packets across endpoint slots. Every
endpoint enforces its own configured maximum, and their capacities add to the
logical executor capacity.

```text
                              lan-inference
                       logical capacity = 2 + 2 + 4 = 8
                                    │
                      local bounded request scheduler
                                    │
                 ┌────────────────┼────────────────┐
                 │                │                │
       llama.cpp :18081      llama.cpp :18080      llama server :11434
            2 slots               2 slots                 4 slots
```

Eight is an upper bound, not a promise that every stage always has eight
requests in flight. Effective concurrency is:

```text
min(ready independent requests, sum(healthy endpoint capacities))
```

Stage dependencies remain barriers. Judge validation must persist successfully
before solver packets run; judge packets depend on persisted solver outputs; and
aggregation waits for all required judgments. Concurrency changes execution of
ready peers, not the experiment graph.

## Workspace configuration

Use a list of structured `endpoints`, not a `base_urls` mapping or compact
colon-delimited strings. Every endpoint has a stable id, one validated base URL,
and one positive capacity.

```yaml
executors:
  lan-inference:
    type: endpoint
    adapter: openai_compatible
    endpoints:
      - id: llama-cpp-17-18081
        base_url: http://192.168.1.17:18081/v1
        max_concurrent_requests: 2
      - id: llama-cpp-17-18080
        base_url: http://192.168.1.17:18080/v1
        max_concurrent_requests: 2
      - id: llama-server-10-11434
        base_url: http://192.168.1.10:11434/v1
        max_concurrent_requests: 4

models:
  qwen-solver:
    name: qwen3.6:27b-coding
    deployment_revision: qwen3.6-27b-coding-local-2026-09-13
    parameters:
      temperature: 0.2
      max_tokens: 4096
      timeout: 600

  qwen-judge:
    name: qwen3.6:27b-coding
    deployment_revision: qwen3.6-27b-coding-local-2026-09-13
    reasoning_effort: none
    parameters:
      temperature: 0.0
      max_tokens: 2048
      timeout: 300

stages:
  validate_judge:
    executor: lan-inference
    model: qwen-judge
    reuse: {policy: exact}
    budget: {model_calls: 100, tokens: 250000, wall_seconds: 1800}
  solve:
    executor: lan-inference
    model: qwen-solver
    reuse: {policy: exact}
    budget: {model_calls: 1, tokens: 32000, wall_seconds: 600}
  judge:
    executor: lan-inference
    model: qwen-judge
    reuse: {policy: exact}
    budget: {model_calls: 3, tokens: 64000, wall_seconds: 600}
```

The executor id describes the service pool rather than a specific model. Model
profile ids describe role-specific inference behavior. Solver and judge remain
different profiles even when they share the same model name and deployment
revision because their reasoning effort and decoding parameters differ.

An endpoint may optionally name `api_key_env`; only the environment variable
name is versioned, and its value remains outside Git and all identities:

```yaml
      - id: hosted-a
        base_url: https://inference-a.example/v1
        max_concurrent_requests: 6
        api_key_env: INFERENCE_A_API_KEY
```

Pooled endpoints were introduced in schema version 11; the current schema is
version 12. The pool shape replaces singular
`executors.<id>.base_url` with the non-empty `endpoints` list for endpoint
executors. There is no legacy compatibility shim: schema-10 workspaces must be
rewritten explicitly, and old schemas continue to fail closed.

Configuration validation requires:

- at least one endpoint for every `type: endpoint` executor;
- a unique lowercase endpoint id within the executor;
- a positive integer `max_concurrent_requests`;
- a unique normalized base URL within the executor;
- HTTP or HTTPS without embedded credentials, query, or fragment;
- no `endpoints` field on harness or process executors.

A one-server setup uses a one-item list. Capacity must always be explicit in
generated templates; the library parser may use `1` only when constructing
configuration programmatically, not to hide missing capacity in a committed
schema-v13 workspace.

## Replica-equivalence contract

Pooling is valid only when every endpoint selected for one model profile serves
the same semantic model deployment for the configured model name. That means the
same weights, quantization, tokenizer, chat template, inference backend behavior,
reasoning control, and relevant compatibility settings.

`models.<id>.deployment_revision` is the operator's attestation for the entire
pool. If the replicas are not equivalent, they must not be placed in one
executor pool. They require separate model deployment identities and separate
experiments or explicit replication analysis.

The coordinator cannot prove weight equivalence from an OpenAI-compatible model
name. Setup and validation may query `/models` on every endpoint to verify that
the configured model id is advertised, but availability is not equivalence. The
operator must change `deployment_revision` whenever any replica's output-affecting
deployment changes.

## Local scheduling model

For an endpoint-backed packet batch, the coordinator creates a bounded
`ThreadPoolExecutor` whose maximum worker count is the sum of configured endpoint
capacities. It does not create one unbounded future per case. A bounded ready
queue feeds workers and applies backpressure to packet preparation.

Each endpoint owns exactly `max_concurrent_requests` permits. A worker must
acquire a concrete endpoint permit before invoking DSPy and must hold that permit
around the complete HTTP request. The permit is returned in `finally`, including
timeouts, parsing failures, and cancellation.

```text
ready packet queue
       │
       ▼
bounded worker pool (8)
       │
       ├── acquire llama-cpp-17-18081 slots 1..2 ──► requests
       ├── acquire llama-cpp-17-18080 slots 1..2 ──► requests
       └── acquire llama-server-10-11434 slots 1..4 ──► requests
```

The dispatcher uses capacity-aware availability rather than assigning requests
by a fixed modulo. A slow endpoint naturally returns permits more slowly, while
the other endpoint continues accepting work. Selection starts from a stable
rotation to prevent starvation, but completion timing is intentionally not part
of semantic result order.

The limit is enforced at the HTTP-call boundary as well as the packet scheduler.
This prevents nested model work, judge repetitions, or future DSPy programs from
bypassing endpoint capacity. Packet-level concurrency supplies most throughput:
independent solver cases run together, and independent judge cases run together.
Judge validation is a single graph packet but is internally expanded into one
request task per `(fixture, repetition)` pair. With five fixtures and three
repetitions, 15 votes become ready independently and up to eight run concurrently
in this pool.
Votes are restored to stable manifest/repetition order before median and majority
aggregation, so transport completion order cannot change the validation result.

The existing model-call/token/wall-time budget remains a per-packet safety and
accounting contract. Endpoint capacity is a transport scheduler constraint and
must not be implemented by overloading `model_calls`, `tokens`, or
`wall_seconds`.

## Thread and process safety

Every concurrently executed graph packet receives its own execution session.
Judge-validation fan-out creates a separate DSPy program, bound LM, and observer
for each vote while sharing one thread-safe budget ledger for the parent packet.
The session protects its observer and budget registries across worker threads.
DSPy configuration remains inside its scoped context; no thread mutates global
DSPy model state.

The content-addressed artifact store already writes blobs through temporary
files and atomic replacement. The stage ledger already serializes transitions
and uses work locks. The concurrent implementation must preserve those
properties and add tests for simultaneous claim, submit, validate, persist, and
cache publication from different packet workers.

For multiple coordinator processes sharing the same external data root, endpoint
permits use bounded file-lock slots below:

```text
operations/locks/executors/<pool-hash>/<endpoint-id>/<slot>.lock
```

The worker holds an exclusive lock for the duration of one request. Operating
system lock release on process death prevents permanently lost capacity. This
enforces limits across local CLI processes that share the data root. Independent
data roots cannot coordinate, so the inference service must still enforce its
own global admission limit.

## Failure, retry, and recovery

Replica pools provide bounded failover for endpoint-level failures. One logical
DSPy call may try each eligible replica at most once when the previous attempt
fails with a connection, provider-server, or rate-limit error. A failing replica
is quarantined from new calls for a short cooldown so concurrent work shifts to
the remaining compatible replicas. Context preflight rejection uses the same
bounded routing rule but never makes a generation request to the rejected
replica.

Every physical attempt remains observable. Provenance records the failed route,
the successful route, normalized errors, latency, and model-call usage. This is
transport failover, not semantic retry: parsing failures, invalid model output,
authentication failures, budget failures, and application errors are not sent
to another replica. When every eligible replica fails, the stage attempt is
durably failed.

`stages.<stage>.budget.model_calls` limits completed model outputs. Failed
physical requests followed by failover are recorded as request attempts but do
not consume another successful-output allowance. The router still bounds
physical attempts to at most one per eligible replica for each logical DSPy
call, so a three-replica pool cannot produce unbounded retries.

The required batch behavior is:

1. exhaust the bounded eligible-replica set for the failing model call;
2. stop scheduling new work after the first terminal packet failure;
3. allow already in-flight requests to finish and persist their model-call and
   trace evidence;
4. durably record every attempted endpoint and normalized/redacted error;
5. return the run as blocked or failed under the existing stage rules; and
6. on explicit resume, create a new immutable attempt for failed connection,
   provider-server, and rate-limit errors while reusing all persisted work.

An experiment revision is immutable. Correcting a mutable workspace endpoint
does not rewrite an old run's frozen definition. A historical run resumes from
its frozen replica pool and may recover through another eligible frozen replica;
a newly started run uses the corrected current workspace and forms a new
experiment revision.

## Deterministic results and information firewalls

Packets are planned in deterministic case/repetition order but may complete in
any order. Results retain their case, repetition, attempt, work, producer, and
model identities. Selection and aggregation continue sorting by their canonical
keys and never use completion order.

Concurrency does not relax information boundaries:

- solver workers receive only solver views;
- judge workers receive only the assigned candidate and judge view;
- different cases do not share mutable prompts, responses, or DSPy state;
- private judge material never enters a solver task or shared worker context; and
- one failed packet cannot expose another packet's captured request or response.

## Identity, caching, and provenance

Endpoint topology and concurrency are attempt-level execution properties. They
do not change the semantic request and therefore do not enter a work id or model
deployment hash. Changing a capacity, renaming a replica, adding an equivalent
replica, or removing one must not force solver or judge model calls to repeat.

The mutable workspace and experiment revision may change to record the new pool
configuration, but ADR-003 work-node reuse remains available when the model
deployment, generation parameters, program, inputs, and model-visible execution
environment are unchanged.

Every executed call records:

- logical executor id and pool configuration hash;
- selected endpoint id and a sanitized endpoint identity hash;
- configured endpoint capacity;
- queue wait time, request start/end time, and latency;
- observed model id, deployment revision, and generation parameters;
- success or normalized/redacted failure; and
- call, packet, attempt, work, experiment-revision, and run identities.

Ordinary one-call packets retain a single `endpoint_pool.endpoint_id`. A packet
that fans out internally records `endpoint_pool.routes[]`, with request/call
counts, latency, queue wait, failures, and observed concurrency attributed to
each endpoint. Reports normalize both shapes into the same per-endpoint table.

Run summaries report configured total capacity, observed peak concurrency,
calls and packet successes/failures per endpoint, cumulative latency and queue
wait, and executed versus reused work. Credentials and authorization headers
are never recorded.

If two endpoints are intentionally not equivalent, their endpoint choice becomes
part of model deployment identity and the work key. They are then separate
experimental conditions, not replicas in this pool.

## Progress UX

Completion is no longer ordered by case number. The synchronized terminal emits
each packet's stable position and selected endpoint as completion occurs; the
final summary reports replica count, total capacity, and calls:

```text
[37/102] Solved case-037 · llama-cpp-17-18081
[38/102] Solved case-041 · llama-cpp-17-18080
[1/15] metatable-graph-partial · vote 2 · llama-server-10-11434
Endpoint pool  3 replicas · 8 slots · 423 calls
```

Case-level start, reuse, completion, and failure events retain the case id. UI
rendering must be synchronized so worker threads cannot interleave terminal
lines or corrupt counters.

## Implemented sequence

1. Bump the workspace schema to 11 and introduce validated endpoint-pool models.
2. Update setup, templates, example workspaces, inspection output, and reference
   documentation to use `endpoints` with explicit capacities.
3. Separate endpoint topology/capacity identity from semantic executor/model
   identity so scheduling changes preserve ADR-003 reuse.
4. Add the capacity-aware endpoint permit registry and cross-process slot locks.
5. Replace sequential endpoint packet batches with bounded local thread-pool
   execution while keeping process and harness stages unchanged.
6. Fan out judge-validation fixture votes through the same bounded endpoint pool,
   then aggregate them deterministically and persist per-endpoint route evidence.
7. Give every worker isolated DSPy/session/observer state and keep request
   recording atomic.
8. Implement bounded compatible-replica failover, endpoint cooldowns, fail-stop
   batch scheduling, in-flight draining, and immutable retry attempts on resume.
9. Make progress reporting thread-safe and add endpoint-level utilization to
   run summaries.
10. Validate with deterministic instrumented endpoint workers, an end-to-end
   three-slot run, and compilation of the MainSequence 2+2+4 LAN workspace.

## Acceptance criteria

- Three instrumented fake endpoints configured for capacities two, two, and
  four observe no more than two, two, and four concurrent requests per
  endpoint, and no more than eight across the pool.
- A one-endpoint pool behaves identically to the current sequential executor when
  configured with capacity one.
- At least eight ready packets cause eight concurrent requests when all
  three fake endpoints are healthy and response times overlap.
- Solver and judge profiles can share the pool without sharing mutable DSPy or
  observer state.
- Aggregate content and case ordering are independent of request completion
  order.
- Connection, provider-server, and rate-limit failures try each other eligible
  replica at most once and record every physical attempt.
- Non-transport failures are never rerouted.
- A terminal request failure stops new scheduling, drains already in-flight
  work, and retains successful results.
- Resume creates a new attempt only for retryable transient endpoint failures,
  executes unresolved packets, and reuses already persisted work.
- Adding, removing, renaming, or resizing an equivalent replica does not change
  reusable work ids.
- Changing the model deployment revision or any output-affecting model parameter
  invalidates the correct work and downstream nodes.
- Endpoint ids, URLs, errors, usage, peak concurrency, and per-endpoint latency
  are observable, while credentials remain absent from artifacts and logs.
- Concurrent artifact and ledger operations pass race-focused tests.

## Alternatives considered

### One `base_url` plus one global thread count

Rejected. It cannot express different server capacities, cannot route around a
failed server, and cannot prove that an eight-slot server and a four-slot server
were each respected.

### Duplicate executor definitions for every endpoint

Rejected as the primary UX. A stage selects only one executor today, so another
scheduler layer would still be needed. It also repeats the same model-stage
assignment and hides that the servers are equivalent replicas of one logical
deployment.

### Put concurrency on the model profile

Rejected. Capacity belongs to the serving endpoint, not to temperature,
reasoning effort, or model semantics. The same endpoint capacity must be shared
by solver and judge profiles and by concurrent local runs.

### Put only `parallelism` on each stage

Rejected as the capacity authority. Stage fan-out cannot prevent two stages or
runs from jointly exceeding a shared endpoint. A future stage field may lower
its own use, but it cannot exceed endpoint permits and is not needed initially.

### Asynchronous networking throughout the coordinator

Deferred. The current coordinator, DSPy execution, artifact store, and CLI are
synchronous. A bounded thread pool provides parallel blocking HTTP calls with a
smaller migration surface. The endpoint-permit abstraction can later back an
async implementation without changing workspace semantics.

## Consequences

Positive consequences:

- LAN inference capacity is represented explicitly and enforced;
- several equivalent servers combine capacity without duplicating stages;
- large case banks complete substantially faster;
- existing packet, validation, resume, and content-addressed reuse boundaries
  remain intact; and
- reports show which endpoint actually executed each call.

Costs and risks:

- execution, progress reporting, and tests become concurrent;
- operators must truthfully attest replica equivalence;
- the server must have enough memory and queue capacity for the configured
  parallelism;
- failures can arrive out of order and require deterministic draining; and
- independent data roots still require server-side admission control to enforce
  a machine-wide limit.

## Non-goals

This decision does not introduce a distributed worker cluster, dynamic
autoscaling, speculative execution, automatic capacity discovery, request
batching, or silent retry. It does not pool semantically different models or
deployment revisions. Codex CLI and artifact-exchange harness concurrency remain
separate concerns because their isolation and resource contracts differ from an
OpenAI-compatible HTTP endpoint.
