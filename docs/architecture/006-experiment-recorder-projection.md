# ADR-006: Experiment Recorder Projection and MLflow Adapter

> Optimization scope/table naming is refined by
> [ADR-011](011-stateful-optimization-and-semantic-reuse.md): active runs use
> one fixed validation holdout and stateful rounds rather than independent folds.

- Status: Accepted and implemented
- Date: 2026-09-14
- Decision owners: MS Agent Eval maintainers
- Scope: experiment recording, run lifecycle, metrics, traces, tables, graphs,
  artifacts, external-recorder delivery, recovery, and MLflow integration
- Extends:
  [ADR-001: Portable Staged Execution](001-portable-staged-execution.md)
- Preserves:
  [ADR-003: Content-Addressed Experiment Graph and Stage Reuse](003-content-addressed-experiment-graph.md)

> Schema note: ADR-009 refines the MLflow mapping to one Experiment per
> workspace and adds immutable cross-arm `model_comparison` runs. Historical
> per-experiment naming examples below document ADR-006's original decision.
>
> Execution note: ADR-013 supersedes coordinator-owned background projection
> and terminal flushing. Recorder work now executes only through durable queue
> intents claimed by run-managed or optional standalone consumers.

## Context

MS Agent Eval already persists the facts required to reconstruct an experiment:

- a frozen experiment revision and run identity;
- immutable stage packets, attempts, results, dependencies, and provenance;
- provider requests and responses, usage, latency, and errors;
- compiled DSPy program artifacts; and
- deterministic aggregate and per-case reports.

This local evidence is optimized for integrity, recovery, and cross-run reuse.
It is not, by itself, an experiment-tracking user experience. Comparing runs,
inspecting metric history, navigating traces, viewing execution graphs, and
finding artifacts currently requires reading several local manifests and report
files.

The terminal progress stream cannot close this gap. Progress events are
presentation-oriented and ephemeral. A process can fail after durable evidence
has been written but before a progress event reaches a consumer. Historical
runs also cannot be reconstructed from a live event stream.

The framework needs an experiment-recorder boundary that can support different
tracking systems without coupling experiment semantics to any one product. The
first adapter will use MLflow. MLflow Tracking organizes information into
experiments and runs and supports parameters, metrics, artifacts, tables,
datasets, and traces. Its run hierarchy is useful for experiment stages, while
its trace hierarchy is useful for individual stage attempts and model calls.

MLflow must not replace the content-addressed artifact store or stage ledger.
Making an external service authoritative would make evaluation correctness,
resume behavior, and cache reuse depend on service availability and mutable
remote state. It would also make switching recorders materially change the
experiment.

## Decision

An experiment recorder is a derived, retryable projection of canonical local
experiment evidence.

```text
solver / judge / optimizer
            │
            ▼
 canonical local evidence
 ┌───────────────────────────────┐
 │ frozen experiment revision    │
 │ run lifecycle                 │
 │ stage packets and results     │
 │ model calls and usage         │
 │ reports and artifacts         │
 └───────────────┬───────────────┘
                 │ read-only projection
                 ▼
 RecorderProjector → RecordingPlan
                 │
                 ▼
 durable outbox, bindings, receipts
                 │
                 ▼
 ExperimentRecorder protocol
       │
       ├── MlflowRecorder
       └── future recorder adapters
```

The ordering boundary is strict:

1. Persist canonical experiment state locally.
2. Derive a deterministic recording plan from that state.
3. Persist the delivery intention in a local outbox.
4. Project the plan to the configured recorder.
5. Persist external identifiers and delivery receipts.

A recorder operation never runs a model, changes a score, mutates canonical
evidence, or participates in stage-result cache eligibility.

The progress sink may wake the recorder worker after a local commit, but it is
not the source of recorder data. Every wake-up causes a rescan of canonical
state so a missed notification is repaired automatically.

## Source of truth and ownership

Canonical local evidence remains authoritative for:

- experiment and run identities;
- source, instruction, case-bank, split, model, and executor hashes;
- stage dependency and reuse decisions;
- prompts, responses, expected evidence, rubrics, and judge decisions;
- model-call usage and errors;
- compiled DSPy programs;
- aggregate scores and benchmark validity; and
- resume, retry, and garbage-collection behavior.

The external recorder owns only its projection:

