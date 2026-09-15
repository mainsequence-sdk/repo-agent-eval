# ADR-013: Queued Recorder Workers and Incremental Projection

- Status: Accepted and implemented
- Date: 2026-09-15
- Decision owners: MS Agent Eval maintainers
- Scope: recorder-adapter isolation, durable queues, worker deployment,
  projection phases, cursors, coalescing, retry, and final report generation
- Extends:
  [ADR-003: Content-Addressed Experiment Graph](003-content-addressed-experiment-graph.md)
  and
  [ADR-006: Experiment Recorder Projection](006-experiment-recorder-projection.md)
- Supersedes: ADR-006's in-process background synchronization and synchronous
  terminal flush

## Context

The recorder is a derived view of canonical experiment evidence. It cannot
change a model response, score, stage result, benchmark identity, or reusable
Merkle node. Nevertheless, the original implementation requested projection
after practically every packet. Projection scanned accumulated state, rebuilt
tables and traces, rendered HTML/SVG/Plotly assets, and contacted MLflow from a
thread owned by the experiment process.

For `n` packets, repeatedly projecting facts `1..n` approaches `O(n²)` work.
Optimization makes this especially costly because candidates and folds create
many packets. The behavior also lets recorder CPU, filesystem I/O, network
latency, and process shutdown delay inference despite recording being
semantically irrelevant to the experiment result.

This is not an MLflow-specific problem. Any future recorder could perform
expensive serialization, uploads, indexing, or visualization. The boundary
must therefore isolate the generic recorder adapter rather than special-case
MLflow.

## Decision

Every recorder operation executes behind a durable `RecorderQueue`. The
experiment coordinator commits canonical evidence and then appends a small,
pointer-only `ProjectionIntent`. It never constructs a complete recording
plan, imports an external recorder client, renders reports, or waits for an
adapter.

A separately launched `RecorderWorkerService` claims queue items, reads the
canonical evidence, constructs a phase-specific recorder-neutral projection,
calls the configured `ExperimentRecorder`, advances a durable cursor, and
acknowledges the queue item.

```text
                    experiment process

 model/stage work → canonical commit → enqueue intent → continue/exit
                                          │
------------------------------------------│------------------------
                                          ▼
                         durable RecorderQueue
                                          │ claim + lease
                                          ▼
                  independent RecorderWorkerService
                       │         │            │
                       │         │            └── final reports
                       │         └── RecorderProjector
                       └── ExperimentRecorder.apply(...)
                                      │
                                      ├── MLflow
                                      └── future adapters
```

The queue boundary applies to live metrics, stage tables, traces, final charts,
comparison reports, and external run finalization. Terminal execution may
enqueue a high-priority finalization intent, but it may not bypass the queue.

## Dependency rule

The four responsibilities are separate:

1. `ExperimentCoordinator` owns semantic execution and queue production.
2. `RecorderQueue` owns durable scheduling, leases, retries, and coalescing.
3. `RecorderProjector` owns recorder-neutral derived structures.
4. `ExperimentRecorder` owns translation into one external system.

Coordinator, runner, stage executor, model provider, DSPy program, and
aggregation modules may depend on queue-domain types. They may not import
MLflow or instantiate a recorder adapter. Adapter construction occurs only in
doctor, worker, or one-shot projection commands.

## Queue intent

The portable intent contains identifiers and a high-water mark, not content:

```json
{
  "schema_version": 1,
  "intent_id": "sha256:...",
  "recorder_id": "local-mlflow",
  "recorder_config_hash": "sha256:...",
  "workspace_id": "mainsequence-sdk-evaluation",
  "run_id": "...",
  "experiment_revision_id": "sha256:...",
  "kind": "finalization",
  "target_sequence": 1900,
  "priority": 100,
  "stage": null,
  "boundary_id": null
}
```

It contains no prompts, responses, expected answers, rubrics, trace bodies,
tables, charts, credentials, or `RecordingPlan`. The worker reads authorized
content from the canonical store under `content_policy`.

`intent_id` is deterministic from semantic queue fields; timestamps are not
part of identity. Publishing the same logical boundary twice is idempotent.

The intent kinds are:

| Kind | Purpose | Queue behavior |
| --- | --- | --- |
| `live_progress` | Counters, throughput, usage, endpoint load, errors | Coalescible and throttled |
| `stage_boundary` | Stage tables, summaries, and traces | Durable |
| `finalization` | Full reports, charts, evidence, comparisons, terminal state | Durable, highest priority |
| `reconcile` | Repair missing intent or ambiguous delivery | Durable |
| `backfill` | Project retained historical evidence | Durable |

