# Formal COPRO Summary

A completed COPRO benchmark reports the effect of one selected instruction
layer against the original zero-shot program on the same untouched test cases.
A run optimizes either skills or global instructions and freezes the other
layer. This is a paired comparison, not a comparison of two unrelated runs.

## What is compared

For every test case and solver repetition:

```text
original score  = judge(original global + original skill response)
optimized score = judge(effective global + effective skill response)
delta           = optimized score - original score
```

Both responses use the same frozen DSPy signature, solver and judge deployments,
and locked case, rubric, and expected evidence. The report records whether the
effective change came from a skill overlay or one global bundle. Positive delta means the COPRO program
scored higher; negative delta means it regressed. The untouched test scores are
never sent back into COPRO.

The report presents:

- optimized mean score as the run's primary benchmark mean;
- original and optimized means;
- mean paired case delta;
- macro mean of per-skill deltas;
- improved, regressed, and unchanged skill counts;
- a deterministic 95% cluster-bootstrap interval over leakage groups;
- per-skill, per-criterion, and per-response-length comparisons; and
- every paired case row used by those summaries.

There is no framework pass threshold or pass rate.

## Exact expected-response token length

The old `easy`/`complex` case labels have been removed. They were subjective and
could not support a reproducible analysis axis. Instead, the framework counts
tokens in each hidden expected response using the exact tokenizer exposed by
the configured solver endpoint.

The measurement deliberately excludes the solver prompt, skill context, chat
template, and special tokens. It measures expected answer length—not total
request size and not semantic difficulty. Authors never enter the count in
`case.yaml`.

| Band | Exact expected-response tokens |
| --- | ---: |
| `001-128` | 1–128 |
| `129-256` | 129–256 |
| `257-512` | 257–512 |
| `513-1024` | 513–1,024 |
| `1025-2048` | 1,025–2,048 |
| `2049+` | 2,049 or more |

The measurement index records tokenizer provenance. If exact `/tokenize`
support is unavailable, token-dependent assets are omitted and the omission is
recorded. Character-based estimates are never substituted.

## Local report assets

Every completed run writes under
`<data-root>/summary/runs/<run-id>/`:

| Path | Contents |
| --- | --- |
| `summary.md` | Human-readable run, paired COPRO, model, execution, endpoint, skill, case, and failure summary. |
| `results.json` | Canonical report projection including `test_comparison` and token-measurement provenance. |
| `results.csv` | One row per judged response arm with candidate, strategy, score, token count, and provenance. |
| `cases/*.md` | Human-readable evidence for each original or optimized case result. |
| `index.html` | Local entry point linking the interactive charts and sortable tables. |
| `graphs/index.html` | Plotly metric explorer and described chart catalog. |
| `graphs/*.{html,svg}` | Interactive Plotly charts and deterministic static fallbacks. |
| `graphs/execution-dag.{json,svg}` | Complete machine graph and bounded rendered dependency graph. |
| `tables/index.html` | Searchable, sortable, paginated browser over every available projected table. |
| `tables/copro-case-comparison.csv` | Paired original/optimized score and delta for every case repetition. |
| `tables/copro-skill-comparison.csv` | Skill means and deltas, ordered by delta descending. |
| `tables/copro-token-band-comparison.csv` | Means and deltas by fixed expected-response token band. |
| `tables/copro-criterion-comparison.csv` | Means and deltas by rubric criterion id. |
| `tables/expected-response-token-counts.json` | Exact counts, bands, content hashes, and tokenizer provenance. |

The local report is authoritative. Open `index.html` from the filesystem; the
HTML and SVG files are not hidden inside the content-addressed blob store.
MLflow is a retryable projection of the same views and the underlying stage
evidence. Failed and cancelled runs still receive the operational chart and
table shell, while COPRO comparison entries explain that no paired test
aggregate is available.

## MLflow assets

With `artifact_policy: full`, the MLflow run records these COPRO-specific
assets in addition to the normal execution DAG, traces, identity documents,
endpoint tables, and report bundle:

### Graphs

Each basename below is recorded as interactive Plotly `.html` and a static
`.svg` export. Open the HTML artifact in MLflow to hover, zoom, select, toggle
series, and export the current view.

| Artifact | Question answered |
| --- | --- |
| `graphs/copro-overall-paired-score.{html,svg}` | Did the optimized program improve the overall untouched-test mean? |
| `graphs/copro-skill-lift.{html,svg}` | Which skills improved or regressed most? Rows are sorted visually by signed delta, with the strongest gains at the top. |
| `graphs/copro-case-paired-score.{html,svg}` | What were the original and optimized scores for every paired test case and solver repetition? Rows are ordered by delta. |
| `graphs/copro-case-delta-distribution.{html,svg}` | How broadly are gains and regressions distributed across cases? |
| `graphs/copro-delta-by-expected-tokens.{html,svg}` | Is lift related to exact expected-answer length? |
| `graphs/copro-score-by-token-band.{html,svg}` | How do original and optimized means compare in fixed response-length bands? |
| `graphs/copro-criterion-lift.{html,svg}` | Which rubric dimensions improved or regressed? |

### Tables

MLflow tables named `copro-case-comparison`, `copro-skill-comparison`,
`copro-token-band-comparison`, `copro-criterion-comparison`, and
`expected-response-token-counts` contain the machine-readable values behind
the charts. `case-results` also identifies each row's comparison arm,
candidate, strategy, and expected-response length.

Open `tables/index.html` in the parent run's **Artifacts** tab to browse these
as an actual searchable, sortable, paginated table. The adjacent hashed JSON
files use MLflow's required table storage format; they are not the intended
human reading surface.

These comparison tables and graphs exist only after both original and
optimized responses have been judged on the untouched test split and the
aggregate stage has completed. Before then, `graphs/manifest.json` records the
comparison assets as omitted rather than fabricating a partial comparison.

### Scalar metrics

The parent MLflow run records:

- `copro.baseline_mean_score`;
- `copro.optimized_mean_score`;
- `copro.micro_case_delta`;
- `copro.macro_skill_delta`;
- `copro.skills_improved`;
- `copro.skills_regressed`; and
- `copro.skills_unchanged`.

Use the tables for per-skill and per-case values rather than creating hundreds
of unstable scalar metric names.

## Reading the skill chart

The left marker is the original score and the right marker is the COPRO score.
The line connects the same skill under both programs. The signed value at the
right is the optimized-minus-original delta. Because rows are ordered by delta,
the strongest gains appear first and the largest regressions appear last.

Do not interpret response-length bands as difficulty. A short expected response
can demand difficult reasoning, and a long expected response can be routine.
The bands answer only whether performance changes with the expected amount of
answer material.
