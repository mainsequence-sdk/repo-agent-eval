# Incremental Execution and Cross-Run Reuse

MS Agent Eval executes an experiment as a content-addressed graph. Starting a
new run does not imply rerunning every model call. The coordinator reuses an
existing result only when it can prove that the requested computation is
semantically identical and that the earlier result was validated and persisted.

This guide describes the implemented schema-v17 behavior. The design rationale
and complete identity rules are in
[ADR-003](../architecture/003-content-addressed-experiment-graph.md).

## The three identities

```text
baseline@8d12c0…                    experiment revision
  └── solve work sha256:61af…      reusable semantic computation
        ├── attempt sha256:92bc…   execution in run A
        └── attempt sha256:b20d…   cache attachment in run B
              └── result sha256…  validated output plus provenance
```

- `experiment_revision_id` identifies the selected experiment's effective
  configuration and immutable evaluation inputs. The full workspace hash is
  retained as provenance, but changing an unrelated experiment does not change
  this revision.
- `work_id` identifies a stage computation independently of a run UUID,
  timestamps, leases, worker names, or credentials.
- `attempt_id` identifies one run-specific execution, retry, replicate, or
  cache attachment. `packet_id` is the attempt-envelope identity and currently
  equals `attempt_id`.
- `result_id` identifies validated output artifacts plus executor, observed
  model, usage, timing, and provenance.
- `run_graph_id` identifies the concrete work/result nodes materialized by one
  completed run, including fresh stochastic results and cache attachments.

A retry keeps the same `work_id` and creates a new `attempt_id`. A new run of
the same experiment revision also creates new attempts, but exact hits consume
zero model calls.

## What is in a work ID

All output-affecting data belongs in the canonical work document:

- stage-specific semantic inputs and dependency artifact hashes;
- instruction artifact and output contract;
- executor adapter and output-affecting configuration hash (not equivalent
  endpoint topology or capacity);
- model configuration hash, including deployment revision, reasoning effort,
  and generation parameters; and
- model-call and token limits that can change the produced output.

Run UUIDs, timestamps, leases, worker identities, API keys, and wall-clock
accounting are excluded.

The coordinator stores each canonical document at:

```text
operations/manifests/graph/work/<work-hash>/work.json
```

Attempts and results are separate:

```text
operations/manifests/graph/attempts/<attempt-hash>.json
operations/manifests/graph/results/<result-hash>.json
operations/manifests/graph/work/<work-hash>/results/<result-hash>.json
```

The final path is a coordinator-owned eligibility record. A candidate response
cannot make itself reusable merely by writing bytes with the correct hash.

## Case projections and precise invalidation

Every loaded case exposes four hashes:

- `solver_view_hash`: prompt and solver-visible attachments;
- `judge_view_hash`: prompt, rubric, expected response, expected artifacts;
- `metadata_hash`: title, ids, classification, coverage, and source paths; and
- `content_hash`: the complete reviewed case package.

Stage work uses the narrowest valid projection. Solver work never hashes the
rubric, expected answer, expected artifacts, or judge feedback. Judge work
hashes its judge view, the exact candidate response, the producing solver work
and deployment identities, the ordering-consistent judge-validation artifact,
the judge program,
and the configured number of judge repetitions.

Judge-validation model work similarly hashes only fixture ids, candidate
responses, and judge-visible case views. Private fixture labels remain outside
the LLM input. The coordinator recomputes ordinal consistency and set identity
locally. An inconsistent result is retained for its run but is not reusable.

Consequently:

| Change | Solver | Judge | Aggregate |
| --- | --- | --- | --- |
| Case title only | reuse | reuse | rebuild report metadata |
| Rubric or expected answer | reuse | execute | execute |
| Solver prompt | execute | execute | execute |
| Solver deployment revision | execute | execute for changed responses | execute |
| Judge deployment or reasoning | reuse | execute judge validation and judging | execute |
| Unrelated experiment | reuse; same revision | reuse; same revision | reuse |

The solver packet still carries case id, split, and repetition for reporting.
Those envelope fields are rebound when a cached semantic response is attached
to another run; they do not leak hidden judge material into the solver key.

## Configuring trustworthy deployment identities

Model names are often mutable aliases. Every semantic deployment therefore
requires an operator-controlled revision, while role profiles reference it:

```yaml
deployments:
  qwen-local:
    executor: lan-inference
    model: qwen3.6:27b-coding
    revision: qwen3.6-27b-coding-local-2026-09-14
models:
  qwen-solver:
    deployment: qwen-local
    reasoning_effort: high
    parameters:
      temperature: 0.2
      max_tokens: 4096
```

Use a weights digest when the serving system exposes one. Otherwise use a
revision value your team controls. Change it whenever weights, quantization,
tokenizer, chat template, backend version, tool policy, or other hidden serving
state changes. Failing to change it can incorrectly reuse a result from a
different deployment.

Credentials never enter the hash.