- external experiment, run, trace, and artifact identifiers;
- recorder-native indexes and charts;
- delivery status and service-specific metadata; and
- links that make canonical evidence easier to navigate.

When local and external data disagree, the local evidence wins. A reconcile or
backfill operation repairs the external projection; it never rewrites the local
experiment to match MLflow.

## Semantic identity boundary

Recorder configuration is operational configuration. It is deliberately
excluded from:

- the experiment revision;
- stage work ids;
- stage result ids;
- model-call request identities; and
- cross-run reuse eligibility.

Enabling recording, disabling recording, changing the MLflow URL, changing the
MLflow experiment name, or switching to another recorder must not invalidate
completed work.

The framework computes a separate `recorder_config_hash` from the normalized,
non-secret recorder configuration. This hash identifies one projection
destination and policy. Secret values are excluded; stable references such as
`password_env: MLFLOW_PASSWORD` may be included.

The recording-plan schema version is part of each projected event identity.
Changing the projection schema can therefore create a new, auditable projection
without changing the underlying experiment result.

## Generic recorder domain

The core package defines JSON-neutral immutable records rather than importing
MLflow types:

```python
@dataclass(frozen=True)
class RecorderCapabilities:
    nested_runs: bool
    metrics: bool
    tables: bool
    artifacts: bool
    traces: bool
    trace_links: bool


@dataclass(frozen=True)
class RecordingPlan:
    schema_version: int
    recorder_config_hash: str
    experiment: ExperimentDescriptor
    run: RunDescriptor
    scopes: tuple[ScopeDescriptor, ...]
    metrics: tuple[MetricPoint, ...]
    tables: tuple[TableArtifact, ...]
    artifacts: tuple[ArtifactDescriptor, ...]
    traces: tuple[TraceDescriptor, ...]
```

The minimum adapter protocol is:

```python
class ExperimentRecorder(Protocol):
    def healthcheck(self) -> RecorderHealth: ...
    def ensure_experiment(
        self, descriptor: ExperimentDescriptor
    ) -> ExternalExperimentRef: ...
    def ensure_run(
        self, descriptor: RunDescriptor
    ) -> ExternalRunRef: ...
    def sync(
        self,
        plan: RecordingPlan,
        binding: ExternalBinding,
    ) -> RecorderReceipt: ...
    def finalize(
        self,
        binding: ExternalBinding,
        status: RunLifecycleState,
        end_time: datetime,
    ) -> RecorderReceipt: ...
```

`sync()` receives the desired projection of all currently available canonical
state. It is not a collection of fire-and-forget callbacks such as
`on_case_finished()`. A desired-state plan is deterministic, testable,
replayable, and suitable for historical backfill.

Adapters declare capabilities. When a recorder lacks a native feature, the
projector retains the data as portable JSON or another artifact rather than
discarding it. Recorder-specific degradation is recorded in the receipt.

## Canonical run lifecycle

The framework adds an append-only run lifecycle stream. Stage transitions alone
are insufficient because a run can pause, await an external harness, be
cancelled, or fail between stages.

The initial lifecycle vocabulary is:

```text
created
running
paused
awaiting_harness
failed
cancelled
completed
report_published
```

Each lifecycle entry contains:

- run id and experiment revision;
- monotonically increasing sequence;
- event id and schema version;
- state and reason code;
- timestamp;
- responsible process or executor identity; and
- references to the latest canonical stage transitions and reports.

Every coordinator exit path writes its lifecycle event after the relevant
canonical stage transition. A stale external `RUNNING` run can then be repaired
from durable local state after a crash.

MLflow terminal status maps as follows:

| Canonical state | MLflow status |
| --- | --- |
| `completed` | `FINISHED` |
| terminal `failed` | `FAILED` |
| `cancelled` | `KILLED` |
| `paused` | retain `RUNNING` and set a lifecycle tag |
| `awaiting_harness` | retain `RUNNING` and set a lifecycle tag |

`report_published` is an artifact-publication event, not a second terminal
state.

## Recording projection

`RecorderProjector` reads only canonical manifests and artifacts. For a run it
constructs a stable `RecordingPlan` containing:

- bounded identity tags and parameters;
- root and stage metrics;
- stage scopes;
- stage-attempt and model-call traces;
- case, criterion, calibration, endpoint, and optimization tables;
- graph and chart artifacts;
- the human-readable report bundle; and
- an artifact index containing local content hashes.

