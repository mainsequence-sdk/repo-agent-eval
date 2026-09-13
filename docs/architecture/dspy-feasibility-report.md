# DSPy Feasibility Report

Date: 2026-08-19
Implementation task: `001-dspy-feasibility-and-prompt-parity-spike.md`
Decision: **adopt-with-constraints**

## Executive recommendation

Use DSPy as the default engine for newly authored, typed, optimizable model programs, behind the
engine-neutral `ProgramSpecification`, `ProgramResult`, and `ModelCallRecord` boundary proposed in
task 000. Keep `raw_messages` as a mandatory, DSPy-independent engine for historical replay,
wire-format controls, and causal comparisons.

The completed spike proved that the framework can capture DSPy's final adapter-rendered messages, preserve
target instructions as immutable inputs, project a framework-owned evaluator into DSPy, run both
few-shot and feedback optimizers without exposing held-out data, enforce provider-bound budgets,
save/load state-only JSON, and isolate concurrent configurations. It does **not** prove benchmark
quality, production provider compatibility, or optimizer-internal resume.

Task 002 may proceed only after this report is reviewed and task 000 is amended with the constraints
listed below. Its approved boundaries were reimplemented under
`src/ms_agent_eval/programs/dspy/`; the disposable spike project was then removed.

## Tested release and dependency surface

- DSPy: `3.2.1`, exactly pinned in the isolated spike project.
- GEPA: `0.0.27`, resolved transitively by that DSPy release.
- Python: baseline CPython 3.12 with an allowed range of `>=3.12,<3.15`; also tested with
  CPython 3.14.3.
- Lock SHA-256: `e073f3af9343ac4cd284a0c609b3590534954ab00648040dcae94c3540689b43`.
- Lock surface: 67 packages including the spike and development dependencies.
- Root runtime dependency files contain no DSPy reference.

The isolated dependency tree includes LiteLLM, OpenAI's Python library, GEPA, diskcache, and
cloudpickle. Recorded license metadata for the selected high-impact packages is MIT/MIT License,
Apache-2.0, BSD-3-Clause, or Apache 2.0. This is an inventory, not a legal conclusion or a CVE
audit. Production adoption requires automated vulnerability and license-policy checks over the
resolved lock and built image.

Two dependency behaviors require special treatment:

1. importing the DSPy stack caused LiteLLM to attempt an HTTPS model-cost-map refresh, then fall
   back to its bundled map when network access was unavailable;
2. cloudpickle is installed transitively even though the spike prohibits every pickle and
   full-program save/load path.

The raw engine was separated from the DSPy observer module. A clean subprocess imports
`dspy_spike.raw_engine` without placing `dspy` in `sys.modules`. The production raw engine and core
package must preserve this dependency firewall.

## Combinations tested

| Provider | Adapter/path | Module | Optimizer | Result |
|---|---|---|---|---|
| deterministic dummy | raw two-message request | raw response | none | pass |
| DSPy `DummyLM` with framework observation | `ChatAdapter`, JSON fallback disabled | typed `Predict` | none | pass |
| DSPy `DummyLM` | `ChatAdapter` | typed `Predict` | `LabeledFewShot(k=2)` | pass |
| separate dummy student and reflection models | `ChatAdapter` | typed `Predict` | `GEPA`, 8 metric calls | pass |
| two concurrent dummy configurations | scoped `dspy.context` | typed `Predict` | none | pass |
| two spawned worker processes | per-process cache/context | typed `Predict` | none | pass |
| Ollama | raw `/api/chat` plus `ollama_chat/<model>` | typed `Predict` | none | not run; environment unconfigured |

The Ollama command exits successfully with an explicit skip when either `OLLAMA_BASE_URL` or
`OLLAMA_MODEL` is absent. No endpoint or model name is committed as a default. A real provider run
is still a production gate because the mandatory tests deliberately use no network.

## Raw historical parity

The raw path reconstructed the stored Ollama request for the historical SDK 3.17.33 run using its
saved system prompt, user prompt, model, temperature, and legacy request shape.

- normalized expected request hash:
  `sha256:6d264d470468901ccb1f0545a47feae7e88c2ca2f47fad42203d4c8b3e055549`;
- normalized reconstructed request hash: the same value;
- parity: true.

“Byte parity” here means equality of the canonical UTF-8 JSON representation after normalization;
it does not claim that inconsequential whitespace in the historical JSON file was reproduced.

## Raw versus DSPy prompt rendering

For identical resolved target-context and task values, both paths emitted two chat messages, but
they were intentionally not byte-equivalent.

- raw message hash:
  `sha256:007bae486c5bbbc771b3291d97e69a2bdb29b60dc04d53de0c46d67f5634f93a`;
- DSPy message hash:
  `sha256:62e6cad798b4b4897c8b320395482f78181830adec1821fcbde1f8e568e8c1b2`;
