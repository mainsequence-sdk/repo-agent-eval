# 001 — DSPy Feasibility, Prompt-Parity, Safe-State, and Isolation Spike

Status: Superseded by task 017; retained only as historical implementation evidence
Priority: P0 / pre-refactor decision gate
Estimated effort: 3–5 engineering days
Primary area: model-program abstraction and optimization feasibility
Depends on: `000-library-agnostic-refactor-analysis.md`
Blocks: tasks 002–013 and any production DSPy dependency

Result: [`../architecture/dspy-feasibility-report.md`](../architecture/dspy-feasibility-report.md)

## Summary

Build a disposable vertical spike that determines whether DSPy can safely serve as the canonical engine for newly authored model programs without taking ownership of the framework's source, Docker, storage, experiment, or evaluator boundaries.

The spike must compare two paths over the same immutable inputs:

1. a `raw_messages` path that reconstructs an existing model request byte-for-byte;
2. a DSPy `Predict` path using a typed signature and captured adapter-rendered messages.

It must also prove that a small DSPy optimization can run against a framework-shaped metric, save state as JSON, reload it, obey budgets, keep held-out data inaccessible, and remain isolated across concurrent configurations.

This is a decision task, not the beginning of the production refactor. The expected output is evidence and a clear `adopt`, `adopt-with-constraints`, or `defer` recommendation.

## Decision Being Tested

Proposed production decision:

- DSPy is the default engine for authored and optimizable model programs.
- A raw-message engine remains mandatory for legacy replay and causal controls.
- Framework-owned schemas are the persisted system of record.
- DSPy is pinned inside a separate engine package.
- Optimizers cannot run until evaluator calibration and protected data splits exist.

The spike must attempt to falsify this decision, not merely demonstrate that `dspy.Predict()` can return text.

## Questions the Spike Must Answer

1. Can the framework observe the exact messages DSPy sends after signature/module/adapter rendering?
2. Can raw and DSPy executions share one neutral run/result envelope?
3. Can target instructions remain immutable input fields rather than optimizer-controlled instructions?
4. Can expected answers and rubric data be kept completely out of student inputs?
5. Can provider identity, model parameters, usage, retries, errors, and rendered messages be normalized?
6. Can DSPy state be saved and loaded using JSON only?
7. Can the system reject pickle/cloudpickle artifacts without losing required functionality?
8. Can two program/model configurations execute concurrently without leaking DSPy's global settings?
9. Can a framework evaluator result be projected into a DSPy metric without duplicating scoring logic?
10. Can optimization be stopped by framework call/cost/time budgets and leave structured evidence?
11. Can train/development/test separation be enforced structurally rather than by convention?
12. Is DSPy's dependency and release surface acceptable behind a separate package boundary?

## Scope

### In scope

- one pinned stable DSPy release;
- a minimal neutral `ProgramSpecification` fixture;
- a minimal neutral `ProgramResult`/`ModelCallRecord` fixture;
- a raw-message renderer matching one historical request;
- a DSPy `Predict` program with typed fields;
- the DSPy `ChatAdapter` or current stable equivalent;
- deterministic fake-provider tests;
- one local Ollama integration run when the configured endpoint is available;
- capture and comparison of wire messages;
- typed success and parse-failure behavior;
- synthetic train/development/test data;
- one simple few-shot optimizer;
- one feedback-capable optimizer supported by the pinned release;
- evaluator-to-metric projection;
- JSON state save/load;
- process/context isolation experiments;
- external spike artifacts and a written recommendation.

### Out of scope

- production monorepo creation;
- migration of all cases or snapshots;
- production provider, storage, or executor plugins;
- optimizing any current Main Sequence instruction file;
- using current test cases as optimizer training data;
- claiming benchmark-quality improvements;
- fixing the existing evaluator implementation;
- repository-agent or tool execution beyond a restricted fake tool;
- weight fine-tuning;
- committing raw responses, traces, caches, or compiled candidates;
- loading any serialized Python program object.

## Safety and Repository Constraints

