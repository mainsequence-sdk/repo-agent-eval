# Running an Experiment

Run static checks before spending model capacity:

```bash
uv run ms-agent-eval validate --workspace workspace.yaml
uv run ms-agent-eval inspect --workspace workspace.yaml
uv run ms-agent-eval preflight baseline --workspace workspace.yaml
```

`inspect` reports resolved commits and snapshots, selected instructions,
coverage gaps, case projection hashes, split integrity, model deployment
identities, reuse policies, judge-validation configuration, budgets, and experiment
revision hashes.

`preflight` is networked but generation-free: it renders the actual DSPy
messages, asks each configured endpoint for its optional native context and
tokenizer result, and reports how many configured slots can accept the batch.
It creates no run, manifests, blobs, or summaries. Use `--json` for the full
per-request/per-endpoint matrix, or repeat `--case` to diagnose selected cases.
Exact judge requests remain deferred until solver outputs exist.

## Choose a run

Diagnostic pilot while authoring is incomplete:

```bash
uv run ms-agent-eval run baseline --pilot --workspace workspace.yaml
```

Valid baseline benchmark after readiness passes:

```bash
uv run ms-agent-eval run baseline --workspace workspace.yaml
```

Grouped K-fold model selection followed by one held-out test evaluation:

```bash
uv run ms-agent-eval run optimize-few-shot --workspace workspace.yaml
```

When standard output is an interactive terminal, the command renders live
stage and case progress. It identifies the active case by title and id, shows
its skill, split, repetition, model, elapsed time, judge score and verdict, and
marks exact cross-run reuse with `↺` and the source run. It finishes with
executed/reused counts, the aggregate result, and external data location. Authoring shows
the candidate count and ids after the author model returns its campaign.

For scripts and CI, request the stable final JSON document explicitly:

```bash
uv run ms-agent-eval run baseline --json --workspace workspace.yaml
```

Output is also automatically JSON when redirected or piped. Live presentation
is driven by structured coordinator events; it does not alter packet identity,
model inputs, stored results, or execution order.

After the terminal aggregate is persisted, the coordinator automatically writes
`summary.md`, `results.csv`, `results.json`, and readable per-case reports under
`<data-root>/summary/runs/<run-id>/`. This is deterministic formatting over the
stored evidence, not an additional model stage. To backfill or verify an older
run without rerunning models:

```bash
uv run ms-agent-eval report build --run <run-id> --workspace workspace.yaml
```

The experiment id is the key under `experiments` in `workspace.yaml`; it is not
a global framework default.

## Pause and resume

```bash
uv run ms-agent-eval run baseline --through solve --workspace workspace.yaml
uv run ms-agent-eval stage list --run <run-id> --workspace workspace.yaml
uv run ms-agent-eval run resume <run-id> --workspace workspace.yaml
```

Resume uses the existing immutable run and packet identities. If the selected
workspace definition has changed, the coordinator verifies and rehydrates the
old run's frozen experiment definition instead of applying the edit to an
in-flight run. Starting the named experiment creates the new revision; resuming
the old run completes the old revision. A pilot must be resumed and operated
with `--pilot` so its execution mode cannot be confused with a benchmark.

Starting another run is not the same as forcing another model sample. Under the
default `exact` policy the coordinator attaches eligible validated work from
any compatible prior run. Use `refresh` or solver `replicate` intentionally
when fresh execution is the experiment. See
[Incremental execution](../execution/incremental-execution.md).

See [Pilot versus scored runs](../evaluation/pilot-vs-scored-runs.md),
[Training and optimization](../training/index.md), and
[Resume and recovery](../execution/resume-and-recovery.md).
