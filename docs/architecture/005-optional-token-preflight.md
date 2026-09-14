# ADR-005: Optional Endpoint-Native Token Preflight

- Status: Accepted and implemented
- Date: 2026-09-13
- Decision owners: MS Agent Eval maintainers
- Scope: endpoint capability discovery, chat-template rendering, token counting,
  context-window admission, pooled routing, runtime failures, and provenance
- Refines:
  [ADR-004: Capacity-Aware Pooled Endpoint Concurrency](004-pooled-endpoint-concurrency.md)
- Preserves:
  [ADR-003: Content-Addressed Experiment Graph and Stage Reuse](003-content-addressed-experiment-graph.md)

## Context

An OpenAI-compatible generation endpoint does not provide a standard operation
for counting tokens or advertising its effective context window. Requiring a
tokenizer adapter or a manually configured context size would make otherwise
valid endpoints unusable and would contradict the framework's portable
execution contract.

Some servers expose useful non-standard, read-only extensions. The three
llama.cpp-compatible endpoints in the MainSequence experiment expose:

- `GET /props`, including an effective `n_ctx`;
- `GET /v1/models`, including model metadata and `n_ctx`;
- `POST /apply-template`, which renders chat messages with the server's active
  chat template; and
- `POST /tokenize`, which returns token ids without running inference.

Before the subsequent server restart, all three configured endpoints returned
the same token ids and rendered chat-template probe. The observed context at
that time was:

| Endpoint | `/apply-template` | `/tokenize` | Prior observed `n_ctx` |
| --- | --- | --- | ---: |
| `llama-cpp-17-18081` | available | available | 8192 |
| `llama-cpp-17-18080` | available | available | 8192 |
| `llama-server-10-11434` | available | available | 8192 |

The restarted servers advertise a larger context dynamically. That deployment
value is deliberately not copied into `workspace.yaml` or treated as permanent
configuration.

The need for admission control became visible when a solver request contained a
small case prompt but also the complete global instructions, selected skill,
and DSPy formatting contract. The final request contained about 40,000
characters and was rejected by an 8192-token server. Character length is not a
token count, and checking only the authored case prompt would have missed the
actual failure.

The framework therefore benefits from token preflight when an endpoint can
perform it. It must still support endpoints that cannot.

## Decision

Token preflight is an automatically detected, optional endpoint capability. It
is not a required executor adapter, workspace field, or condition for using an
OpenAI-compatible endpoint.

```text
final DSPy-rendered messages
             │
             ▼
 endpoint supports exact preflight?
       │                    │
      yes                   no
       │                    │
       ▼                    └──────────► dispatch normally
apply server chat template                         │
       │                                           ▼
       ▼                                  preserve any provider
tokenize rendered prompt                    context error
       │
       ▼
input tokens + output reservation <= n_ctx?
       │                    │
      yes                   no
       │                    │
       ▼                    ▼
dispatch request       reject before inference
```

The absence of a tokenizer capability never makes workspace validation fail.
When exact preflight is unavailable, the coordinator skips it and submits the
generation request normally. If the provider later rejects the request because
of its context window, the framework records that provider error as the stage
attempt outcome.

The framework must not silently truncate global instructions, skill context,
DSPy demonstrations, case prompts, candidate answers, or expected evidence to
make a request fit.

## Capability discovery

Capability discovery occurs at runtime and is bounded. Static commands such as
`validate` and `inspect` remain network-free.

The explicit diagnostic command is intentionally different: `ms-agent-eval
preflight <experiment>` performs the same bounded capability, template, and
tokenizer calls ahead of a run, but never makes a generation call and never
persists run evidence. It renders each unique static DSPy request, evaluates it
against every configured endpoint, and reports:

- configured pool capacity;
- maximum simultaneous exact-fit requests;
- maximum dispatchable concurrency including optional-capability skips;
- per-endpoint context, fit, rejection, and skip counts; and
- the minimum dispatch waves for the selected batch under endpoint capacities.

This is a diagnostic calculation, not an alternate admission policy. Dynamic
judge messages are deferred until the solver response exists, and optimized
solver messages are deferred until the fitted DSPy program exists; guessing
either request would violate the exact-request boundary.

