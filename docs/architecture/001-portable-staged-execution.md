# ADR-001: Portable Staged Execution

- Status: Accepted
- Date: 2026-09-12
- Decision owners: MS Agent Eval maintainers
- Scope: Experiment orchestration, model execution, artifact exchange, and run provenance

## Context

MS Agent Eval currently assumes different execution mechanisms for different
parts of an experiment:

- case authoring is performed directly by the active Codex session;
- solver and judge calls are executed synchronously through an
  OpenAI-compatible endpoint;
- judge calibration is embedded inside the run before solver execution; and
- DSPy optimization and held-out evaluation are controlled by one monolithic
  runner invocation.

This prevents a run from pausing after one stage and continuing with another
executor. For example, a remote solver cannot produce locked candidate
responses which a separately selected endpoint or local harness judges later. It also
makes the orchestration model depend on transport details instead of experiment
semantics.

Every stage needs to be executable either by a configured remote service or by
a local harness such as Codex, without changing the stage's inputs, outputs, or
provenance contract.

## Decision

Adopt a portable staged-execution architecture. The experiment coordinator
will express all work as immutable stage packets and accept only validated
stage results. Execution location and transport are selected by a pluggable
executor adapter.

```text
Experiment coordinator
        │
        ├── author cases ───── remote endpoint OR local harness
        ├── calibrate judge ── remote endpoint OR local harness
        ├── solve cases ────── remote endpoint OR local harness
        ├── judge responses ── remote endpoint OR local harness
        └── optimize/validate ─ remote worker OR local process/harness
```

The coordinator owns experiment state and artifact identity. Executors perform
bounded work; they do not own the run or mutate earlier stage records.

## Terminology

- **Stage**: a bounded unit of experiment work with declared dependencies,
  inputs, outputs, and completion rules.
- **Stage packet**: immutable, content-addressed description of work ready for
  an executor.
- **Stage result**: immutable executor submission tied to exactly one packet
  and attempt.
- **Executor**: mechanism that claims or receives a packet and returns a result.
- **Endpoint executor**: invokes a model or worker through a configured remote
  API.
- **Harness executor**: gives the packet to a local agent runtime such as Codex.
- **Coordinator**: validates dependencies, creates packets, accepts results,
  advances run state, and persists the audit trail.

An executor is not a model. Model identity and execution mechanism are separate
configuration concepts.

## Stage lifecycle

Every stage follows the same logical lifecycle:

```text
prepare → claim → execute → submit → validate → persist
```

The durable states are:

```text
prepared → claimed → submitted → validated → persisted
              │           │           │
              └────────── failure / rejection / retry
```

An endpoint executor may perform claim, execute, and submit synchronously. A
harness executor may pause indefinitely after preparation while a local task
claims the packet. Both produce the same stage-result schema.

A stage is complete only after its result is validated and persisted. Process
completion, an HTTP success response, or a Codex final message alone does not
complete a stage.

## Canonical stages

The coordinator recognizes these stage kinds:

| Stage | Main input | Main output | Sensitive visibility |
| --- | --- | --- | --- |
| `author` | Locked instructions, capability catalog, source evidence, campaign | Candidate or promoted case packages | No solver responses, judge results, optimizer traces, or held-out feedback |
| `calibrate_judge` | Calibration fixtures, case rubrics, expected evidence | Typed calibration judgments and pass/fail result | May see calibration-only hidden material |
| `solve` | Global context, one skill context, case prompt | Candidate response and optional artifacts | Never sees rubric, expected response, or judge feedback |
| `judge` | Prompt, skill context, rubric, expected evidence, candidate response | Criterion scores, hard failures, feedback | Sees hidden judging material for its assigned case only |
| `optimize` | Train/development cases and permitted results | Published state-only DSPy artifact | Never loads test/challenge inputs or results |
| `final_evaluate` | Published optimizer artifact and untouched test cases | Solver responses and judge evaluations | Test data becomes visible only after publication |
| `aggregate` | Validated evaluation records | Run summary and report inputs | Deterministic; no model is required |

Source resolution, schema validation, hash verification, split calculation,
weighted arithmetic, and artifact persistence remain deterministic coordinator
operations. They may run in a local process or remote worker, but must not be
delegated to an LLM as semantic authority.

## Portable stage packet

Every packet contains at least:

```yaml
schema_version: 1
packet_id: sha256:...
run_id: 00000000-0000-0000-0000-000000000000
experiment_id: baseline
stage: solve
attempt: 1
dependencies:
  - sha256:...
inputs:
  - id: case
    content_id: sha256:...
    media_type: application/json
instructions:
  content_id: sha256:...
output_contract:
  schema_id: solver-response-v1
executor:
  id: remote-qwen
  adapter: openai_compatible
model:
  id: qwen3.6:27b-coding
budget:
  model_calls: 1
  tokens: 32000
  wall_seconds: 600
```