## Evidence and projection cursors

The projection target is derived without scanning accumulated packets:

```text
target_sequence = latest stage-transition sequence
                + latest run-lifecycle sequence
```

Both sequences are monotonic within a run. Stage transitions already include
the persistence boundary for provider calls and stage results. Report
publication and terminal state are lifecycle facts. These counters do not
participate in stage work or result identity.

Each `(recorder_config_hash, run_id)` stores a `ProjectionCursor`:

```json
{
  "schema_version": 1,
  "recorder_config_hash": "sha256:...",
  "run_id": "...",
  "projected_through": 1900,
  "finalized_through": 1900,
  "updated_at": "2026-09-15T12:00:00Z"
}
```

A worker skips an intent already covered by the cursor. It creates a
`ProjectionBatch` containing only metric, table, artifact, and trace identities
not already present in the external binding. Recorder calls are therefore
incremental and idempotent even when a full canonical snapshot is used for
explicit reconciliation.

If execution crashes after canonical commit but before enqueue, an idle worker
compares run high-water marks with cursors and creates a `reconcile` or
`finalization` intent. The repair scan belongs to the worker, never the
experiment path.

## Projection phases

### Live

Live projection emits metrics and scope state only. It does not build tables,
traces, charts, table browsers, report indexes, evidence bundles, or local
summary pages. Pending live intents for the same run and recorder collapse to
the latest target. `minimum_interval_seconds` prevents a fast worker from
recreating a live projection after every packet.

### Stage boundary

Stage projection emits tables, summaries, and traces. It deliberately skips
Plotly, SVG, HTML dashboards, complete evidence indexes, and cross-arm
comparison reports. Paused stage boundaries are explicit durable intents;
later required boundaries may subsume their evidence range without losing
their acknowledgement.

### Finalization

Finalization is an independent CPU job. It produces the complete sortable
tables, Plotly and SVG reports, execution DAG, paired test comparisons,
evidence index, configured full-content artifacts, and external terminal state.
It runs once for a finalization cursor identity. Experiment completion and
recorder publication are distinct statuses.

## Incremental adapter contract

The worker converts a phase-specific `RecordingPlan` into a `ProjectionBatch`:

```python
@dataclass(frozen=True)
class ProjectionBatch:
    batch_id: str
    plan_id: str
    recorder_config_hash: str
    from_sequence_exclusive: int
    through_sequence_inclusive: int
    phase: str
    metrics: tuple[MetricPoint, ...]
    tables: tuple[TableArtifact, ...]
    artifacts: tuple[ArtifactDescriptor, ...]
    traces: tuple[TraceDescriptor, ...]
```

The adapter protocol uses `apply(batch, binding)`. Adapters must use stable
item identities and deterministic external paths. MLflow uses explicit run ids
and the local binding's delivered-item set; it does not rely on global active
run state.

Recorder configuration remains operational. Changing the recorder, queue,
tracking URL, projection policy, or trace sampling changes recorder projection
identity but not experiment revisions, model requests, work ids, results, or
reuse eligibility.

## Queue semantics

The initial queue is filesystem-backed beneath the external data root. The
protocol permits future PostgreSQL, Redis, SQS, or other durable backends
without changing coordinator or adapter semantics.

The filesystem backend provides:

- atomic publication;
- priority claims;
- lease ownership and expiry recovery;
- bounded attempts and dead-letter state;
- deterministic idempotency;
- live intent coalescing and throttling;
- per-recorder/run cursors; and
- observable pending, leased, acknowledged, and dead-letter counts.

Queue pressure never throttles model inference. Optional live intents may be
coalesced or throttled; stage, failure, reconciliation, backfill, and
finalization intents remain durable.

## Workspace configuration

Schema 16 removes `live_and_finalize` and `finalize_only`. Those names combined
projection content with execution placement and implied coordinator delivery.