Equivalent endpoint replica ids, URLs, and capacities are also excluded from
the semantic work id. They remain frozen in the experiment revision and are
recorded on each executed attempt. This is safe only because
`deployments.<id>.revision` attests that every eligible route serves the same
output-affecting model deployment. An endpoint without that deployment in its
`routes` is never considered, regardless of unused capacity.

## Reuse policies

Every stage requires an explicit policy.

Normal exact reuse:

```yaml
stages:
  solve:
    reuse: {policy: exact}
    budget: {model_calls: 1, tokens: 32000, wall_seconds: 600}
```

Force a fresh result:

```yaml
reuse: {policy: refresh}
```

Request independent solver samples:

```yaml
reuse:
  policy: replicate
  samples: 3
```

`replicate` is solver-only because author, judge validation, fit, select, and
aggregate stages each require a single unambiguous output in the current
pipeline. Replicates deliberately bypass exact reuse. They share semantic work
identity but receive distinct replicate slots, attempts, results, and usage.
This is different from `evaluation.judge.repetitions`, which asks the judge for
multiple votes over one candidate response.

## Eligibility and single-flight execution

An exact hit must be:

1. completed rather than failed;
2. schema-valid and stage-valid;
3. durably persisted by the coordinator;
4. indexed under the exact same `work_id`; and
5. backed by content-addressed output artifacts that still verify.

Failed, rejected, incomplete, corrupt, missing, or unindexed results are misses.
Automatic executors take a hash-bucketed cross-process work lock, check the
index again after acquiring it, and only then spend model capacity. Concurrent
runs requesting the same miss therefore serialize publication and the later
run attaches the verified result.

On a hit the coordinator creates a normal run-local attempt/result lifecycle.
The attached result records:

```json
{
  "cache_hit": true,
  "reuse_policy": "exact",
  "source_work_id": "sha256:...",
  "source_result_id": "sha256:...",
  "source_attempt_id": "sha256:...",
  "source_run_id": "...",
  "source_executor": {},
  "source_model": {},
  "source_usage": {},
  "artifacts_verified": true
}
```

The current attempt records `model_calls: 0` and `tokens: 0`. Original usage
remains attached to the source result and is never counted as newly consumed.

## Running and reading the result

The command is unchanged:

```bash
uv run ms-agent-eval run baseline \
  --workspace experiments/mainsequence-sdk/workspace.yaml
```

The terminal labels fresh work with `✓` and reused work with `↺`, including
case progress and the source run. It prints the experiment revision as
`baseline@<short-hash>`. `--json` keeps the same facts in structured events and
the final result.

Every generated human-readable report includes an **Execution reuse** section
with executed and reused counts by stage. The JSON report adds:

```json
{
  "identity": {
    "experiment_lock_hash": "sha256:...",
    "run_graph_id": "sha256:..."
  },
  "execution": {
    "executed_this_run": 4,
    "reused_this_run": 19,
    "stages": []
  }
}
```

Each stage record contains `work_id`, `attempt_id`, `result_id`, `reused`, and
`source_result_id`. Each case provenance record identifies both solver and
judge work/attempt IDs and whether each was reused.

## Experiment revisions and storage

Starting a run persists one immutable revision bundle and a run reference:

```text
operations/manifests/
├── experiments/<family>/<revision>/
│   ├── experiment.lock.json
│   ├── bundle.json
│   └── runs/<run-id>.json
├── graph/
│   ├── work/
│   ├── attempts/
│   └── results/
└── runs/<run-id>/
    ├── run.json
    ├── transitions/
    ├── model-calls/
    └── traces/
```

Repeated runs of identical effective configuration share a revision directory.
A relevant change creates a new revision while unchanged work nodes can still
be reused from older revisions. A run manifest uses schema v4 and directly
references its immutable lock, bundle, and schema-v3 experiment definition.

Resume never adopts the current workspace silently. If the current selected
definition still hashes to the run's revision, it is used directly. If it has
changed, the coordinator verifies the run's stored definition and all of its
content-addressed files and repository snapshots, reconstructs that revision
in a disposable workspace, and continues the old run there. Use `run baseline`
instead of `run resume <old-id>` when the intention is to start the newly edited
revision. Unchanged work can still be shared between the old and new revisions.

The stored definition contains only effective revision inputs: the selected
experiment (and its base for optimization), stage/executor/deployment/profile configuration,
case-authoring policy, capability catalog, case packages, split assignment,
judge validation, resolved source context, and readiness result. Files such
as a cases-directory README are not frozen because they do not participate in
evaluation identity.

Automatic garbage collection treats graph manifests, experiment bundles, run
manifests, model-call evidence, and summaries as reachability roots. A result
shared by several revisions remains retained while any root references it.

## Operational checks

Use the existing commands to inspect identities and per-attempt state:

```bash
uv run ms-agent-eval inspect --workspace workspace.yaml
uv run ms-agent-eval stage list --run <run-id> --workspace workspace.yaml
uv run ms-agent-eval report build --run <run-id> --workspace workspace.yaml
```

When a server deployment changes, update `deployments.<id>.revision` before running.
When you explicitly need new observations, select `refresh` or a solver
`replicate` campaign instead of deleting stored evidence.