- line-diff artifact:
  `sha256:872062e2b9bfa6ee6c86bf4d1478c74fba52aa1b0cdd493627e542f22955bd7b`.

DSPy's differences are attributable to four observable categories:

1. signature instruction;
2. input/output field descriptions;
3. adapter field/completion markers;
4. typed-output constraints.

The target `global_context`, `instruction_context`, and `task` values were present byte-for-byte as
input values. Gold answers, rubric content, and evaluator feedback were absent. DSPy therefore
preserves the semantic source inputs, but it does not and should not masquerade as a byte-compatible
legacy renderer. Exact replay belongs to `raw_messages`.

## Neutral results and provider observation

Raw and DSPy execution return the same neutral `ProgramResult` shape. Framework observation stores
provider/model identity, parameters, final rendered messages, request and response artifact
references, usage, latency, configured cost, status, and error classification. The fake provider
proved:

- typed success;
- typed parse failure (`typed_output_parse_error`);
- a timeout followed by a successful retry, retained as ordered failed/completed call records;
- a permanent failure with no retry;
- a terminal outer-budget rejection.

Contract/judge validation evidence is stored as
`sha256:ebcf84af1f6e7c1776434cc6bed736942c89b9d734a2ed90db80d2035aaf13d8`.

Observability limitations remain:

- dummy usage is deterministic zero-token usage and does not validate every real provider's usage
  or cost fields;
- a production binding must capture the final request at the actual provider boundary, not depend
  only on `inspect_history()`;
- real-provider retry metadata, cache-hit metadata, and model digest behavior remain unverified;
- the spike subclasses DSPy's `DummyLM` only for deterministic testing and does not justify a broad
  production fork of DSPy's LM interface.

## Typed contract and target immutability

The tested signature has exactly three input fields—`global_context`, `instruction_context`, and
`task`—and one output field, `response`. Extra or missing inputs fail validation. Expected answers,
rubrics, evaluator feedback, and test artifacts have no path into the student call.

Optimization changed signature instructions or demonstrations only. It did not mutate the target
snapshot or the three input values. The original program state remained unchanged after both
compiles.

## Metric and optimizer lifecycle

The deterministic framework-shaped evaluator is the single scoring source. `DspyMetricAdapter`
invokes it exactly once, stores its complete `DetailedEvaluationResult`, and projects either a
numeric score or DSPy's score-plus-feedback prediction. Evaluator exceptions raise
`MetricEvaluationError`; they are not converted into a legitimate zero score.

Judge validation outcomes were:

| Candidate | Score |
|---|---:|
| exact uppercase token | 1.0 |
| plausible lowercase answer | 0.0 |
| keyword-stuffed answer | 0.0 |

The production design derived from this spike exposes only the training cases
for fitting and grouped cross-validation. It has no test attribute or
test-loading method. Group overlap and duplicate IDs are hard errors. The
held-out loader is a separate API invoked only after model selection and the
final refit return.

Compile results:

- `LabeledFewShot(k=2)` added inspectable demonstrations, did not call a model, did not mutate the
  original program, and produced state-only JSON;
- GEPA used a distinct student model and reflection model, made 8 observed student calls and 2
  observed reflection calls, received evaluator feedback through the metric adapter, and produced
  an immutable optimization lock and selected state;
- the synthetic selected program scored 1.0 out of fold and 1.0 on held-out test;
- both few-shot and GEPA programs scored 1.0 on the synthetic held-out set.

Those scores validate lifecycle wiring only. The dummy provider was scripted, the dataset is tiny,
and the result is not evidence that optimization improves real prompts or real models.

Optimization summary artifact:
`sha256:d54f048194a876c01eca4d40b0e923f1ac555bc5a101ed314ce57664f1bada21`.
The optimizer-visible manifest hash is
`sha256:0dc07ecc4e7f89863e1943bc370ba468bda0a2f7b5d0ba1250364aee7208a6c0`;
the held-out manifest hash is
`sha256:a4055e7ad50b3bb9c09f34ac01781554fcd1306462a034daa813b2c713761096`.

## State, cache, and serialization

State was saved with DSPy's state-only JSON path and loaded into freshly instantiated, trusted
program code with `allow_pickle=False` and unsafe LM state disabled. `.pkl` and every non-JSON path
are rejected before DSPy is called.

- labeled-few-shot state:
  `sha256:4da2def2db4c6e529bcc757ad1e21a12a324385274dccfc1d8918adddaa4c21f`;
- selected GEPA state:
  `sha256:897745232c80bb3193b4655750c4cd2c4670390188e799aefb783fb3df15118f`;
- state round-trip evidence:
  `sha256:e79b8030fa3dcefe6f67a1ea3752ecd57a7ef38a72eca76906decee5de1a30b0`.