```yaml
schema_version: 16

recording:
  enabled: true
  recorder: local-mlflow
  queue: local-recorder-queue
  failure_policy: continue
  wait_on_run_exit: false
  artifact_policy: full
  content_policy: full
  projection:
    live:
      enabled: true
      minimum_interval_seconds: 15
    stage_boundary:
      tables: incremental
      summaries: true
    finalization:
      reports: true
      plotly: true
      evidence: true
    traces:
      optimization:
        policy: winners_failures_and_sample
        sample_rate: 0.05
      validation: {policy: full}
      test: {policy: full}

recorder_queues:
  local-recorder-queue:
    type: filesystem
    path: operations/recorder-queue
    live_coalescing: true
    lease_seconds: 300
    max_attempts: 12

recorders:
  local-mlflow:
    type: mlflow
    tracking_uri: http://192.168.1.17:5000
    experiment_name: ms-agent-eval/{workspace_id}
    authentication: {type: none}
```

`wait_on_run_exit` defaults to `false`. A future compliance runner may wait for
a queue receipt when it is true, but it still must not execute adapter work in
the experiment process.

## CLI and deployment

Normal execution only creates queue intents:

```bash
uv run --extra mlflow ms-agent-eval run optimize-solver \
  --workspace experiments/mainsequence-sdk/workspace.yaml
```

Deploy a long-running worker separately:

```bash
uv run --extra mlflow ms-agent-eval recorder worker \
  --workspace experiments/mainsequence-sdk/workspace.yaml
```

Drain currently available work and exit:

```bash
uv run --extra mlflow ms-agent-eval recorder worker --once \
  --workspace experiments/mainsequence-sdk/workspace.yaml
```

Project one run as a bounded scheduler job:

```bash
uv run --extra mlflow ms-agent-eval recorder project \
  --workspace experiments/mainsequence-sdk/workspace.yaml \
  --run <run-id>
```

The project and worker commands make no model calls. Workers may run on another
host when canonical storage and the queue are shared.

Status is explicit:

```bash
uv run ms-agent-eval recorder status \
  --workspace experiments/mainsequence-sdk/workspace.yaml \
  --run <run-id>
```

It reports canonical high-water mark, projected and finalized cursors,
projection lag, queue counts, and external recorder ids.

## Storage and garbage collection

All state remains outside Git:

```text
<data-root>/operations/
├── recorder-queue/<queue-id>/
│   ├── pending/
│   ├── leased/
│   ├── acknowledged/
│   ├── dead-letter/
│   └── state/<recorder-config-hash>/<run-id>/cursor.json
└── recorders/<recorder-id>/<recorder-config-hash>/
    ├── runs/<run-id>/binding.json
    └── comparisons/...

<data-root>/summary/runs/<run-id>/
├── index.html
├── tables/
└── graphs/
```

Pending, leased, and dead-letter required intents are operational retention
roots. Acknowledged live intents may be compacted after their receipt and
cursor are durable. Canonical manifest reachability remains authoritative for
experiment evidence.

## Failure semantics

- Recorder failure cannot fail or reopen a semantic experiment.
- A worker crash is recovered by lease expiration.
- A deterministic batch may be redelivered safely.
- Adapter errors are redacted before entering queue state.
- Exponential retry is bounded by `max_attempts`; exhausted work is visible in
  dead letter.
- Ambiguous delivery is resolved through binding and item identities.
- Reprojection to a new destination uses a new recorder configuration hash and
  does not call a model.

## Verification requirements

The implementation is covered by tests proving:

- live intents contain pointers only and coalesce;
- required boundary intents do not coalesce away;
- leases expire and recover;
- retry, dead-letter, acknowledgement, and cursor transitions are durable;
- completed experiments queue work without invoking an adapter;
- explicit workers project and finalize afterward;
- adapter errors cannot change run completion;
- historical projection performs no inference;
- frozen workspaces rehydrate with active operational queue configuration; and
- recorder destination changes do not alter semantic experiment identity.

## Consequences

Expensive recorder work now uses independent worker CPU and can outlive the
experiment process. Projection lag becomes visible and harmless. Final charts
may appear after experiment completion, and operators must run or schedule a
worker. The framework gains queue leases, retry, cursor, and dead-letter state,
but these operational concerns no longer contaminate the training path.

Rejected alternatives:

- An in-process thread still competes for CPU, memory, I/O, and process life.
- Synchronous terminal flush still blocks on the most expensive projection.
- Sending a full plan through the queue makes the producer perform the costly
  scan and serialization.
- Using MLflow as queue or source of truth couples correctness to one mutable
  external product.
- An unbounded event per packet moves the same garbage problem into queue
  storage; coalescing is required.