Every projected item has a deterministic event id derived from:

```text
recording-plan schema version
+ canonical source artifact or event id
+ projected item kind
+ logical destination path or scope
```

Projection never reads current workspace intent when a frozen run revision is
available. This preserves the meaning of historical runs after
`workspace.yaml`, cases, rubrics, models, or sources change.

## MLflow mapping

The MLflow adapter uses the low-level `MlflowClient` and explicit run ids. It
must not depend on process-global active-run state. Explicit identities are
required because stage packets execute concurrently and recorder delivery may
occur in a different process from model execution.

The mapping is:

| MS Agent Eval concept | MLflow representation |
| --- | --- |
| workspace and experiment family | MLflow Experiment |
| one local run UUID | parent MLflow Run |
| stage or optimization scope | nested child MLflow Run |
| one stage-packet attempt | Trace |
| one DSPy/provider invocation | child Span |
| final aggregate values | parent-run metrics |
| metric progress | metric history with stable steps |
| case and criterion results | tables and artifacts |
| case-bank split | dataset input |
| frozen revision and reports | artifacts |
| dependency and reuse relationships | DAG artifact and trace links |

### Experiment and run names

The default MLflow experiment-name template is:

```text
ms-agent-eval/{workspace_id}/{experiment_id}
```

This groups comparable baseline and optimization runs while preserving their
revision hashes as tags. One local run UUID maps to exactly one parent MLflow
run for a recorder destination.

The root run stores bounded tags for:

- local run id;
- experiment id, mode, and revision;
- workspace id and workspace hash;
- case-bank and split hashes;
- solver, judge, and author model identities;
- executor and deployment revision identities;
- framework version and Git revision;
- benchmark or pilot status; and
- recorder-plan and recorder-configuration hashes.

Large manifests and high-cardinality collections remain artifacts or tables,
not tags or parameters.

### Stage hierarchy

Nested child runs represent stable stages and optimization scopes, not
individual cases.

```text
baseline parent run
├── validate_judge
├── solve.test
├── judge.test
└── aggregate
```

```text
optimization parent run
├── optimize
│   ├── candidate-001
│   │   ├── fold-01
│   │   └── fold-02
│   ├── candidate-002
│   ├── select
│   └── final-fit
├── solve.test
├── judge.test
└── aggregate
```

Creating one child run per case is rejected because it produces an
unmanageable run tree. Case-level work belongs in tables and traces.

### Traces and spans

One stage-packet attempt becomes one trace. A normal trace is:

```text
stage-packet attempt
├── prepare-input
├── DSPy-program
│   └── provider-call
├── validate-output
└── persist-result
```

The trace records, when available:

- work, packet, attempt, and result ids;
- run and experiment-revision ids;
- stage, case, skill, split, and rubric identities;
- model name and deployment revision;
- executor pool and concrete endpoint;
- input and output token usage, latency, and configured cost;
- cache status and source result for reused work;
- normalized failure kind; and
- content hashes or full bodies according to content policy.

Dependency and reuse edges may be emitted as recorder-native trace links when
supported. The canonical graph artifact remains required because a nested run
or span tree cannot fully represent the stage DAG or cross-run reuse.

MLflow DSPy autologging is not the canonical integration. Global autologging
state is poorly aligned with concurrent explicit execution, does not provide a
reliable historical backfill path, and would duplicate the existing provider
observer. It may be offered later as an opt-in diagnostic aid.

### Metrics

Final scalar metrics on the parent run include:

```text
score.mean
score.stddev
score.minimum
score.maximum
cases.total
cases.succeeded
cases.failed
cases.reused
judge.calibration_passed
calls.total
tokens.input
tokens.output
tokens.total
cost.configured
latency.total_seconds
preflight.rejected
```

Metric histories may include:

```text
progress.completed_packets
progress.failed_packets
progress.reused_packets
usage.cumulative_input_tokens
usage.cumulative_output_tokens
usage.cumulative_cost
quality.running_mean
```

Metric steps are derived from canonical lifecycle or transition sequence, not
thread completion order. High-cardinality case, skill, criterion, endpoint,
and error breakdowns belong in tables so metric-name cardinality remains
bounded.

