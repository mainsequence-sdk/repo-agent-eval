# Interpreting Results

Every terminal run writes a deterministic report bundle under
`<data-root>/summary/runs/<run-id>/`. The bundle includes Markdown, CSV, JSON,
and per-case evidence.

Benchmark reports contain:

- each test case's continuous score, criterion scores, critical violations,
  judge feedback, split, and repetition;
- the arithmetic mean test score;
- cross-validation candidate means and the selected configuration for an
  optimization experiment;
- solver and judge usage; and
- source, case, split, program, model, judge-validation, and experiment-lock
  identities.

There is no framework-defined pass rate or per-case pass/fail status. Interpret
scores with their rubrics and inspect results by skill and capability so strong
performance in one area cannot hide a systematic weakness elsewhere.

Compare runs only when their evaluation identities are compatible. A solver or
fitted-program change is an intended candidate difference; a rubric, case,
source, judge, or split change creates a different evaluation contract.

Pilot reports omit benchmark means and remain diagnostic.

## Context-window outcomes

Endpoint summaries report `preflight_checks`, `preflight_rejections`, and
`preflight_skips` separately from model calls. A rejection means the exact
rendered request plus its configured output reservation exceeded a concrete
endpoint's advertised context window; no inference call was made there. A
rejection followed by a successful call on another endpoint is a normal pooled
reroute, not a failed case.

A stage-level `context_window_exceeded` error means every eligible endpoint was
known to be too small. It is an execution failure, not a solver-quality score
and not a DSPy parsing failure. When preflight is `skipped`, the endpoint lacks
the optional exact-count contract; the request is still sent and any provider
context error is recorded normally. The framework never estimates a token
count from characters to authorize or reject a benchmark request.