1. Put spike code under `spikes/dspy/`; do not move production files.
2. Give the spike its own dependency declaration and resolved lock.
3. Do not add DSPy to the root package's runtime dependencies.
4. Write runtime artifacts beneath an ignored external directory selected by `MS_AGENT_EVAL_DATA_ROOT` or a temporary directory.
5. Do not modify current authored cases, expected responses, rubrics, snapshots, or historical runs.
6. Do not import or execute Python from a fetched target repository.
7. Do not expose host shell, Docker socket, credentials, or unrestricted paths to a DSPy tool.
8. Do not use pickle/cloudpickle for state or cache interchange.
9. Do not treat a failed metric call as a legitimate training score.
10. Preserve all unrelated user changes in the dirty worktree.

## Required Spike Layout

```text
spikes/dspy/
├── README.md
├── pyproject.toml
├── uv.lock
├── src/dspy_spike/
│   ├── domain.py
│   ├── raw_engine.py
│   ├── dspy_engine.py
│   ├── provider_observer.py
│   ├── metric_adapter.py
│   ├── optimization.py
│   └── cli.py
├── fixtures/
│   ├── programs/
│   ├── datasets/
│   ├── provider_responses/
│   └── expected_messages/
└── tests/
    ├── test_raw_parity.py
    ├── test_dspy_rendering.py
    ├── test_typed_outputs.py
    ├── test_metric_adapter.py
    ├── test_optimization_splits.py
    ├── test_state_round_trip.py
    ├── test_budget_abort.py
    └── test_context_isolation.py

docs/architecture/
└── dspy-feasibility-report.md
```

This layout is disposable. Passing the task does not authorize copying spike modules into production without redesign and review.

## Neutral Spike Records

Define the smallest possible framework-shaped records without importing DSPy types into them.

```python
@dataclass(frozen=True)
class ProgramSpecification:
    id: str
    engine: Literal["raw_messages", "dspy"]
    schema_version: int
    content_hash: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class ModelCallRecord:
    call_id: str
    provider: str
    model: str
    parameters: Mapping[str, object]
    rendered_messages: tuple[Mapping[str, object], ...]
    request_artifact: str
    response_artifact: str
    usage: Mapping[str, object]
    status: Literal["completed", "failed"]
    error_kind: str | None


@dataclass(frozen=True)
class ProgramResult:
    outputs: Mapping[str, object]
    primary_response: str | None
    calls: tuple[ModelCallRecord, ...]
    engine_trace_artifact: str | None
    status: Literal["completed", "failed"]
    error_kind: str | None
```

These are feasibility shapes, not final production APIs. Their purpose is to prove that DSPy-native objects can be normalized without becoming persisted domain dependencies.

## Representative Inputs

Use two input groups.

### Historical prompt-parity input

Select one existing response-only case whose historical request can be reconstructed from:

- global instruction context;
- one instruction unit;
- one case prompt;
- existing legacy system-message literal.

The raw engine must reproduce the prior normalized provider request exactly. Do not use the existing evaluator for optimization. It may be invoked only to demonstrate result-envelope compatibility if its output is clearly labeled non-authoritative.

### Synthetic optimization dataset

Create a tiny, target-neutral dataset with grouped examples:

```text
group-a -> train examples and paraphrases
group-b -> development examples and paraphrases
group-c -> held-out test examples and paraphrases
```

The expected output should be objectively checkable by a deterministic metric. Include at least:

- a valid positive example;
- a plausible but wrong answer;
- a keyword-stuffed adversarial answer;
- a structured-output parse failure;
- a paraphrase that would leak if randomly split by row.

The optimizer-facing loader must expose only train/development examples. Test data must be physically or logically inaccessible through that API, not simply omitted by caller discipline.

## DSPy Program Under Test

Create a class-based or declaratively generated signature equivalent to:

```python
class InstructionResponse(dspy.Signature):
    """Answer the task using the supplied repository instruction context."""

    global_context: str = dspy.InputField()
    instruction_context: str = dspy.InputField()
    task: str = dspy.InputField()
    response: str = dspy.OutputField()
```

Use `dspy.Predict` for the main spike. Do not default to `ChainOfThought` or an agent module.

The effective signature instructions and demonstrations must be inspectable after compilation. Target context fields and case inputs must remain byte-identical before and after compilation.

## Raw Versus DSPy Prompt-Diff Experiment

For the same resolved input values and model profile:

1. render the raw legacy messages;
2. execute or render the DSPy program;
3. capture DSPy's final messages at the provider boundary;
4. normalize provider-irrelevant metadata;
5. produce a semantic and line-oriented diff;
6. classify every addition as signature instruction, field description, adapter marker, demonstration, output constraint, or provider transformation;
7. record message hashes and the selected adapter.

The report must state plainly whether DSPy can reproduce raw prompt semantics and whether byte parity is possible or desirable. It must not call two requests equivalent merely because their high-level inputs match.

## Provider Work

### Deterministic fake provider

The mandatory test provider must:

- return fixture responses without network access;
- capture the final request it receives;
- simulate typed success, malformed output, timeout, retryable failure, and permanent failure;
- return deterministic usage/cost fixtures;
- allow tests to assert that no gold/evaluator data was sent.

### Ollama integration

When `OLLAMA_BASE_URL` and a model profile are available:

- run the raw and DSPy variants once with identical sampling parameters;
- save request/response evidence externally;
- record model identity/digest when available;
- compare parsing, usage visibility, and latency;
- skip cleanly with an explicit reason when unavailable.

No internal endpoint or model name belongs in committed defaults.

## Metric Adapter

Create one framework-shaped detailed evaluator result:

```python
@dataclass(frozen=True)
class DetailedEvaluationResult:
    evaluator_id: str
    evaluator_version: str
    calibration_id: str
    score: float
    passed: bool
    feedback: str | None
    checks: tuple[Mapping[str, object], ...]
```

`DspyMetricAdapter` must:

1. call the framework-shaped evaluator exactly once;
2. preserve its complete result as the source artifact;
3. return the DSPy-supported numeric/bool shape for simple optimizers;
4. return score plus approved feedback for the feedback-driven optimizer;
5. distinguish evaluation mode from optimizer trace mode where required by the pinned DSPy contract;
6. propagate or classify evaluator failure according to explicit policy;
7. never reimplement the rubric.

Tests must prove that a keyword-stuffed bad output cannot pass the synthetic metric.

## Optimization Experiments

Run two small compiles:

### Compile A — wiring proof

Use a simple few-shot optimizer with a tiny budget. Prove:

- correct example input/output field mapping;
- demonstration provenance;
- train-only access;
- compiled program differs from the uncompiled program in an inspectable way;
- original program is not mutated;
- candidate state can be saved as JSON.

### Compile B — feedback proof

Use one feedback-capable optimizer available in the pinned stable release. Prove:

- evaluator feedback reaches the optimizer only through the adapter;
- teacher/reflection model identity is recorded separately from the student;
- call and cost budgets are enforced by an outer framework counter;
- development data may select a candidate;
- test data is used only after compilation is complete;
- development and test scores are reported separately.

The objective is lifecycle validation, not a statistically meaningful quality gain.

## State, Cache, and Artifact Policy

1. Save compiled state using DSPy's state-only JSON path.
2. Hash the base specification, JSON state, DSPy version, optimizer configuration, data manifests, metric identity, and models.
3. Reload the state into freshly instantiated trusted program code.
4. Compare effective instructions, demonstrations, and a deterministic fake-provider execution before/after reload.
5. Reject `.pkl`, full-program, or `allow_pickle=True` paths in spike code and tests.
6. Put DSPy caches under the external spike artifact root.
7. Namespace or clear caches between program/model/adapter experiments.
8. Record cache hits without allowing them to hide provider-call evidence expected by a test.

## Concurrency and Isolation Test

Run two configurations concurrently:

- different fake model ids;
- different adapter or signature configuration;
- distinct callbacks/observers;
- distinct output roots.

Test both scoped context and process isolation. The recommended production mode passes only if repeated stress runs show no cross-observed model id, message, callback, cache, or output.

If scoped in-process execution is ambiguous or depends on undocumented behavior, select one initialized worker process per program/model configuration for the production design.

## Budget and Failure Test

Implement an outer budget ledger independent of DSPy's optimizer settings:

- maximum model calls;
- maximum configured cost;
- maximum wall time;
- maximum consecutive provider errors.

Force a compile to exceed one limit. The spike must:

- stop additional model calls;
- retain the last complete candidate and all completed call records;
- write a structured `budget_exhausted` outcome;
- avoid marking the compile successful;
- demonstrate whether resume is supported or explicitly document why the selected DSPy optimizer cannot resume internally.