Reused work records zero new inference calls, tokens, latency, and cost. It also
records the source run, work id, and result id. Usage from the original run must
not be counted again in the consuming run.

### Datasets

The adapter logs the case bank and its train, development, test, and challenge
splits as dataset inputs when supported. The adapter may use a recorder-native
digest for indexing, but it also records the complete framework SHA-256 in a
tag or artifact. The framework hash remains authoritative.

### Tables

The portable recording bundle contains, where applicable:

```text
tables/
├── index.html
├── case-results/<content-hash>.json
├── criterion-results/<content-hash>.json
├── stage-attempts/<content-hash>.json
├── model-calls/<content-hash>.json
├── endpoint-usage/<content-hash>.json
├── judge-calibration/<content-hash>.json
├── optimization-candidates/<content-hash>.json
└── optimization-folds/<content-hash>.json
```

Table rows contain stable local identities and artifact references. Full prompt
or response bodies are not duplicated into every table row. `index.html` is a
self-contained searchable, sortable, paginated human view. Each JSON snapshot
is written once through the recorder's table capability. The generic artifact
stream must not upload another payload to the same path: doing so would destroy
recorder-native table metadata and schema.

### Graphs and charts

Graph construction is recorder-neutral and deterministic. The initial artifact
bundle is:

```text
graphs/
├── index.html
├── manifest.json
├── execution-dag.json
├── execution-dag.svg
├── scores-by-skill.{html,svg}
├── criterion-heatmap.{html,svg}
├── stage-latency-summary.{html,svg}
├── token-usage-by-stage.{html,svg}
├── endpoint-workload.{html,svg}
├── endpoint-saturation.{html,svg}
├── cache-reuse-by-stage.{html,svg}
└── optimization-validation.{html,svg}
```

`execution-dag.json` contains work, attempt, and result nodes plus dependency,
retry, production, and cache-reuse edges. The SVG is a bounded, collapsed
rendering of the execution scopes and the dependency/retry edges between them;
it must never degrade into one unconnected row per packet. `index.html` provides
an interactive Plotly latency/token call explorer and a chart guide.
`manifest.json` defines each chart's Plotly HTML and SVG paths, question,
population, unit, source table, interpretation, and limitations.

Plotly HTML is the canonical exploratory representation: it supports hover,
zoom, selection, legend filtering, and client-side export in MLflow's artifact
viewer. The recorder projection writes finalized HTML bytes into the durable
outbox rather than passing an in-memory Plotly object to the MLflow adapter.
That preserves content hashing, retry, historical backfill, and portability to
other recorders while producing the same artifact class supported by
`mlflow.log_figure`. Plotly uses its versioned CDN rather than embedding the
multi-megabyte JavaScript bundle into every chart; SVG is the offline and
static companion.

Static operational charts summarize rather than enumerate observations:

- stage latency shows p50, p95, and maximum executed-attempt duration and
  excludes exact cache reuse;
- token usage separately shows input, output, and provider-reported
  unclassified tokens from completed model calls;
- endpoint workload reports request distribution and must not be labeled
  utilization;
- endpoint saturation reports peak observed concurrency divided by configured
  capacity and must not be described as average utilization; and
- cache reuse is grouped by stage and explicitly counts stage results rather
  than claiming equivalent time or token savings.

Every SVG includes an accessible `<title>` and `<desc>` plus a visible concise
description. Every Plotly chart repeats that title and description and exposes
the source values in hover labels. Charts must be derived from the frozen run
and must use deterministic ordering and labels.

A chart is omitted when its required data does not exist. The artifact index
records the omission reason; the framework must not create misleading empty
charts.

### Artifact bundle

The adapter publishes a navigable, versioned artifact tree:

```text
index.html

identity/
├── run.json
├── experiment.lock.json
├── recorder-manifest.json
└── software-environment.json

reports/
├── summary.md
├── results.json
├── results.csv
└── cases/...

programs/
└── compiled-dspy-program.json

evidence/
└── artifact-index.json
```

The root `index.html` is the operator entry point. It links to the Plotly chart
explorer, the table explorer, and reports whether a final paired optimization
comparison is available.

The artifact index maps every external path to its local SHA-256, media type,
size, canonical source, and content policy. A newly generated report version is
written below a path containing its report hash; it does not ambiguously
overwrite evidence previously recorded for the run.