For an endpoint whose configured OpenAI-compatible base URL ends in `/v1`, the
built-in llama.cpp probe derives the service root by removing that final path
component. It probes only the known read-only extension paths. It does not crawl
the service or infer arbitrary URLs.

Each capability has one of four runtime states:

| State | Meaning | Admission behavior |
| --- | --- | --- |
| `supported` | A bounded probe returned the expected schema. | Use the capability. |
| `unsupported` | The endpoint returned `404`, `405`, `501`, or an incompatible schema. | Skip preflight. |
| `unavailable` | Connection, DNS, or timeout prevented the probe. | Do not misclassify it as unsupported; normal endpoint health handling applies. |
| `unauthorized` | The extension requires credentials or rejects access. | Skip token preflight; retain the normal generation credential path. |

A failed optional capability probe must not be reported as a model-call failure.
It is attempt-level endpoint provenance. A generation endpoint that is itself
unavailable remains an endpoint failure under ADR-004.

Probe timeouts must be short and independent from the model generation timeout.
Probe responses are size-bounded. Capability results are cached for the run so
the coordinator does not probe before every case.

No user-supplied tokenizer adapter is required. Future provider-specific or
local tokenizer integrations may be added as optional capabilities, but their
absence cannot block an endpoint.

## Exact request boundary

Preflight must measure the final request, not an approximation assembled from
source files. The correct interception point is immediately after DSPy and its
adapter have rendered the request messages and immediately before the observed
LM submits the generation call.

The measured input includes:

- DSPy signature instructions and output-field contract;
- compiled demonstrations, when present;
- global context selected by the experiment;
- the complete selected skill context or other locked instruction context;
- the case task;
- for judge calls, the rubric, expected result, expected artifacts, and actual
  solver response; and
- system, role, separator, generation-prompt, and reasoning-control tokens added
  by the server's chat template.

For llama.cpp-compatible exact preflight, the coordinator uses this sequence:

1. Send the final chat messages and template-relevant controls to
   `/apply-template` with the generation prompt enabled.
2. Send the returned prompt to `/tokenize` with special-token handling matching
   the generation endpoint.
3. Count the returned token ids.
4. Compare that input count plus the configured maximum output tokens against
   the endpoint's advertised effective `n_ctx`.

If the coordinator cannot reproduce all template-relevant controls, the result
is not exact and must not be used to reject a benchmark request. The framework
then skips preflight for that request and lets the provider decide.

The admission equation is:

```text
required_context = rendered_input_tokens + maximum_output_tokens
fits = required_context <= effective_context_window
```

`max_tokens` is an output reservation, not the model's context size. An
8192-token endpoint with `max_tokens: 4096` has at most 4096 tokens available
for the fully rendered input.

## Pooled endpoint routing

Capability and context-window facts belong to each concrete endpoint, not only
to the logical executor pool. An endpoint permit may be used for a packet only
when either:

- exact preflight says the request fits that endpoint; or
- exact preflight is unavailable for that endpoint.

When one endpoint is known to be too small, the scheduler may route the packet
to another equivalent endpoint with sufficient context. A known-incompatible
endpoint must not receive that packet merely because it has a free concurrency
slot.

If every endpoint with exact preflight support is too small and no endpoint has
unknown capacity, the packet fails before inference with a normalized
`context_window_exceeded` error. If at least one otherwise healthy endpoint has
unknown preflight capability, normal policy may dispatch there; the provider is
authoritative.

ADR-004's replica-equivalence contract still applies. Different effective
context sizes may affect routing capacity, but endpoints in one pool must retain
the same semantic model deployment, tokenizer, and chat template. A discovered
chat-template or tokenizer mismatch is evidence that the pool's equivalence
attestation should be reviewed; it must not be silently treated as one shared
token-count cache.

Endpoint permits cover capability requests as well as generation requests so
preflight cannot exceed declared service capacity. Cached capability and token
facts should eliminate repeated preflight traffic where safe.

## Caching

An exact input-token count may be reused only under a key containing:

```text
model deployment revision
+ tokenizer capability fingerprint
+ chat-template fingerprint
+ template-relevant model controls
+ DSPy program hash
+ rendered-message hash
```

Equivalent endpoints with matching fingerprints may share a count. Each
endpoint still compares the shared count with its own effective context window.
An endpoint with a different or unknown fingerprint receives its own preflight
or follows the unknown-capability path.