Do not promise optimizer-level resume if DSPy does not provide a stable checkpoint contract. Framework job resume and optimizer internal resume are separate capabilities.

## Required Artifacts

All artifacts go outside Git. The report references them by content id and redacted path.

Required artifacts:

- dependency lock and selected DSPy release identity;
- raw legacy request and hash;
- DSPy rendered request and hash;
- prompt-delta classification;
- normalized call records;
- typed success and failure envelopes;
- synthetic split manifests;
- metric calibration fixtures/results;
- optimization locks for compiles A and B;
- candidate and selected JSON states;
- state round-trip report;
- concurrency stress results;
- budget-abort evidence;
- dependency/license/security surface summary;
- final decision report.

## Decision Report

Create `docs/architecture/dspy-feasibility-report.md` with:

1. executive recommendation: `adopt`, `adopt-with-constraints`, or `defer`;
2. exact DSPy version and dependency lock hash;
3. supported provider/adapter/module/optimizer combinations tested;
4. raw-versus-DSPy prompt diff and interpretation;
5. observability gaps;
6. serialization and cache findings;
7. concurrency findings;
8. metric/optimizer lifecycle findings;
9. dependency and upgrade risks;
10. changes required to task 000;
11. whether task 002 may proceed.

## Acceptance Criteria

The task passes with `adopt` or `adopt-with-constraints` only when all are true:

- [ ] DSPy is pinned in the isolated spike project and absent from root runtime dependencies.
- [ ] The historical raw request is reconstructed byte-for-byte.
- [ ] The DSPy program uses typed inputs/outputs and the target context remains immutable.
- [ ] Final adapter-rendered messages are captured and hashed.
- [ ] Raw and DSPy executions emit the same neutral result envelope.
- [ ] No expected answer, rubric, evaluator feedback, or test artifact appears in student inputs.
- [ ] Typed parse failures are structured and do not masquerade as model-quality scores.
- [ ] Framework evaluator output is the sole source for DSPy metric score/feedback.
- [ ] Synthetic positive, negative, and adversarial calibration tests pass.
- [ ] Train/development/test grouping prevents paraphrase leakage.
- [ ] Both required optimization compiles complete within their budgets.
- [ ] An outer budget abort stops calls and writes structured evidence.
- [ ] State-only JSON round-trips under the pinned DSPy version.
- [ ] All pickle/full-program load paths are rejected.
- [ ] Concurrent configurations show no settings, callback, cache, or output leakage.
- [ ] Target code does not run in the DSPy process.
- [ ] Raw responses, traces, caches, and candidate states remain outside Git.
- [ ] The report makes a falsifiable recommendation and identifies unresolved risks.

The task results in `defer` if any of these remain unsolved:

- final wire messages cannot be captured reliably;
- safe JSON state cannot preserve required optimized behavior;
- provider support requires a broad framework fork of DSPy internals;
- process/context isolation cannot prevent cross-job configuration leakage;
- optimizer access to held-out data cannot be structurally prevented;
- evaluator failures are silently converted into optimizer training signals;
- budgets cannot stop further model calls;
- required behavior depends on an unstable/beta-only API with no acceptable pin.

`defer` is a valid completed outcome. In that case, task 000 keeps the neutral `ProgramEngine` boundary, promotes `raw_messages` as the first production engine, and records DSPy as a future optional integration.

## Verification Commands

The implementation task must document the exact environment-specific commands. At minimum:

```bash
uv sync --project spikes/dspy --frozen
uv run --project spikes/dspy pytest
uv run --project spikes/dspy python -m dspy_spike.cli raw-parity
uv run --project spikes/dspy python -m dspy_spike.cli render-diff
uv run --project spikes/dspy python -m dspy_spike.cli optimize --fixture synthetic
uv run --project spikes/dspy python -m dspy_spike.cli isolation-check
```

The default test command must be network-independent. Ollama integration is a separately selected test and must never fail merely because a developer has no local endpoint.

## Handoff

Do not begin task 002 until the decision report is reviewed and task 000 is updated with any constraints discovered by the spike. If adopted, production work must reimplement the approved boundary cleanly rather than promote disposable spike code by path move.