## Workspace configuration

Non-secret recorder configuration belongs in `workspace.yaml`:

```yaml
recording:
  enabled: true
  recorder: local-mlflow
  mode: live_and_finalize
  failure_policy: continue
  artifact_policy: full
  content_policy: full

recorders:
  local-mlflow:
    type: mlflow
    tracking_uri: http://127.0.0.1:5000
    experiment_name: "ms-agent-eval/{workspace_id}/{experiment_id}"
    authentication:
      type: none
```

The initial modes are:

| Mode | Behavior |
| --- | --- |
| `finalize_only` | Project only after terminal state or report publication. |
| `live_and_finalize` | Checkpoint during execution and reconcile at finalization. |

The initial failure policy is `continue`: recorder failure is visible but does
not fail model execution or invalidate the local result. A future strict
compliance policy may require successful delivery before the CLI returns
success, but it still cannot change semantic experiment identity.

For an authenticated server, configuration contains references rather than
secret values:

```yaml
recorders:
  shared-mlflow:
    type: mlflow
    tracking_uri: https://mlflow.example.com
    experiment_name: "ms-agent-eval/{workspace_id}/{experiment_id}"
    authentication:
      type: basic
      username_env: MLFLOW_USERNAME
      password_env: MLFLOW_PASSWORD
```

URLs, experiment names, adapter selection, policies, and timeouts are not
environment variables. Only credentials or secret tokens are resolved from the
environment, and their values are never persisted or hashed.

## Delivery ledger, bindings, and idempotency

The filesystem artifact store keeps recorder operations outside Git:

```text
operations/recorders/
└── <recorder-id>/
    └── <recorder-config-hash>/
        └── runs/
            └── <local-run-id>/
                ├── binding.json
                ├── events/
                ├── receipts/
                ├── errors/
                └── sync.lock
```

`binding.json` maps local identifiers to external identifiers:

```text
local run id       → MLflow experiment id and parent run id
local scope id     → nested run id
local attempt id   → trace id
local artifact id  → external artifact path and receipt
```

The outbox is append-only. A delivery receipt names the deterministic event id,
external identifiers, operation status, attempt count, timestamps, and a
sanitized error when applicable.

MLflow does not expose one universal idempotency key for every write. The
adapter therefore uses:

- canonical local ids as tags;
- deterministic child-scope names and artifact paths;
- deterministic metric key, step, and timestamp tuples;
- local delivery receipts;
- external search before creating a missing root or scope; and
- reconciliation after an ambiguous timeout.

If the process crashes after creating an MLflow run but before persisting its
binding, reconciliation searches for the tags
`agent_eval.local_run_id`, `agent_eval.recorder_config_hash`, and
`agent_eval.scope_id` before creating anything. A per-recorder/run file lock
prevents concurrent local processes from creating duplicate roots.

Trace creation has an unavoidable at-least-once edge when the external service
accepts a request but neither returns an id nor exposes it immediately through
search. The adapter minimizes this risk with canonical trace tags, search, and
receipts, and documents any unresolved duplicate as a recorder defect rather
than corrupting local evidence.

Unsynchronized outbox entries and their referenced artifacts are garbage-
collection roots. After a receipt confirms delivery under the configured
retention policy, normal artifact garbage collection may reclaim otherwise
unreferenced projection intermediates.

## Automatic synchronization

> Superseded by
> [ADR-013: Queued Recorder Workers and Incremental Projection](013-queued-recorder-workers.md).
> This section records ADR-006's original behavior.

When recording is enabled, normal execution requests a sync after these durable
checkpoints:

- run creation;
- a persisted stage success, failure, or pause;
- judge-validation completion;
- optimization selection and final fit;
- aggregation; and
- report publication or terminal lifecycle state.

The request may be handled by a bounded background recorder worker. The worker
never receives mutable in-memory stage output as its authority; it reopens the
local run and derives the latest plan. Finalization flushes pending work within
a configured timeout and leaves durable pending state if the service remains
unavailable.

Resuming a run first reconciles its recorder binding and pending outbox. It does
not require the recorder to be available before semantic work can continue
under the `continue` failure policy.

## CLI contract

Recording is automatic for normal runs when enabled. Explicit commands provide
diagnosis, repair, and historical backfill:

