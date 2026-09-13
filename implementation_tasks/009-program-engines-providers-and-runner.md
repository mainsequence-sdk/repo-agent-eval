# 009 — Raw/DSPy Engines, Provider Bindings, Evidence, and Runner

Status: Superseded by task 017; retained only as historical implementation evidence
Priority: P0 / model execution
Depends on: tasks 002–008
Unblocks: task 010; Ollama-backed production runs remain gated

> Superseded layout: task 014 preserved these boundaries as
> `ms_agent_eval.core`, `ms_agent_eval.programs`, and
> `ms_agent_eval.providers` subpackages inside one distribution. The separate
> distribution names below are historical implementation evidence.

## Outcome

Model execution is now split into neutral core contracts and independently
installable raw, DSPy, and Ollama packages. The same core runner wraps different
targets in one lifecycle/storage path; engines and providers contain no target
branches. Every model call records the final rendered messages plus immutable
request/response artifacts, usage, latency, parameters, status, and configured
cost.

## Package Boundaries

```text
agent-eval-core
  ProgramEngine / ModelProvider protocols
  ProgramInputs / ProgramResult / ModelCallRecord
  ModelCallObserver / ExperimentRunner

agent-eval-program-raw
  target-neutral configurable system/user message templates

agent-eval-program-dspy
  DSPy 3.2.1 Predict program, typed response, observed LM, JSON state

agent-eval-provider-ollama
  stdlib HTTP raw binding and optional observed DSPy binding
```

The current base `ms-agent-eval` install still requires only PyYAML. DSPy and
its transitive dependencies are installed through the `ms-agent-eval[dspy]`
extra; Ollama's raw binding uses the standard library.

## Raw Engine

The raw engine requires explicit `system_template` and `user_template` fields.
It interpolates only `global_context`, `instruction_context`, and `task`, sends
an ordered system/user message sequence through the provider protocol, and
stores a canonical trace. It has no built-in wording, target name, skill root,
or provider.

Target-specific legacy wording lives in:

```text
experiments/mainsequence-sdk/programs/raw-legacy-mainsequence.yaml
```

A regression test reconstructs the historical 3.17.33 request from its saved
global/unit/task inputs and proves canonical request bytes match the archived
Ollama request exactly, including model, message whitespace, `stream: false`,
and temperature.

## DSPy Engine

The production DSPy package cleanly reimplements the accepted spike boundary:

- exact pinned dependency `dspy==3.2.1`;
- `InstructionResponse` signature with three string inputs and one typed string
  response;
- `dspy.Predict` plus `ChatAdapter` with JSON fallback disabled;
- scoped LM/adapter context for a single response job;
- structured typed-output parse failure;
- normalized program trace and semantic state inspection;
- state-only `.json` save/load with `save_program=False`, `allow_pickle=False`,
  and unsafe LM state disabled;
- explicit rejection of pickle/full-program paths.

`ObservedDspyLM` disables LiteLLM retries so retry/budget policy remains owned
by the framework boundary. It records the actual adapter-rendered messages and
final provider payload through the same core observer used by raw calls. API
keys, headers, and credentials are excluded from observed parameters.

Optimization is intentionally not implemented in this engine task. Task 011
runs compilation in a separate process with train/development-only access and
loads the held-out test set only after JSON state publication.

## Ollama Bindings

The raw provider:

- validates a credential-free HTTP(S) endpoint;
- builds exact `/api/chat` JSON with model, ordered messages, `stream: false`,
  and configured options;
- normalizes token counts into common usage fields;
- returns structured failures through the observer;
- uses only the Python standard library.

The optional DSPy binding creates `ollama_chat/<model>` through
`ObservedDspyLM`, disables cache/retries, and retains adapter-rendered evidence.

The committed provider profile is an `.example` file and references
`OLLAMA_BASE_URL`; it contains no endpoint secret.

## Neutral Runner

`ExperimentRunner` receives an already locked job, program inputs, selected
engine, and provider. It:

1. validates engine identity;
2. compare-and-swap transitions planned/failed to running;
3. executes through the neutral engine protocol;
4. publishes an attempt-specific immutable result/failure manifest;
5. records artifact references in SQLite;
6. transitions to completed or structured failed state.

Retries use distinct attempt manifests, so history is never overwritten. A
test executes the synthetic `alpha` and `beta` targets through the same runner,
raw engine, provider, database, and artifact store; both finish without target
conditionals.

## Verification Evidence

On clean CPython 3.12.8 with DSPy 3.2.1:

```text
ruff check <all core/raw/dspy/ollama source and tests>
All checks passed!

pytest <core + raw + dspy + ollama + pack tests>
54 passed, 1 optional Docker test skipped
```

Covered behavior includes raw request/trace, typed DSPy success, structured
parse failure, JSON-only state round-trip, exact rendered DSPy messages, raw
Ollama request shape/usage, DSPy Ollama binding settings, historical request
parity, two-target runner execution, lifecycle transitions, and external
artifact persistence.

## Live Ollama Acceptance Gate

The required live raw-versus-DSPy provider test was attempted on
`http://127.0.0.1:11434`, but no Ollama service or `OLLAMA_BASE_URL`/
`OLLAMA_MODEL` configuration exists in this environment. Therefore:

- implementation and deterministic contract tests pass;
- the Ollama provider must remain opt-in/experimental;
- it must not be labeled production-accepted until a configured endpoint runs
  both raw and DSPy calls and their captured rendered payloads are reviewed.

This external gate does not weaken or bypass the generic provider interface and
does not block evaluator implementation with deterministic test providers.

## Acceptance Criteria

- [x] Raw and DSPy engines are separate optional packages.
- [x] Core imports without either engine or `mainsequence` installed.
- [x] Raw legacy request reconstruction is byte-identical.
- [x] DSPy returns typed success and structured parse failure.
- [x] DSPy state interchange is JSON-only; pickle is prohibited.
- [x] Raw and DSPy calls normalize through one model-call record contract.
- [x] Full rendered messages and request/response artifacts are retained.
- [x] Ollama has distinct raw and DSPy bindings with retries/cache disabled.
- [x] Two targets execute through one neutral runner without branching.
- [x] Attempts are externally persisted and resumable without overwrite.
- [ ] A real configured Ollama model passes raw and DSPy integration calls.