Solver requests can be preflighted as soon as their final messages are rendered.
Judge requests must be measured after the solver answer is available because the
candidate response is part of the judge input. Compiled DSPy programs must be
remeasured when their demonstrations or instructions change.

Token-count records are operational facts, not semantic model outputs. They do
not change a stage work id or experiment revision. Successful stage-result reuse
continues to depend on ADR-003 identities. A context rejection is a failed
attempt and is never published as a reusable successful result.

## Failure and provenance

A preflight rejection records:

- endpoint id and endpoint configuration hash;
- model id and deployment revision;
- effective context window;
- rendered input token count;
- configured maximum output tokens;
- required total tokens;
- tokenizer and chat-template fingerprints;
- rendered-message hash;
- capability source and probe time; and
- normalized error kind `context_window_exceeded`.

It does not duplicate the full prompt in human-readable logs. The immutable
request artifact remains in the external artifact store under the existing
provenance contract.

When preflight was skipped, provenance records the reason, such as
`tokenizer_unsupported`, `template_unsupported`, `context_unknown`, or
`capability_unauthorized`. A later provider context error is retained with its
original sanitized message and normalized error kind. The framework must not
mislabel it as a DSPy parsing failure.

Terminal output should distinguish:

```text
context preflight rejected request
provider rejected request after preflight was unavailable
DSPy could not parse a completed model response
```

## Security and cost boundary

Capability probes use fixed, non-sensitive sample messages. Exact per-request
preflight sends the same locked prompt content to the same configured endpoint
that would receive the generation request; it does not introduce a new data
recipient.

`/apply-template` and `/tokenize` do not generate model output and are not model
calls for experiment budgets, usage, or scoring. Their latency and failures are
recorded separately as transport operations. They still consume endpoint
capacity and must remain bounded.

Preflight must use the endpoint's existing credential configuration without
persisting credential values. Probe responses and errors follow the same
redaction requirements as model calls.

## Consequences

Positive consequences:

- requests known not to fit are rejected before expensive inference;
- failures report exact input, output, and context-window counts;
- mixed-capacity pools can route work to compatible replicas;
- the measured payload is the real DSPy request, not the small authored case
  prompt; and
- generic OpenAI-compatible endpoints remain usable without extra adapters.

Tradeoffs:

- exact llama.cpp preflight adds extension requests on cache misses;
- optional capability discovery introduces attempt-level runtime state;
- unknown endpoints can still fail at generation time; and
- an 8192-token deployment still requires experiment content that actually fits
  8192 tokens. Preflight diagnoses that constraint but does not solve it by
  truncating evidence.

## Rejected alternatives

### Require every endpoint to configure a tokenizer adapter

Rejected. OpenAI-compatible generation remains useful without tokenization, and
the user explicitly should not have to provide an adapter merely to run an
experiment.

### Require a manually configured context window

Rejected as a universal rule. It duplicates mutable deployment state and may be
wrong. Automatic discovery is preferable when supported; otherwise the provider
remains authoritative at execution time.

### Estimate tokens from character count and reject requests

Rejected. Character-to-token ratios vary by tokenizer, language, Markdown, and
code. Such estimates may be shown diagnostically but cannot reject a benchmark
request.

### Truncate the prompt automatically

Rejected. Silent truncation changes the experiment input, can remove governing
instructions, and invalidates comparison with other runs.

### Treat missing `/tokenize` as an endpoint error

Rejected. `/tokenize` is not part of the OpenAI-compatible standard. Missing
optional extensions must cause preflight to be skipped, not make the model
endpoint unusable.

## Implementation

The coordinator now intercepts final DSPy messages through a routed LM,
acquires one endpoint permit per model call, performs bounded and cached native
preflight when available, and reroutes around replicas known to be too small.
The same path is used by solver calls, judge calls, and every independent judge
calibration vote.

Preflight facts are stored in endpoint attempt provenance and summarized in
terminal progress and generated reports. Static workspace validation remains
network-free. Focused tests cover exact admission, rejection, missing and
unavailable extensions, bounded responses, pool exclusion, mixed-pool
rerouting, zero-call rejection, and the existing concurrent solver/judge run.