```bash
uv run ms-agent-eval recorder doctor \
  --workspace experiments/mainsequence-sdk/workspace.yaml

uv run ms-agent-eval recorder status \
  --workspace experiments/mainsequence-sdk/workspace.yaml \
  --run <run-id>

uv run ms-agent-eval recorder sync \
  --workspace experiments/mainsequence-sdk/workspace.yaml \
  --run <run-id>

uv run ms-agent-eval recorder sync \
  --workspace experiments/mainsequence-sdk/workspace.yaml \
  --all
```

`recorder doctor` is read-only except for an explicitly identified temporary
recorder probe. It verifies:

- adapter installation and client/server version compatibility;
- server reachability and authentication;
- experiment create/read permission;
- temporary artifact upload, download, and integrity;
- table and trace capabilities; and
- the effective content and artifact policies.

It never starts a server, runs a case, calls a model, creates experiment
evidence, or changes benchmark state.

`recorder sync --all` projects existing historical runs without author, solver,
judge, or optimizer calls. This backfill path is a required part of the
contract, not a migration convenience.

## Dependency boundary

MLflow is an optional integration dependency, not a core runtime dependency.
The package exposes a pinned, tested extra such as:

```toml
[project.optional-dependencies]
mlflow = ["mlflow==<tested-version>"]
```

The exact client version is selected and pinned when the adapter implementation
lands with its compatibility tests. A workspace selecting `type: mlflow`
without the extra installed receives a direct installation error. Workspaces
with recording disabled do not import MLflow.

The adapter registry must allow another recorder implementation without edits
to stage semantics, the coordinator's model path, or the canonical artifact
store.

## Deployment and storage

Local MLflow state must not be created in the Git repository. The documented
local default is beneath the external MS Agent Eval data area, for example:

```text
~/ms_agent_eval/recorders/mlflow/
├── mlflow.db
└── artifacts/
```

Local instructions start an explicit tracking server with SQLite and an
external artifact directory. They do not rely on an implicit `./mlruns` or
`./mlflow.db` in the current directory.

Shared deployments use an MLflow tracking server, a supported SQL backend, and
durable artifact storage. Server lifecycle and database administration remain
deployment responsibilities, not coordinator responsibilities. The evaluation
CLI connects to an already configured service.

## Privacy and security

Prompts, responses, expected answers, rubrics, repository fragments, and judge
reasoning can contain private source or benchmark answers. Recording therefore
requires an explicit content policy:

| Policy | External content |
| --- | --- |
| `hashes_only` | identities, scalar metrics, hashes, and sanitized metadata |
| `summary` | aggregate reports and non-sensitive result tables |
| `full` | complete configured prompts, responses, and evidence as artifacts, plus trace provenance with bounded artifact pointers |

`artifact_policy` independently controls whether portable artifacts are
uploaded or only indexed. The effective policy and its hash are recorded on the
external run.

The implementation must:

- exclude credentials and resolved environment secrets from plans and hashes;
- never upload `.env` files, provider headers, or authentication configuration;
- redact secrets from transport errors;
- scan selected text artifacts for recognized credential patterns before
  upload;
- support TLS verification and an optional configured CA path;
- avoid exposing hidden judge evidence to solver traces; and
- preserve the stage-specific information boundary defined by ADR-001 and
  ADR-002.

A recorder configured with `full` content does not expand what any model can
see. It controls only what the operator authorizes the external tracking system
to retain after the canonical stage has run.

Trace span input and output fields must never carry arbitrary prompt or response
bodies. MLflow persists those fields through size-bounded attributes, and a
truncated serialized object is not valid JSON. The projection therefore places
the complete bytes under `evidence/model-calls/...` and records only content
ids, sizes, media types, and MLflow artifact paths in the trace. This preserves
full evidence without making trace durability depend on provider payload size.

Complete tables are projected only after a run reaches `completed`, `failed`,
or `cancelled`. Live checkpoints keep scalar progress, compact traces, and
same-path graph artifacts current without generating a new content-addressed
full-table object after every stage transition.

## Failure semantics

Recorder failures are operational failures, not model or benchmark failures.
The CLI and reports distinguish:

```text
experiment status: completed
recording status: pending
recording destination: local-mlflow
last recording error: connection unavailable
```

