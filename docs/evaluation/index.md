# Evaluation

Evaluation measures one locked solver against one locked semantic scoring
contract. A comparable result binds the source snapshot, cases, grouped splits,
judge-validation set, solver and judge programs, runtime, and model identities.

## Readiness

A scored run requires resolvable immutable sources, a complete capability
catalog, sufficient primary-case and leakage-group coverage, valid case
provenance, a valid train/test/challenge split manifest, resolved model
identities, and a complete judge-validation set.

The judge-validation stage checks ordinal consistency on reviewed training-case
fixtures before solver work begins. It does not classify evaluation cases or
define a numerical acceptance rule.

## Run types

| Run | Data use | Result |
| --- | --- | --- |
| Diagnostic pilot | Training cases only | Per-case diagnostics with `benchmark_valid: false`; no benchmark mean. |
| Baseline benchmark | Untouched test cases | Continuous held-out scores for the unfitted solver. |
| Optimization benchmark | Fixed group-safe validation selection within train, one final materialization, then untouched test | Continuous held-out scores plus the complete selection record. COPRO compares frozen original and optimized programs on identical test cases. |

The solver receives only its task, attachments, global instructions, and skill
context. The judge separately receives the candidate, rubric, expected
evidence, and skill context. It emits criterion scores, named critical
violations, and feedback. The framework validates and aggregates those outputs.

Reports contain per-case continuous scores, criterion details, critical
violations, the benchmark mean, model usage, execution/reuse evidence, and all
relevant identities. They contain no framework-defined case verdict or pass
rate.

See [Capability coverage](capability-coverage.md),
[judging and judge validation](judging-and-validation.md),
[pilot versus scored runs](pilot-vs-scored-runs.md), and
[interpreting results](interpreting-results.md). COPRO output details and the
complete asset contract are in [Formal COPRO summary](formal-copro-summary.md).
