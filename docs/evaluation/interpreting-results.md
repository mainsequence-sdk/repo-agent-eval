# Interpreting Results

Every terminal run publishes a deterministic bundle at
`<data-root>/summary/runs/<run-id>/`. Start with `summary.md`, use `results.csv`
for analysis, inspect `cases/*.md` for the full prompt/response/rubric evidence,
and use `results.json` for lossless programmatic access. These files organize
persisted evidence with ordinary code; no LLM writes the summary.

A benchmark summary is useful only together with its locked identity and
per-case evidence. Mean score alone cannot show which capabilities failed,
whether a hard failure occurred, or whether two runs used the same correctness
contract.

## Summary fields

| Field | Interpretation |
| --- | --- |
| `execution_mode` | `benchmark` or `pilot`. |
| `benchmark_valid` | Whether aggregate benchmark metrics may be reported. |
| `coverage_status` | Coverage state captured when the run began. |
| `evaluated_splits` | Splits represented in the aggregate result. |
| `cases` / `case_count` | Per-case judge results and their count. |
| `mean_score` | Arithmetic mean of final case scores; benchmark runs only. |
| `pass_rate` | Fraction of cases whose rubric-defined pass decision is true; benchmark runs only. |
| `role_usage` | Solver and judge model-call/token evidence. |
| `execution` | Fresh versus reused work totals and per-stage counts. |
| `independent` | Whether solver and judge use different executors; not a claim that model ids differ. |

For each case, inspect criterion scores, declared hard failures, final score,
pass/fail, split, repetition, and judge feedback. Aggregate by skill,
capability, archetype, and difficulty during analysis so repeated strength in
one area cannot hide systematic failure in another.

Usage is incremental to the current run. A reused result reports zero new calls
and identifies its source result, attempt, and run in provenance. Historical
usage remains on that source and must not be added again to the current run.

## Comparing runs

Only compare records after verifying compatible evaluation identity:

- source commit and snapshot;
- case hashes and split assignments;
- authoring policy and capability catalog;
- judge model, program, repetitions, and calibration corpus; and
- runtime and experiment lock.

A solver model or compiled-program change is the intended candidate difference.
Changing a correctness-contract field creates a different evaluation and must
be labelled rather than reported as a regression or improvement.

Pilot results have no `mean_score` or `pass_rate` and must remain diagnostic.
See [Pilot versus scored runs](pilot-vs-scored-runs.md).