The initial recording states are:

```text
disabled
pending
syncing
synced
partially_synced
failed
```

A recording error never becomes a stage-result error, never changes a judge
score, and never publishes a failed stage result into the reuse graph.

An unavailable server retains the outbox and permits later `recorder sync`.
Partial delivery retains completed receipts and retries only missing or
ambiguous events. A malformed canonical artifact fails projection visibly and
is not silently omitted.

## Observability of the recorder

Recorder operations must not recursively record themselves as model or
experiment stages. Their operational telemetry is limited to:

- projection duration;
- queue depth;
- delivered, pending, and failed event counts;
- uploaded bytes;
- retry counts; and
- sanitized adapter errors.

This telemetry appears in `recorder status` and local receipts. It may be
attached to the MLflow root run after successful synchronization, but it is not
part of benchmark scoring or model cost.

## Testing strategy

The implementation requires the following test layers.

### Core contract tests

- recorder configuration parsing and secret exclusion;
- recording enabled/disabled semantic identity invariance;
- deterministic plan and event identities;
- capability degradation into portable artifacts;
- stable metric steps under concurrent completion;
- fake-recorder ordering and receipt behavior; and
- projection from frozen revisions rather than current workspace state.

### Delivery and recovery tests

- repeated sync creates one root and one stable set of stage scopes;
- crash between external run creation and local binding persistence;
- crash after artifact upload but before receipt persistence;
- ambiguous timeout and reconciliation;
- parallel processes competing to sync one run;
- pause and resume retaining one external root;
- cancellation and failure status mapping;
- pending outbox behavior while the server is unavailable; and
- garbage collection preserving unsynchronized dependencies.

### MLflow integration tests

- experiment and explicit root-run creation;
- nested stage hierarchy;
- batch metrics and deterministic metric history;
- table upload and download;
- artifact hash verification after download;
- dataset inputs and split context;
- traces, spans, and supported links;
- report replacement below versioned paths;
- full, summary, and hashes-only policies; and
- client/server compatibility against the pinned version.

### Experiment behavior tests

- the same run with recording disabled and enabled has identical experiment,
  work, result, and report hashes;
- recorder configuration changes do not cause model calls;
- historical backfill performs zero author, solver, judge, or optimizer calls;
- reused work records zero new usage and names its source;
- baseline and optimization run structures remain distinct;
- every DAG dependency and reuse edge appears in `execution-dag.json`; and
- the DAG SVG contains rendered dependency/retry paths and remains bounded for
  large runs;
- chart names, units, descriptions, and source tables agree with the values
  passed to their renderers; and
- recorder outage never changes scores or benchmark validity.

## Implementation sequence

1. Add the schema fields and normalized operational recorder-config hash.
2. Add canonical append-only run lifecycle events and cover every coordinator
   exit path.
3. Add recorder-neutral descriptors, capabilities, plans, bindings, receipts,
   and a fake recorder.
4. Implement deterministic projection from the frozen run, stage ledger, model
   calls, compiled program, and reports.
5. Add the durable outbox, per-run lock, reconciliation, and garbage-collection
   roots.
6. Add recorder-neutral tables, artifact index, DAG JSON/SVG, and summary
   charts.
7. Add the optional pinned MLflow dependency and explicit-client adapter.
8. Integrate bounded automatic checkpoint and finalization sync with the
   coordinator.
9. Add `recorder doctor`, `status`, and `sync`, including `sync --all`
   historical backfill.
10. Document local and shared MLflow deployment, privacy policy, recovery, and
    user workflow.
11. Run fault-injection, concurrency, compatibility, and semantic-invariance
    tests before marking this ADR implemented.

## Implemented surface

Schema v12, the append-only run lifecycle, recorder-neutral plan and protocol,
durable outbox/bindings/receipts, MLflow 3.16 adapter, automatic coordinator
checkpoints, garbage-collection roots, and recorder CLI are implemented in:

- `src/ms_agent_eval/core/workspace.py`;
- `src/ms_agent_eval/core/lifecycle.py`;
- `src/ms_agent_eval/core/recording_projection.py`;
- `src/ms_agent_eval/core/recorder.py`;
- `src/ms_agent_eval/adapters/mlflow.py`;
- `src/ms_agent_eval/core/coordinator.py`; and
- `src/ms_agent_eval/core/garbage_collection.py`.