The round trip preserved effective instructions, demonstrations, deterministic output, and final
adapter-rendered messages. One representation detail is important: demonstrations can be DSPy
`Example` instances before save and ordinary mappings after load. The neutral inspection layer had
to normalize both representations. Production compatibility tests must compare semantic state and
rendered behavior, not Python object identity.

GEPA automatically reused its log directory when a command was rerun against the same artifact
root. That reuse produced zero new observed student/reflection calls and could have hidden the fact
that a compile was resumed. The CLI was changed so every invocation creates a unique run directory,
with its own DSPy cache and optimizer logs. Production run locks must state whether resume is
intended; a fresh run must never share an optimizer log directory accidentally.

## Budget behavior and resume

The outer ledger enforces model-call, configured-cost, wall-time, and consecutive-error limits at
the provider boundary. In the forced-abort experiment, the limit allowed one completed model call.
The next two attempts were rejected without reaching the fake provider, the last complete program
state was retained, and the lifecycle outcome was `budget_exhausted` rather than success.

Budget evidence:
`sha256:79c96b94925ce76d9f49494e1af4198e7817e9813c8f567090bdb6fe809d6ade`.

GEPA caught the provider exceptions and continued its bounded search loop. Consequently, a provider
ledger is sufficient to prevent cost after exhaustion, but not sufficient to terminate optimizer
control flow immediately. The production optimization worker needs a terminal cancellation signal
or process termination after persisting the last complete state. The pinned GEPA interface does not
provide a stable state-only internal-search resume contract; framework job retry must start a new
compile from an explicitly selected saved program state.

## Concurrency and isolation

Two concurrent scoped `dspy.context` executions retained their own model ids, messages, callbacks,
responses, and artifact roots. Two spawned subprocesses additionally retained distinct DSPy cache
roots. Isolation evidence:
`sha256:3feaa1117777e846c68159d805d28a4c39ab4e2e1ced2c2adde48f63b110d7ea`.

Scoped context passed this deterministic test, but process-global environment variables, import
side effects, provider clients, optimizer logs, and caches remain broader than a context. The
production recommendation is one initialized worker process per program/model optimization job.
Scoped context is acceptable only for controlled, non-overlapping calls within one job.

## Security and execution boundary

No Python from a target repository was imported or executed. Target files were read as untrusted
text inputs. No host shell, Docker socket, credential, unrestricted path, or target tool was exposed
to a DSPy program. Runtime responses, call traces, caches, GEPA logs, and compiled states were
written outside Git beneath a selected external data root.

Dependency surface evidence:
`sha256:226592aec2e96d48e3ae4db4d008beb9b594b75d21339483eac3bbce074363f5`.
Artifact paths in this report are intentionally represented by content ids; verification runs used
`MS_AGENT_EVAL_DATA_ROOT/<unique-run>/...` outside the repository.

## Required changes to task 000

Before task 002 starts, revise task 000 to make these findings normative:

1. Make process isolation mandatory for optimization and multi-model parallelism, while describing
   scoped context as a limited within-job option rather than an equivalent isolation boundary.
2. Namespace not only DSPy caches but also optimizer log/checkpoint directories by immutable run
   id. Treat any resume as explicit lock state and record whether provider calls were reused.
3. Specify that an outer provider ledger prevents additional paid calls but may not stop optimizer
   control flow. Add worker cancellation/termination and last-complete-state persistence to the
   budget-exhaustion protocol.
4. Keep the raw engine in a distribution/import graph that does not import DSPy, LiteLLM, or
   optimizer dependencies.
5. Require semantic state compatibility and rendered-message comparison across DSPy upgrades;
   serialized demonstrations may not preserve their in-memory Python type.
6. Add the LiteLLM import-time network attempt and transitive cloudpickle presence to the engine
   package threat/dependency review. Continue rejecting pickle even when the dependency exists.
7. Make a real configured Ollama raw/DSPy integration run, with usage and model identity evidence,
   a task-002 provider gate rather than claiming it was completed by this network-free spike.
8. Record out-of-fold model-selection estimates and held-out test results
   separately, and never expose the held-out loader to an optimizer-facing
   service or process.

## Decision and handoff

None of the task's mandatory defer conditions occurred: final dummy-provider messages were
capturable, state-only JSON preserved effective behavior, the raw engine did not require a DSPy
fork, process isolation passed, the optimizer API had no held-out access, evaluator failure was not
silently scored, and the outer budget prevented additional model calls.

The decision is therefore **adopt-with-constraints**. Task 002 is technically unblocked by the
feasibility result, but procedurally remains pending review of this report and incorporation of the
eight task-000 changes above.

## Current verification

```bash
uv sync --all-extras
uv run pytest tests/test_dspy_engine.py tests/test_metric.py tests/test_optimization.py
```

These production tests are network-independent. Live Ollama acceptance remains an
explicitly configured optional gate.
