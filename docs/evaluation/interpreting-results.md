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