The pinned MLflow integration test exercises a parent run, nested stage run,
metric, table, artifact bundle, and trace against a local SQL tracking store.
The generic tests cover deterministic plan reconstruction, append-only
lifecycle recovery, retry after outage, secret redaction, idempotent repeated
sync, automatic terminal flush, pending-outbox garbage-collection protection,
and the semantic identity boundary for recorder destination changes. Operator
usage is documented in
[Experiment recording with MLflow](../execution/experiment-recording.md).

## Acceptance criteria

The decision is implemented only when:

- recording on and off produces identical canonical experiment hashes,
  results, scores, and cache decisions;
- changing recorder configuration never re-executes model work;
- exactly one MLflow parent run exists per local run and recorder destination;
- nested scopes, traces, metrics, tables, reports, graphs, and artifacts are
  navigable in MLflow;
- repeated synchronization and process crashes do not create duplicate root or
  stage runs;
- all historical local runs can be backfilled with zero model calls;
- cache reuse is visible and does not double-count inference usage;
- paused and resumed work remains attached to the same MLflow run;
- unavailable recording leaves durable, visible pending state without
  invalidating the experiment;
- downloaded artifacts can be verified against canonical hashes;
- no MLflow database, artifact directory, credential, or generated run output
  is created in Git; and
- the complete unit, fault-injection, concurrency, and pinned-MLflow
  integration suite passes on Python 3.12 or newer.

## Consequences

Positive consequences:

- experiments gain a complete comparison and inspection interface;
- existing runs can be recorded without rerunning expensive model stages;
- MLflow outages do not corrupt or invalidate benchmark work;
- metrics, traces, graphs, artifacts, and human reports share stable local
  identities;
- recorder destinations can change without invalidating cached results; and
- future recorder adapters can reuse the same projection and delivery ledger.

Tradeoffs:

- the framework gains an additional schema, outbox, reconciliation loop, and
  optional dependency;
- external APIs cannot provide perfect exactly-once delivery for every object;
- rich trace and artifact recording can consume significant storage; and
- full-content recording requires deliberate privacy and retention decisions.

## Rejected alternatives

### Replace the local artifact store with MLflow

Rejected. Evaluation correctness, resumption, and cross-run reuse must not
depend on a mutable external service or its availability.

### Record directly from terminal progress events

Rejected. Progress events are incomplete, ephemeral, and unsuitable for crash
recovery or historical backfill.

### Call MLflow throughout solver and judge code

Rejected. Scattered vendor calls couple model execution to MLflow, complicate
concurrency, and cannot produce a deterministic projection.

### Use DSPy autologging as the primary record

Rejected. It does not encode the complete experiment graph, canonical local
identities, case tables, cache-reuse lineage, or reliable backfill semantics.

### Create one MLflow child run per case

Rejected. It creates excessive run cardinality and hides the stable stage
structure. Case attempts are traces and table rows.

### Fail the benchmark whenever MLflow is unavailable

Rejected as the default. Recording availability is independent from model and
judge validity. Durable deferred delivery preserves both results and
operational visibility.

### Include recorder settings in experiment or work hashes

Rejected. A reporting destination does not change the problem, model,
instructions, expected evidence, or output contract and therefore cannot
invalidate semantic work.

## References

- [MLflow Tracking](https://mlflow.org/docs/latest/ml/tracking/)
- [MLflow Tracking API](https://mlflow.org/docs/latest/ml/tracking/tracking-api/)
- [MLflow Python client](https://mlflow.org/docs/latest/api_reference/python_api/mlflow.client.html)
- [MLflow datasets](https://mlflow.org/docs/latest/dataset/)
- [MLflow traces](https://mlflow.org/docs/latest/genai/concepts/trace/)
- [MLflow spans](https://mlflow.org/docs/latest/genai/concepts/span/)
- [MLflow token usage and cost](https://mlflow.org/docs/latest/genai/tracing/token-usage-cost/)
- [MLflow architecture](https://mlflow.org/docs/latest/self-hosting/architecture/overview/)
- [MLflow backend store](https://mlflow.org/docs/latest/self-hosting/architecture/backend-store/)
- [MLflow artifact store](https://mlflow.org/docs/latest/self-hosting/architecture/artifact-store/)
