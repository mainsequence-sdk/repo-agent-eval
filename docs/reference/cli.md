# CLI Reference

All commands accept `--workspace` when they operate on a workspace file.

| Command | Purpose |
| --- | --- |
| `ms-agent-eval setup` | Interactively create or inspect a workspace. |
| `ms-agent-eval models refresh` | Upsert the OpenRouter model list into the static catalog without deleting existing rows. |
| `ms-agent-eval models match <name>` | Rank catalog models for a local or deployment model name. |
| `ms-agent-eval init` | Create an explicit schema-v17 workspace and supporting files. |
| `ms-agent-eval validate` | Resolve and validate the complete static workspace contract. |
| `ms-agent-eval inspect` | Display resolved identities, readiness, budgets, and blockers. |
| `ms-agent-eval preflight <experiment>` | Expand the complete workload, then calculate endpoint-native token fit and usable concurrency without generation. |
| `ms-agent-eval run <experiment>` | Start a named evaluation or optimization and automatically manage recording when enabled. |
| `ms-agent-eval run resume <run-id>` | Continue an existing immutable run with the same automatic recording behavior. |
| `ms-agent-eval stage list` | List packet attempts and states for a run. |
| `ms-agent-eval stage prepare` | Prepare a stage packet independently. |
| `ms-agent-eval stage claim` | Lease a packet to an external worker. |
| `ms-agent-eval stage execute` | Execute a packet using its configured automatic executor. |
| `ms-agent-eval stage submit` | Validate and submit an external result. |
| `ms-agent-eval stage retry` | Create a new immutable attempt after failure. |
| `ms-agent-eval report build --run <run-id>` | Build or verify deterministic Markdown, CSV, JSON, and per-case reports without model calls. |
| `ms-agent-eval report build --all` | Render every retained run for one workspace. |
| `ms-agent-eval recorder doctor` | Verify recorder connectivity and, by default, perform a temporary artifact/table/trace write probe. |
| `ms-agent-eval recorder status --run <run-id>` | Show queue counts, cursor lag, publication state, and external ids. |
| `ms-agent-eval recorder project --run <run-id>` | Project or backfill one run without model calls. |
| `ms-agent-eval recorder worker` | Optional recovery/distributed consumer for durable recorder-queue work. Normal runs do not require it. |
| `ms-agent-eval recorder worker --once` | Drain currently available recovery work and exit. |
| `ms-agent-eval codex login` | Provision dedicated subscription authentication. |
| `ms-agent-eval codex status` | Inspect the safe authentication identity. |
| `ms-agent-eval codex preflight` | Validate image, network, CLI, Python, and authentication without inference. |

Common run options:

- `--pilot` selects a diagnostic train-only execution and is valid
  only for evaluation experiments.
- `--through <stage>` pauses after `author`, `validate_judge`, `optimize`,
  `fit`, `solve`, `judge`, `select`, or `aggregate` as applicable. COPRO search
  interleaves these stages, so `--through optimize`, `select`, or `fit` pauses
  after the selected search and final compilation boundary.

Use `ms-agent-eval <command> --help` for the exact arguments supported by the
installed version.

The `models` commands do not load `workspace.yaml`. They default to
`models/openrouter.json`; use `--catalog <path>` to select another file. See
[OpenRouter model catalog](openrouter-model-catalog.md) for the stored fields,
upsert behavior, and matching semantics.

Run the endpoint diagnostic before spending inference capacity:

```bash
ms-agent-eval preflight baseline --workspace workspace.yaml
```

The first tables always describe the complete experiment graph rather than
only the final case count. They include permanent train/test/challenge counts,
derived optimization-training and validation counts, every model/local stage,
the formula and logical work for each stage, COPRO target/round expansion, paired
test arms, model/deployment routes, configured endpoint slots, and totals by
role. The total is a fresh-run logical upper bound before semantic reuse.

After that expansion, the command checks judge-validation and solver requests
for the selected benchmark split. The later judge stage is shown as deferred
because its exact DSPy message includes the not-yet-produced solver response.
Repeat `--stage` with `validate_judge`, `optimize`, `solve`, or `judge`; repeat
`--case <case-id>` to narrow only the token-fit inspection; or use `--pilot` for
the train split. `--json` emits both `workload` and the complete per-request
matrix. A normal interactive `run` also prints a compact version of the case,
call, and COPRO search plan before its first model stage.

The command uses only bounded capability, chat-template, and tokenizer calls;
it makes zero generation calls and writes no run data. `guaranteed_parallel_capacity`
counts only exact native fits. `dispatchable_parallel_capacity` also includes
endpoints where optional native tokenization is unavailable because normal run
policy allows those requests through. A request rejected by every endpoint
makes the command exit with status 2.

For non-interactive `init`, pass `--solver-deployment-revision`,
`--judge-deployment-revision`, and optionally
`--author-deployment-revision` when the immutable revision differs from the
model name. Generated workspaces always store the field explicitly. It is a
cache boundary and must be bumped when hidden serving state changes.

`init --optimization-strategy labeled_few_shot` generates a demonstration
search. `init --optimization-strategy copro` generates a zero-shot solver
prompt/all-skills search and accepts independent `--optimizer-model`,
`--optimizer-deployment-revision`, and `--optimizer-reasoning-effort` values.
The optimizer role defaults to the solver deployment only when these are
omitted; it remains separately budgeted and recorded.

Use one `--api-base-url URL --api-max-concurrent-requests N` pair for a simple
single-endpoint workspace. To configure a pool, replace that pair with one or
more repeatable `--endpoint ID BASE_URL MAX_CONCURRENT_REQUESTS` arguments.
`--api-key-env` applies the named credential to each endpoint generated by this
command; edit `workspace.yaml` when replicas intentionally use different key
names.

`report build` accepts `--replace` when an existing derived bundle differs from
the current deterministic rendering. Operational manifests and blobs remain
canonical and are never rewritten by this command. Reporting resolves cases,
models, and provenance from the run's frozen experiment definition, so later
workspace edits cannot reinterpret an older run.

`recorder doctor --read-only` skips its temporary write probe. `recorder project`
is idempotent and makes no inference calls; it rebuilds a deterministic
projection from canonical local evidence. Recorder operations require the
optional dependency installed by `uv sync --extra mlflow`. See
[Experiment recording](../execution/experiment-recording.md).