The packet stores content references rather than mutable source paths whenever
possible. It must include enough identity to reproduce the rendered request
without including credentials.

Stage-specific packet construction enforces information boundaries. In
particular, a `solve` packet cannot reference rubric, expected-answer, judge, or
held-out feedback artifacts.

## Portable stage result

Every result contains at least:

```yaml
schema_version: 1
packet_id: sha256:...
attempt: 1
status: completed
executor:
  id: remote-qwen
  adapter: openai_compatible
  version: pinned-version
model:
  id: qwen3.6:27b-coding
outputs:
  - id: judgment
    content_id: sha256:...
    media_type: application/json
usage:
  input_tokens: 12450
  output_tokens: 850
timing:
  started_at: ...
  completed_at: ...
error: null
```

Unsupported usage fields remain absent or explicitly unavailable; they are not
invented as zero. Failed results contain a normalized error kind and redacted
message. Secrets, raw credentials, and authorization tokens are never stored.

The coordinator verifies packet identity, attempt identity, output schema,
artifact hashes, stage-specific invariants, budgets, and dependency lineage
before accepting a result.

## Executor configuration

Executors are named once and referenced by stages. Durable endpoint and model
configuration belongs in `workspace.yaml`; only secret values remain in the
environment or provider credential store.

```yaml
executors:
  remote-qwen:
    type: endpoint
    adapter: openai_compatible
    base_url: http://192.168.1.10:11434/v1

  local-codex:
    type: harness
    adapter: artifact_exchange

stages:
  author:
    executor: local-codex
    model: gpt-5.6-sol

  solve:
    executor: remote-qwen
    model: qwen3.6:27b-coding

  judge:
    executor: remote-qwen
    model: qwen3.6:27b-coding
```

This example is directional; the implementation may nest stage assignments
under the existing `evaluation` and `experiments` sections. The separation of
model identity from executor identity is mandatory.

At minimum, the initial implementation supports:

1. `openai_compatible` endpoint adapter;
2. artifact-exchange harness adapter for prepare/claim/submit workflows; and
3. `codex_cli` automatic harness adapter defined by
   [ADR-002](002-isolated-codex-cli-execution.md), or `artifact_exchange` for a
   manually controlled local Codex workflow.

The core must not import Codex-specific types. Codex integration belongs in an
optional adapter package or extra.

## Harness operation

A harness can operate interactively or automatically:

1. The coordinator prepares and persists a packet.
2. The harness claims the packet with an attempt and lease identity.
3. The harness resolves only the permitted input artifacts.
4. The agent performs the task and produces the declared output schema.
5. The harness submits a result plus content-addressed output artifacts.
6. The coordinator validates and persists the result before unlocking dependent
   stages.

The artifact-exchange adapter makes manual and third-party harnesses possible
without embedding their SDKs in the core. The isolated Codex CLI adapter
automates the same lifecycle with one ephemeral CLI process inside a disposable
container. Its filesystem, authentication, instruction, and network boundaries
are defined by [ADR-002](002-isolated-codex-cli-execution.md).

## Run identity and persistence

One run id spans every stage. Each persisted stage result records:

- packet and result content hashes;
- stage kind, attempt, dependencies, and transition history;
- executor adapter, executor version, and execution location identity;
- configured and observed model identity;
- rendered requests and responses as redacted content-addressed artifacts;
- usage, latency, configured cost, and budget outcome when observable;
- output artifacts, validation outcome, and normalized errors; and
- the experiment lock, source snapshot, case, split, rubric, program, and
  compiled-artifact identities applicable to that stage.

All packets, results, and raw evidence remain under the external workspace data
root. They are not committed to the framework or experiment repository.

## Pause, resume, retry, and idempotency

- Any persisted stage boundary may be resumed without rerunning completed
  dependencies.
- Packet ids are deterministic from their locked inputs and stage contract.
- Attempts are append-only. A retry never overwrites a previous submission.
- Duplicate submission of identical bytes for the same attempt is idempotent.
- Conflicting bytes for the same packet and attempt are rejected.
- Claims use bounded leases so an abandoned harness does not permanently lock a
  packet.
- A changed model, executor, prompt, case, rubric, source, or compiled artifact
  creates a different packet identity.

## Evaluation integrity

Solver and judge are distinct stage roles, but may deliberately use the same
configured model identity. A judge must pass the locked calibration stage
before its judgments can complete the run. The MainSequence experiment uses
`qwen3.6:27b-coding` for both roles and records the role on every call.

