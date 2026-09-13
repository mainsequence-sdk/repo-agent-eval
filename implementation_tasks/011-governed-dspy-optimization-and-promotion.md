# 011 — Governed DSPy Optimization and Held-Out Promotion

Status: Implemented on 2026-08-19; Main Sequence optimization correctly blocked by evaluator coverage
Priority: P0
Depends on: task 010

## Outcome

DSPy compilation is a separate, immutable optimization experiment. It cannot be
triggered as a side effect of benchmark execution. The governed flow is:

1. validate an optimization profile, grouped split manifest, program, provider,
   calibrated evaluators, and hard budgets;
2. load only train/development cases into an `OptimizerDatasetView`;
3. compile the canonical program in a separate Python process that receives no
   held-out paths or content;
4. publish the optimization lock and DSPy state-only JSON artifact externally;
5. only then open test/challenge cases and compare base versus compiled programs;
6. publish case-level authoritative evaluations and comparison evidence;
7. require an explicit promotion call after non-regression, minimum-delta, and
   anti-gaming/challenge gates pass.

No pickle or full-program artifact is accepted. A benchmark must identify the
compiled manifest/content id explicitly; an optimizer never silently replaces
the base program.

## Implementation

`ms_agent_eval.programs.dspy.optimization` provides the protected split boundary,
content-hashed optimization lock, subprocess compiler, held-out comparison, and
promotion record. The subprocess input contains only canonical base state and
train/development examples. Its working directory is disposable and the
framework does not pass the external data root.

The first supported production profile is deterministic `LabeledFewShot`. More
powerful instruction optimizers such as GEPA/MIPROv2 remain gated until the
relevant suite has enough calibrated train/development evaluators and their
provider/cost behavior has dedicated acceptance evidence.

`BudgetLedger` sits outside DSPy and enforces:

- model-call count;
- configured cost;
- observed total tokens;
- elapsed wall time;
- concurrent calls.

`ObservedDspyLM` reserves and completes ledger entries around every real model
call, so optimizer settings cannot bypass the outer limits. DSPy's internal
optimizer search is not claimed to be resumable. A failed framework attempt is
retained externally and a retry begins a new locked compile; a completed JSON
candidate can be loaded and evaluated independently.

## Held-Out and Promotion Invariants

- Group ownership is validated by `SplitManifest`; a group cannot cross roles.
- The optimizer-facing type has only `train` and `development` fields.
- Test/challenge loading requires an already-published JSON compiled manifest.
- Evaluator preflight covers every case before its role can be used.
- An evaluator error aborts comparison and cannot become a zero score.
- Base and candidate use the same held-out cases and authoritative service.
- Promotion defaults to false and requires an explicit `approved=True` call.
- The promotion artifact references the complete held-out comparison artifact.

## Main Sequence Readiness Result

The committed
`mainsequence-v2-few-shot-readiness` experiment and
`labeled-few-shot-small` profile are valid configuration, but execution is
intentionally rejected before any model call. The current v2 split contains
uncalibrated train/development cases: only `or-001` has an active evaluator,
while the rest are explicitly manual or not evaluable. This is the correct
trust-gate outcome, not an implementation failure.

Before a real Main Sequence optimization can run, maintainers must add calibrated
evaluators (and challenge cases) or author a smaller independent suite/split with
adequate train, development, untouched test, and anti-gaming coverage. Test cases
must never be reclassified into training merely to make compilation run.

## Verification

Synthetic Python 3.12 tests prove that:

- only train/development loaders run during compilation;
- the compiler process id differs from the coordinator process;
- base state is unchanged and compiled state contains demonstrations;
- lock, compiled manifest, evaluation, comparison, and promotion artifacts are
  content-addressed outside Git;
- held-out data loads only after the compiled artifact exists;
- base-versus-compiled comparison and explicit promotion work;
- call/concurrency/token budget state is preserved when work is rejected;
- the real Main Sequence v2 optimization fails closed on evaluator coverage.

Live model optimization is not claimed: the environment has no configured
Ollama endpoint, and task 009's provider acceptance gate remains open.