The protocol permits a development run to reuse an authoring harness as the
judge, because executor portability and independence policy are separate
concerns. Such a run must record the shared executor/session relationship and
be marked non-independent. Benchmark-quality runs should use an independent
judge task and must never expose solver-only or held-out information across
stage boundaries.

Optimization retains the existing firewall:

1. train cases are available to compilation;
2. development results may guide the optimization run;
3. a state-only compiled artifact is persisted and published;
4. only then may `final_evaluate` load untouched test cases; and
5. test/challenge results never flow back into authoring or optimization.

## CLI direction

The CLI must allow complete runs and individual stages without creating two
execution models. A directional interface is:

```bash
ms-agent-eval run baseline --workspace workspace.yaml
ms-agent-eval run baseline --through author --workspace workspace.yaml
ms-agent-eval run baseline --through solve --workspace workspace.yaml
ms-agent-eval stage list --run <run-id> --workspace workspace.yaml
ms-agent-eval stage prepare judge --run <run-id> --workspace workspace.yaml
ms-agent-eval stage execute <packet-id> --executor local-codex --workspace workspace.yaml
ms-agent-eval stage claim <packet-id> --executor local-codex --worker codex-1 --workspace workspace.yaml
ms-agent-eval stage submit <packet-id> --result result.json --executor local-codex --worker codex-1 --lease <lease-id> --workspace workspace.yaml
ms-agent-eval run resume <run-id> --workspace workspace.yaml
```

Exact command names are an implementation decision. The required behavior is
that a full run is orchestration over the same independently executable stage
contract, not a separate shortcut implementation.

## Alternatives considered

### Keep the monolithic runner

Rejected. It cannot pause between solver and judge, cannot mix endpoint and
harness execution, and couples recovery to replaying earlier work.

### Support only model API providers

Rejected. It excludes local Codex tasks, human-supervised harnesses, and agents
whose execution contract is richer than a single completion endpoint.

### Add a special Codex branch to the runner

Rejected. A vendor-specific conditional would duplicate orchestration logic
and leave other harnesses unsupported. Codex is an adapter to the portable
contract.

### Allow arbitrary manual result files

Rejected. Unbound files do not prove which inputs, model, executor, or attempt
produced them. Harness submissions must bind to immutable packets and pass the
same validation as endpoint results.

## Consequences

### Positive

- Remote and local executors can be mixed within one auditable run.
- Expensive solver output can be judged later without rerunning the solver.
- Every stage can pause, resume, retry, and fail independently.
- Codex, other agent harnesses, and future providers use one protocol.
- Information firewalls become enforceable at packet construction time.
- Run provenance records actual execution rather than only intended model
  configuration.

### Costs and risks

- The runner becomes a state machine rather than a synchronous loop.
- Claim leases, retries, idempotency, and partial failure require explicit
  storage semantics.
- Harnesses may not expose complete token, cost, or latency information.
- Codex authentication and approvals remain concerns of the Codex adapter, not
  the experiment core.
- Existing pre-v7 workspaces and run manifests require a deliberate schema
  migration with no compatibility shim.

## Migration plan

1. Define versioned stage-packet, stage-result, transition, and executor schemas.
2. Add append-only stage persistence and run state reconstruction to the
   external artifact store.
3. Split the current runner into coordinator operations for calibration, solve,
   judge, aggregation, optimization, publication, and final evaluation.
4. Reimplement the current OpenAI-compatible path as the first endpoint
   executor.
5. Add artifact-exchange prepare/claim/submit commands and validate round trips
   without a model dependency.
6. Add the isolated Codex CLI harness executor defined by ADR-002.
7. Move case authoring onto the same portable contract while preserving its
   write/review/promotion boundaries.
8. Migrate workspace configuration to separate executors, models, and stage
   assignments; reject the old monolithic schema rather than retaining shims.
9. Prove full-run equivalence, mixed-executor runs, interruption recovery,
   information isolation, and held-out test protection.

## Acceptance criteria

This decision is implemented only when:

- every canonical stage can be prepared and persisted independently;
- endpoint and harness executors consume the same packet and result schemas;
- a remote solver run can pause and later resume with a local Codex judge;
- a full run invokes exactly the same stage operations as manual execution;
- completed stage results are never recomputed unless explicitly retried;
- solver packets cannot resolve hidden judging material;
- judge calibration remains a hard prerequisite for accepted judgments;
- optimization cannot resolve test/challenge inputs before compiled-state
  publication;
- all stage evidence is content-addressed outside Git;
- secrets are absent from packets, results, logs, and content identities; and
- tests cover restart, duplicate submission, conflicting submission, expired
  claims, executor failure, malformed output, and cross-stage leakage.
